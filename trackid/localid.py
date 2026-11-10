#!/usr/bin/env python3
"""Local fingerprint library for TrackID: match your own tracks, edits and unreleased material offline.

Landmark ("constellation") fingerprinting: spectral peaks are paired into hashes, stored in SQLite, and a clip
matches when many of its hashes line up at the same time offset in one track. Clips are also tried at a range of
playback speeds, both "vinyl" (pitch follows speed) and "key lock" (pitch stays), so a track pitched a few percent
on the decks still matches.

  python3 localid.py add ~/trackid/library          index files or folders (skips ones already indexed)
  python3 localid.py list                           what's in the library
  python3 localid.py remove 12                      drop a track by id (or by path)
  python3 localid.py match clip.wav [--start 60]    test a match against the library

Needs numpy and ffmpeg (for decoding mp3/flac/m4a/...). Works on any machine: index on a PC, then copy
the .db file to the Pi's data folder.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sqlite3
import subprocess
import sys
import time

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

RATE = 16000
NFFT = 1024                 # 64 ms window
HOP = 256                   # 16 ms per frame
FRAME_S = HOP / RATE
FMIN, FMAX = 16, 320        # bins: 250 Hz .. 5 kHz (below that, kicks and sub-bass all look alike)
T_RAD, F_RAD = 12, 10       # peak neighbourhood: +-192 ms, +-156 Hz
PEAKS_PER_S = 20
BLOCK = int(round(1.0 / FRAME_S))
DT_MIN, DT_MAX = 2, 63      # pair targets 32 ms .. 1 s after the anchor
DF_MAX = 80
FAN = 4
Q_PEAKS_PER_S = 30         # query side: denser peaks and wider fan-out than the index
Q_FAN = 8
MAX_HITS = 3000             # ignore hashes this common: they carry no information
T_BITS = 22
AUDIO_EXT = {".mp3", ".wav", ".flac", ".aif", ".aiff", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".alac"}


# ================================================================== signal

def _maxfilt(a, r, axis):
    """Running max over a centred window of 2r+1 along `axis` (log-doubling, a few array ops)."""
    if r <= 0:
        return a
    a = np.moveaxis(a, axis, 0)
    L = 2 * r + 1
    pad = np.full((L,) + a.shape[1:], -np.inf, dtype=a.dtype)
    m = np.concatenate([a, pad])                     # m[i] = max(a[i .. i+k-1]) as k doubles
    k = 1
    while 2 * k <= L:
        m[:-k] = np.maximum(m[:-k], m[k:])
        k *= 2
    if k < L:
        m[:-(L - k)] = np.maximum(m[:-(L - k)], m[L - k:])
    # m[i] now covers a[i .. i+L-1]; centre it so out[i] covers a[i-r .. i+r]
    out = np.concatenate([np.full((r,) + a.shape[1:], -np.inf, dtype=a.dtype), m])[: a.shape[0]]
    # the first r rows saw only the left pad; recompute them over the partial window
    for i in range(min(r, a.shape[0])):
        out[i] = a[: i + r + 1].max(axis=0)
    return np.moveaxis(out, 0, axis)


def spectrogram(x):
    """float32 16 kHz mono -> log-magnitude (frames x bins FMIN..FMAX), processed in chunks to stay light."""
    x = np.asarray(x, dtype=np.float32)
    if x.size < NFFT:
        return np.zeros((0, FMAX - FMIN), dtype=np.float32)
    win = np.hanning(NFFT).astype(np.float32)
    frames = sliding_window_view(x, NFFT)[::HOP]
    out = np.empty((frames.shape[0], FMAX - FMIN), dtype=np.float32)
    for i in range(0, frames.shape[0], 1024):
        f = frames[i:i + 1024] * win
        mag = np.abs(np.fft.rfft(f, axis=1))[:, FMIN:FMAX]
        out[i:i + 1024] = 20 * np.log10(mag + 1e-6)
    return out


def peaks(x, per_s=PEAKS_PER_S):
    """Spectral peaks as (t_frame, f_bin_absolute) sorted by time, density-capped per second."""
    S = spectrogram(x)
    if S.shape[0] < 3:
        return np.zeros((0, 2), dtype=np.int32)
    M = _maxfilt(_maxfilt(S, F_RAD, 1), T_RAD, 0)
    floor = np.median(S, axis=1, keepdims=True) + 6.0    # per-frame floor: ignores steady noise
    t, f = np.nonzero((S == M) & (S > floor))
    if t.size == 0:
        return np.zeros((0, 2), dtype=np.int32)
    v = S[t, f]
    blk = t // BLOCK
    order = np.lexsort((-v, blk))
    blk_o = blk[order]
    first = np.searchsorted(blk_o, blk_o, side="left")
    keep = order[(np.arange(order.size) - first) < per_s]
    p = np.stack([t[keep], f[keep] + FMIN], axis=1).astype(np.int32)
    return p[np.lexsort((p[:, 1], p[:, 0]))]


def pairs(p, fan=FAN):
    """Anchor/target pairs -> (f1, f2, dt, t1) int arrays."""
    if len(p) < 2:
        e = np.zeros(0, dtype=np.int32)
        return e, e, e, e
    ts = p[:, 0].tolist()
    fs = p[:, 1].tolist()
    f1, f2, dt, t1 = [], [], [], []
    for i in range(len(ts)):
        j = bisect.bisect_left(ts, ts[i] + DT_MIN, i + 1)
        n = 0
        while j < len(ts) and ts[j] - ts[i] <= DT_MAX and n < fan:
            if abs(fs[j] - fs[i]) <= DF_MAX:
                f1.append(fs[i]); f2.append(fs[j]); dt.append(ts[j] - ts[i]); t1.append(ts[i])
                n += 1
            j += 1
    return tuple(np.array(a, dtype=np.int32) for a in (f1, f2, dt, t1))


def _expand(vals, tq):
    """Speed-scaled coordinates land between bins. Where a value is near a bin edge, try both neighbours."""
    vals = [np.asarray(v, dtype=np.float64) for v in vals]
    cur = [np.rint(v) for v in vals]
    src = np.arange(len(tq))
    for k, v in enumerate(vals):
        vk = v[src]
        lo = np.floor(vk)
        frac = vk - lo
        amb = (frac > 0.25) & (frac < 0.75)
        if not amb.any():
            continue
        alt = [c[amb] for c in cur]
        alt[k] = np.where(cur[k][amb] == lo[amb], lo[amb] + 1, lo[amb])
        cur = [np.concatenate([c, a]) for c, a in zip(cur, alt)]
        src = np.concatenate([src, src[amb]])
    return tuple(c.astype(np.int64) for c in cur) + (np.asarray(tq)[src],)


def pack(f1, f2, dt):
    return (f1.astype(np.int64) << 15) | (f2.astype(np.int64) << 6) | dt.astype(np.int64)


def fingerprint(x):
    """(hashes, t_frames) for indexing a whole track."""
    f1, f2, dt, t1 = pairs(peaks(x))
    return pack(f1, f2, dt), t1


# ================================================================== decoding + metadata

def decode(path):
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode(errors="ignore").strip()[:200] or "ffmpeg failed")
    return np.frombuffer(out.stdout, dtype=np.float32)


def read_tags(path):
    tags = {}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", path],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False)
        raw = (json.loads(out.stdout or b"{}").get("format") or {}).get("tags") or {}
        tags = {k.lower(): v for k, v in raw.items()}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    artist, title = tags.get("artist", "").strip(), tags.get("title", "").strip()
    if not (artist and title):
        stem = os.path.splitext(os.path.basename(path))[0]
        if " - " not in stem and "_-_" in stem:              # Artist_-_Title style names
            stem = stem.replace("_", " ")
        if " - " in stem:
            a, t = stem.split(" - ", 1)
            artist, title = artist or a.strip(), title or t.strip()
        else:
            title = title or stem
    return {"artist": artist, "title": title, "album": tags.get("album", "").strip(),
            "label": (tags.get("label") or tags.get("publisher") or "").strip(),
            "release_date": (tags.get("date") or tags.get("year") or "").strip()}


# ================================================================== library

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks(id INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime REAL, artist TEXT, title TEXT,
  album TEXT, label TEXT, release_date TEXT, duration REAL, n_hashes INTEGER, added REAL, fp BLOB);
"""


def speed_variants(lo=0.92, hi=1.08, step=0.01, keylock=True):
    """(time_scale, freq_scale) pairs. Speed s: the clip plays s times faster than the original."""
    n = int(round((hi - lo) / step))
    out = [(1.0, 1.0)]
    for i in range(n + 1):
        s = round(lo + i * step, 4)
        if abs(s - 1.0) < 1e-6:
            continue
        out.append((s, s))                       # vinyl: pitch moves with speed
        if keylock:
            out.append((s, 1.0))                 # key lock: time stretched, pitch kept
    return out


class Library:
    """Track metadata + per-track fingerprints live in SQLite (<name>.db). For matching, all hashes are
    flattened into two sorted arrays next to it (<name>.h.npy / <name>.v.npy), memory-mapped and searched
    with numpy: a lookup is a binary search, not thousands of SQL rows."""

    def __init__(self, path, speeds=(0.92, 1.08, 0.01), keylock=True, min_score=12, min_ratio=1.5, min_coverage=0.6):
        self.path = path
        self.base = os.path.splitext(path)[0]
        self.variants = speed_variants(*speeds, keylock=keylock)
        self.min_score = min_score
        self.min_ratio = min_ratio
        self.min_coverage = min_coverage
        self._idx = None
        self._idx_mtime = None

    def _conn(self, write=False):
        if not write and not os.path.exists(self.path):
            return None
        if write:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        c = sqlite3.connect(self.path, timeout=30)
        if write:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.executescript(SCHEMA)
        return c

    def count(self):
        c = self._conn()
        if c is None:
            return 0
        try:
            return c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        except sqlite3.Error:
            return 0
        finally:
            c.close()

    def tracks(self):
        c = self._conn()
        if c is None:
            return []
        try:
            rows = c.execute("SELECT id, artist, title, path, duration, n_hashes FROM tracks ORDER BY artist, title").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            c.close()
        return [dict(zip(("id", "artist", "title", "path", "duration", "n_hashes"), r)) for r in rows]

    # ---- indexing -----------------------------------------------------------------------------------
    def add_file(self, path, log=print):
        """Fingerprint one file into the DB. Call rebuild() afterwards (add_paths does)."""
        path = os.path.abspath(path)
        mtime = os.path.getmtime(path)
        c = self._conn(write=True)
        try:
            row = c.execute("SELECT id, mtime FROM tracks WHERE path=?", (path,)).fetchone()
            if row and abs(row[1] - mtime) < 1:
                return None                                   # already indexed, unchanged
            t0 = time.time()
            x = decode(path)
            if x.size < RATE * 5:
                log(f"skip (too short): {path}")
                return None
            h, t = fingerprint(x)
            meta = read_tags(path)
            fp = np.stack([h.astype(np.uint32), t.astype(np.uint32)]).tobytes()
            with c:
                if row:
                    c.execute("DELETE FROM tracks WHERE id=?", (row[0],))
                cur = c.execute("INSERT INTO tracks(path, mtime, artist, title, album, label, release_date, duration,"
                                " n_hashes, added, fp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (path, mtime, meta["artist"], meta["title"], meta["album"], meta["label"],
                                 meta["release_date"], x.size / RATE, int(h.size), time.time(), fp))
                tid = cur.lastrowid
            log(f"indexed #{tid}: {meta['artist']} - {meta['title']} ({h.size} hashes, {time.time() - t0:.1f} s)")
            return tid
        finally:
            c.close()

    def rebuild(self):
        """Flatten every track's hashes into the sorted lookup arrays (atomic replace)."""
        c = self._conn(write=True)
        hs, vs = [], []
        try:
            for tid, fp in c.execute("SELECT id, fp FROM tracks"):
                a = np.frombuffer(fp, dtype=np.uint32).reshape(2, -1)
                hs.append(a[0])
                vs.append((np.int64(tid) << T_BITS) | a[1].astype(np.int64))
        finally:
            c.close()
        h = np.concatenate(hs) if hs else np.zeros(0, np.uint32)
        v = np.concatenate(vs) if vs else np.zeros(0, np.int64)
        order = np.argsort(h, kind="stable")
        for arr, ext in ((h[order], ".h.npy"), (v[order], ".v.npy")):
            tmp = self.base + ext + ".tmp"
            with open(tmp, "wb") as fh:
                np.save(fh, arr)
            os.replace(tmp, self.base + ext)
        self._idx = None
        return int(h.size)

    def add_paths(self, paths, log=print):
        files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, names in os.walk(p):
                    files += [os.path.join(root, n) for n in sorted(names)
                              if os.path.splitext(n)[1].lower() in AUDIO_EXT and not n.startswith(".")]
            elif os.path.isfile(p):
                files.append(p)
        added = 0
        for f in files:
            try:
                if self.add_file(f, log) is not None:
                    added += 1
            except Exception as exc:                          # keep going: one bad file shouldn't stop a scan
                log(f"failed: {f}: {exc}")
        if added or not os.path.exists(self.base + ".h.npy"):
            self.rebuild()
        return added

    def remove(self, key, rebuild=True):
        c = self._conn(write=True)
        try:
            row = c.execute("SELECT id FROM tracks WHERE id=? OR path=?", (key, os.path.abspath(str(key)))).fetchone()
            if not row:
                return False
            with c:
                c.execute("DELETE FROM tracks WHERE id=?", (row[0],))
        finally:
            c.close()
        if rebuild:
            self.rebuild()
        return True

    def prune_missing(self, log=print):
        n = 0
        for t in self.tracks():
            if not os.path.exists(t["path"]):
                self.remove(t["id"], rebuild=False)
                log(f"removed (file gone): {t['artist']} - {t['title']}")
                n += 1
        if n:
            self.rebuild()
        return n

    def _index(self):
        hp = self.base + ".h.npy"
        try:
            m = os.path.getmtime(hp)
        except OSError:
            return None
        if self._idx is None or self._idx_mtime != m:
            try:
                self._idx = (np.load(hp, mmap_mode="r"), np.load(self.base + ".v.npy", mmap_mode="r"))
                self._idx_mtime = m
            except (OSError, ValueError):
                return None
        return self._idx

    # ---- matching ----------------------------------------------------------------------------------
    def match(self, x):
        """Best match for a clip (float32 16 kHz mono) or None.
        Returns AudD-style fields plus score/speed/source, with timecode = where in the track the clip starts."""
        c = self._conn()
        if c is None:
            return None
        try:
            p = peaks(x, Q_PEAKS_PER_S)                       # denser than the index: more chances to hit
            if len(p) < 10:
                return None
            f1, f2, dt, t1 = pairs(p, Q_FAN)
            if f1.size == 0:
                return None
            f1f, f2f, dtf, t1f = (a.astype(np.float64) for a in (f1, f2, dt, t1))
            per_variant = []
            for ts, fsc in self.variants:
                a, b, d, tq = _expand([f1f / fsc, f2f / fsc, dtf * ts], t1f * ts)
                ok = (a >= FMIN) & (a < FMAX) & (b >= FMIN) & (b < FMAX) & (d >= 1) & (d <= 63)
                h = pack(a[ok], b[ok], d[ok])
                per_variant.append((ts, fsc, h, tq[ok]))
            index = self._index()
            if index is None or not per_variant:
                return None
            H, flat = index
            want = np.unique(np.concatenate([pv[2] for pv in per_variant])).astype(np.uint32)
            lo = np.searchsorted(H, want, "left")
            lens = np.searchsorted(H, want, "right") - lo
            keep = (lens > 0) & (lens <= MAX_HITS)           # very common hashes carry no information
            if not keep.any():
                return None
            keys = want[keep].astype(np.int64)
            starts = lo[keep].astype(np.int64)
            lens = lens[keep].astype(np.int64)

            best = None                                       # (score, tid, off, ts, fsc, coverage)
            per_track = {}                                    # best score of every track, any speed
            for ts, fsc, h, tq in per_variant:
                idx = np.searchsorted(keys, h)
                idx[idx >= keys.size] = 0
                found = keys[idx] == h
                idx, tq = idx[found], tq[found]
                if idx.size == 0:
                    continue
                n = lens[idx]
                rep_tq = np.repeat(tq, n)
                pos = np.repeat(starts[idx] - np.cumsum(n) + n, n) + np.arange(int(n.sum()))
                v = np.asarray(flat[pos], dtype=np.int64)
                tid = v >> T_BITS
                off = np.rint(((v & ((1 << T_BITS) - 1)) - rep_tq) / 2.0).astype(np.int64)   # 32 ms bins
                key = (tid << 24) | (off + (1 << 23))
                uk, cnt = np.unique(key, return_counts=True)
                # let neighbouring offset bins share votes (timing jitter between query and reference)
                ukl = uk.tolist()
                cnt_map = dict(zip(ukl, cnt.tolist()))
                vb, vk = 0, None
                for i in np.argsort(-cnt)[:50].tolist():
                    k = ukl[i]
                    sc = cnt_map[k] + cnt_map.get(k - 1, 0) + cnt_map.get(k + 1, 0)
                    t = k >> 24
                    if sc > per_track.get(t, 0):
                        per_track[t] = sc
                    if sc > vb:
                        vb, vk = sc, k
                if vk is not None and (best is None or vb > best[0]):
                    # coverage: how many seconds of the clip voted for this alignment. Real matches agree
                    # all through the clip; coincidences (a shared drum loop, one chord) bunch up.
                    sel = np.abs(key - vk) <= 1
                    cov = np.unique((rep_tq[sel] / ts // BLOCK).astype(np.int64)).size
                    best = (vb, vk >> 24, (vk & ((1 << 24) - 1)) - (1 << 23), ts, fsc, cov)
            if best is None:
                return None
            score, tid, off, ts, fsc, cov = best
            second = max([sc for t, sc in per_track.items() if t != tid] or [0])
            clip_s = max(1, int(len(x) / RATE))
            need = max(3, int(round(clip_s * self.min_coverage)))
            if score >= 2.5 * self.min_score:                 # very strong alignment: allow some dropouts
                need = max(3, need - 2)
            if score < self.min_score or score < self.min_ratio * max(second, 1) or cov < need:
                return None
            row = c.execute("SELECT artist, title, album, label, release_date, path FROM tracks WHERE id=?",
                            (int(tid),)).fetchone()
            if not row:
                return None
            pos_s = max(0.0, off * 2 * FRAME_S)                 # where in the original the clip starts
            elapsed = pos_s / ts                                # real time since the track started playing
            return {"artist": row[0] or "", "title": row[1] or "", "album": row[2] or "", "label": row[3] or "",
                    "release_date": row[4] or "", "link": "", "timecode": f"{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}",
                    "source": "local", "score": int(score), "speed": round(ts, 3),
                    "keylock": bool(ts != 1.0 and fsc == 1.0), "local_id": int(tid), "coverage": int(cov)}
        finally:
            c.close()


# ================================================================== CLI

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="TrackID local fingerprint library")
    ap.add_argument("--db", default=os.path.join(here, "data", "library.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="index audio files / folders")
    a.add_argument("paths", nargs="+")
    a.add_argument("--prune", action="store_true", help="also drop tracks whose files are gone")
    sub.add_parser("list", help="list indexed tracks")
    r = sub.add_parser("remove", help="remove a track by id or path")
    r.add_argument("key")
    m = sub.add_parser("match", help="match an audio file (or a slice of it) against the library")
    m.add_argument("file")
    m.add_argument("--start", type=float, default=0.0)
    m.add_argument("--len", type=float, default=10.0)
    args = ap.parse_args()
    lib = Library(args.db)
    if args.cmd == "add":
        n = lib.add_paths(args.paths)
        if args.prune:
            lib.prune_missing()
        print(f"{n} new/changed track(s) indexed; library has {lib.count()} track(s)")
    elif args.cmd == "list":
        for t in lib.tracks():
            print(f"{t['id']:4d}  {t['artist']} - {t['title']}  ({int(t['duration'] or 0) // 60}:{int(t['duration'] or 0) % 60:02d})")
        print(f"{lib.count()} track(s) in {args.db}")
    elif args.cmd == "remove":
        print("removed" if lib.remove(args.key) else "not found")
    elif args.cmd == "match":
        x = decode(args.file)
        x = x[int(args.start * RATE): int((args.start + args.len) * RATE)]
        t0 = time.time()
        res = lib.match(x)
        print(json.dumps(res, indent=2) if res else "no match", f"({time.time() - t0:.2f} s)", file=sys.stdout)


if __name__ == "__main__":
    main()
