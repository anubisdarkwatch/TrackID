#!/usr/bin/env python3
"""TrackID v2 - always-listening track ID + setlist logger for a Pi Zero 2W with the PiSugar Whisplay HAT.

The mic is always recording into a 20 s ring buffer, so an ID uses the last 10 s instantly.
Live BPM comes from the Beat Commander's beat engine (beat.py) running on the same audio.

Button:  tap        = identify the last 10 s now
         double tap = AUTO on/off (IDs on its own every few minutes while music plays)
         hold 1.5 s = start a new set
Web:     http://<pi-ip>:8080  setlist with plain / YouTube chapters / Mixcloud / CSV exports
Preview: python3 trackid.py --preview DIR   renders every screen to PNG, no hardware needed
"""
from __future__ import annotations

import argparse
import configparser
import csv
import html
import io
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
from urllib.parse import parse_qs, urlparse

import numpy as np

try:
    import requests
except ImportError:
    requests = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
from beat import BeatEngine, RATE  # noqa: E402
from change import ChangeDetector  # noqa: E402
import localid  # noqa: E402
from screens import Screens, W, H, to_rgb565, hue_rgb, GREEN, PURPLE, BLUE, RED, PINK, AMBER, CYAN  # noqa: E402

VERSION = "2.2"
RING_S = 20                 # seconds of audio kept
DOUBLE_TAP_S = 0.35
LONG_PRESS_S = 1.5
RETRY_EVERY_S = 45
log = logging.getLogger("trackid")

DEFAULT_CONFIG = {
    "audd": {"token": "", "market": "us"},
    "capture": {"seconds": "10", "device": "auto", "gain": "1.0"},
    "auto": {"enabled": "false", "trigger": "change", "interval": "150", "min_level": "0.01",
             "sensitivity": "0.4", "min_gap": "60", "max_interval": "420", "max_per_hour": "30"},
    "local": {"enabled": "true", "folder": "library", "db": "library.db", "speed_range": "8", "keylock": "true",
              "min_score": "12", "rescan": "10"},
    "export": {"include_unknown": "true", "unknown_label": "ID - ID"},
    "display": {"brightness": "70", "led": "true", "led_beat": "true", "fps": "12"},
    "web": {"enabled": "true", "port": "8080", "edit": "true", "remote": "true"},
    "storage": {"data_dir": "data", "keep_clips": "false"},
    "conductor": {"url": ""},
}


def load_config(path):
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULT_CONFIG)
    cfg.read(path)
    return cfg


def fmt_hms(sec, force_hours=False):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if (h or force_hours) else f"{m:02d}:{s:02d}"


def parse_timecode(tc):
    try:
        parts = [int(p) for p in str(tc).split(":")]
    except ValueError:
        return None
    v = 0
    for p in parts:
        v = v * 60 + p
    return v


# ================================================================== setlists

def eid(e):
    """Stable id for a setlist entry (older entries had none: their timestamp stands in)."""
    return e.get("id") or f"{float(e.get('ts', 0)):.6f}"


def new_eid():
    return uuid.uuid4().hex[:10]


def eff_start(e):
    """When this track started in the room: a manual edit wins, then AudD/local position, then the clip time."""
    for k in ("start_override", "est_start", "ts"):
        if e.get(k) is not None:
            return float(e[k])
    return 0.0


class SetStore:
    """One JSONL file per set. Exports are regenerated on every change."""

    def __init__(self, data_dir, include_unknown=True, unknown_label="ID - ID"):
        self.dir = os.path.join(data_dir, "sets")
        self.clip_dir = os.path.join(data_dir, "clips")
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(self.clip_dir, exist_ok=True)
        self.include_unknown = include_unknown
        self.unknown_label = unknown_label
        self.lock = threading.RLock()
        self.cur_file = os.path.join(data_dir, "current_set")
        self.set_id = self._read_current() or self.new_set()

    def _read_current(self):
        try:
            sid = open(self.cur_file).read().strip()
            return sid if sid and os.path.exists(self.path(sid)) else None
        except OSError:
            return None

    def path(self, sid, ext="jsonl"):
        return os.path.join(self.dir, f"{sid}.{ext}")

    def new_set(self):
        with self.lock:
            sid = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            open(self.path(sid), "a").close()
            with open(self.cur_file, "w") as fh:
                fh.write(sid)
            self.set_id = sid
            self.export(sid)
            return sid

    def started(self, sid=None):
        try:
            return datetime.strptime(sid or self.set_id, "%Y-%m-%d_%H%M%S").timestamp()
        except ValueError:
            return 0.0

    def entries(self, sid=None):
        out = []
        try:
            with open(self.path(sid or self.set_id)) as fh:
                for line in fh:
                    if line.strip():
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            pass
        return sorted(out, key=lambda e: e.get("ts", 0))

    def _write(self, sid, entries):
        tmp = self.path(sid) + ".tmp"
        with open(tmp, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path(sid))
        self.export(sid)

    def tracks(self, sid=None):
        return [e for e in self.entries(sid) if e.get("status") == "matched"]

    def unknowns(self, sid=None):
        return [e for e in self.entries(sid) if e.get("status") == "nomatch"]

    def same_as_last(self, r, sid=None, before=None):
        tr = [t for t in self.tracks(sid) if before is None or t["ts"] < before]
        if not tr:
            return False
        a = tr[-1]
        return (a.get("artist", "").lower(), a.get("title", "").lower()) == (r.get("artist", "").lower(), r.get("title", "").lower())

    def seen_in_set(self, r, sid=None):
        key = (r.get("artist", "").lower(), r.get("title", "").lower())
        return any((t.get("artist", "").lower(), t.get("title", "").lower()) == key for t in self.tracks(sid))

    def last_logged(self, sid=None):
        """The most recent matched or unknown entry (what the room was last known to be playing)."""
        es = [e for e in self.entries(sid) if e.get("status") in ("matched", "nomatch")]
        return es[-1] if es else None

    def add(self, e, sid=None):
        with self.lock:
            sid = sid or self.set_id
            e.setdefault("id", new_eid())
            es = self.entries(sid)
            es.append(e)
            self._write(sid, es)
            return e["id"]

    def get(self, sid, entry_id):
        return next((e for e in self.entries(sid) if eid(e) == entry_id), None)

    def update(self, sid, entry_id, changes):
        """Merge `changes` into one entry; keys set to None are removed."""
        with self.lock:
            es = self.entries(sid)
            hit = None
            for e in es:
                if eid(e) == entry_id:
                    for k, v in changes.items():
                        if v is None:
                            e.pop(k, None)
                        else:
                            e[k] = v
                    e.setdefault("id", entry_id)
                    hit = e
            if hit is not None:
                self._write(sid, es)
            return hit

    def delete(self, sid, entry_id):
        with self.lock:
            es = self.entries(sid)
            keep = [e for e in es if eid(e) != entry_id]
            gone = [e for e in es if eid(e) == entry_id]
            if not gone:
                return False
            self._write(sid, keep)
        for e in gone:
            clip = e.get("clip")
            if clip and os.path.dirname(os.path.abspath(clip)) == os.path.abspath(self.clip_dir):
                try:
                    os.remove(clip)
                except OSError:
                    pass
        return True

    def pending(self):
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".jsonl"):
                sid = name[:-6]
                out += [(sid, e) for e in self.entries(sid) if e.get("status") == "pending"]
        return out

    def list_sets(self):
        return sorted((n[:-6] for n in os.listdir(self.dir) if n.endswith(".jsonl")), reverse=True)

    # ---- exports -----------------------------------------------------------------------------------
    def timeline(self, sid=None, include_unknown=None):
        """Logged tracks (and, optionally, unidentified ones) with offsets from the set start.
        Start = manual edit, else where AudD / the library says the song started, else the clip time.
        Offsets never go backwards."""
        sid = sid or self.set_id
        inc = self.include_unknown if include_unknown is None else include_unknown
        start = self.started(sid)
        want = ("matched", "nomatch") if inc else ("matched",)
        rows, last = [], 0
        for e in sorted((e for e in self.entries(sid) if e.get("status") in want), key=eff_start):
            off = max(0, int(eff_start(e) - start)) if start else 0
            off = max(off, last)
            last = off
            rows.append((off, e))
        return rows

    def name(self, e):
        """'Artist - Title', or the unknown label for an unidentified track."""
        if e.get("status") == "nomatch":
            return self.unknown_label
        return f"{e.get('artist', '?')} - {e.get('title', '?')}"

    def export_text(self, fmt, sid=None):
        sid = sid or self.set_id
        rows = self.timeline(sid)
        long_set = rows and rows[-1][0] >= 3600
        if fmt == "youtube":
            # YouTube chapters: first at 00:00, each at least 10 s after the previous one
            lines, prev = [], None
            if not rows or rows[0][0] >= 10:
                lines.append(f"{fmt_hms(0, long_set)} Intro")
                prev = 0
            for off, e in rows:
                if prev is not None and off < prev + 10:
                    off = prev + 10
                prev = off
                lines.append(f"{fmt_hms(off, long_set)} {self.name(e)}")
            return "\n".join(lines) + "\n"
        if fmt == "mixcloud":
            return "".join(f"{fmt_hms(off, True)}  {self.name(e).replace(' - ', ' – ', 1)}\n" for off, e in rows) \
                or "(no tracks yet)\n"
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["n", "start", "logged", "artist", "title", "album", "year", "label", "bpm", "link", "status", "source"])
            for i, (off, e) in enumerate(rows, 1):
                w.writerow([i, fmt_hms(off, True), e.get("clock", ""), e.get("artist", ""), e.get("title", ""),
                            e.get("album", ""), (e.get("release_date") or "")[:4], e.get("label", ""),
                            f"{e['bpm']:.0f}" if e.get("bpm") else "", e.get("link", ""),
                            "unknown" if e.get("status") == "nomatch" else "matched", e.get("source", "")])
            return buf.getvalue()
        start = self.started(sid)
        head = f"Tracklist - {datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M') if start else sid}\n\n"
        body = "".join(f"{i:02d}. [{fmt_hms(off, True)}] {self.name(e)}"
                       f"{f'  ({e['bpm']:.0f} BPM)' if e.get('bpm') else ''}\n" for i, (off, e) in enumerate(rows, 1))
        return head + (body or "(no tracks yet)\n")

    def export(self, sid):
        for fmt, ext in (("plain", "txt"), ("csv", "csv"), ("youtube", "youtube.txt"), ("mixcloud", "mixcloud.txt")):
            with open(self.path(sid, ext), "w", encoding="utf-8", newline="") as fh:
                fh.write(self.export_text(fmt, sid))


# ================================================================== audio

def find_capture_devices():
    """Same detection order as PiSugar's own test.py."""
    idx, name = 1, "1"
    try:
        fallback = None
        with open("/proc/asound/cards") as fh:
            for line in fh:
                parts = line.strip().split()
                if not parts or not parts[0].isdigit():
                    continue
                low = line.lower()
                cand = (int(parts[0]), parts[1].strip("[]:") if len(parts) > 1 else parts[0])
                if "whisplaysound" in low:
                    fallback = cand
                    break
                if fallback is None and ("wm8960" in low or "es8389" in low):
                    fallback = cand
        if fallback:
            idx, name = fallback
    except OSError:
        pass
    if name == "whisplaysound":
        return ["whisplaysound", "default"]
    return ([f"plughw:CARD={name}"] if not name.isdigit() else []) + [f"plughw:{idx}"]


class Capture(threading.Thread):
    """Runs arecord continuously: 48 kHz stereo in, 16 kHz mono into a ring buffer + the beat engine."""

    def __init__(self, devices, engine, gain=1.0, source=None, listeners=()):
        super().__init__(daemon=True)
        self.devices = devices
        self.engine = engine
        self.listeners = list(listeners)  # extra analysers fed the same audio (track-change detector)
        self.gain = gain
        self.source = source              # test hook: callable returning float32 16 kHz chunks
        self.ring = np.zeros(RING_S * RATE, dtype=np.float32)
        self.filled = 0
        self.wpos = 0
        self.lock = threading.Lock()
        self.ok = False
        self.error = ""
        self.running = True
        self.t_last = 0.0
        self.peak = 0.0

    def _push(self, x):
        n = x.size
        with self.lock:
            end = self.wpos + n
            if end <= self.ring.size:
                self.ring[self.wpos:end] = x
            else:
                k = self.ring.size - self.wpos
                self.ring[self.wpos:] = x[:k]
                self.ring[:n - k] = x[k:]
            self.wpos = end % self.ring.size
            self.filled = min(self.ring.size, self.filled + n)
            self.t_last = time.time()
        self.peak = max(self.peak * 0.95, float(np.abs(x).max()) if n else 0.0)
        self.engine.feed(x, time.time() * 1000.0 - n * 1000.0 / RATE)
        for li in self.listeners:
            try:
                li.feed(x)
            except Exception:
                log.exception("audio listener failed")

    def last(self, seconds):
        """Most recent `seconds` of audio (float32, 16 kHz mono) and its start time, or (None, 0)."""
        n = int(seconds * RATE)
        with self.lock:
            if self.filled < n:
                return None, 0.0
            start = (self.wpos - n) % self.ring.size
            if start + n <= self.ring.size:
                x = self.ring[start:start + n].copy()
            else:
                x = np.concatenate([self.ring[start:], self.ring[:self.wpos]])
            return x, self.t_last - seconds

    def buffered_s(self):
        return self.filled / RATE

    def run(self):
        if self.source:
            self.ok = True
            while self.running:
                x = self.source()
                if x is None:
                    break
                self._push(x)
            return
        while self.running:
            for dev in list(self.devices):
                cmd = ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", "48000", "-c", "2", "-t", "raw"]
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                except OSError as exc:
                    self.error = str(exc)
                    continue
                got_any = False
                tail = np.zeros(0, dtype=np.float32)
                while self.running:
                    raw = proc.stdout.read(4800 * 4)           # 100 ms
                    if not raw:
                        break
                    got_any = True
                    self.ok = True
                    a = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<i2").astype(np.float32) / 32768.0
                    mono = a.reshape(-1, 2).sum(axis=1)        # the mic lands on one slot: sum both
                    mono = np.concatenate([tail, mono])
                    k = mono.size // 3 * 3
                    tail = mono[k:]
                    x = mono[:k].reshape(-1, 3).mean(axis=1) * self.gain   # 48 k -> 16 k
                    self._push(np.clip(x, -1, 1).astype(np.float32))
                proc.kill()
                err = proc.stderr.read().decode(errors="ignore").strip()
                if got_any:
                    self.devices = [dev] + [d for d in self.devices if d != dev]
                    log.warning("capture stopped on %s: %s (restarting)", dev, err)
                    break
                self.error = err or f"arecord failed on {dev}"
                log.warning("capture failed on %s: %s", dev, self.error)
            self.ok = False
            time.sleep(2)


def wav_to_float(wav_bytes):
    """16-bit mono WAV bytes (as written by clip_wav) -> float32 16 kHz."""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        a = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if wf.getnchannels() > 1:
            a = a.reshape(-1, wf.getnchannels()).mean(axis=1)
        if wf.getframerate() != RATE:
            n = int(a.size * RATE / wf.getframerate())
            a = np.interp(np.linspace(0, a.size - 1, n), np.arange(a.size), a).astype(np.float32)
    return a


def clip_wav(x):
    """float32 16 kHz mono -> normalised 16-bit WAV bytes (~320 KB for 10 s)."""
    x = x - float(x.mean())
    peak = float(np.abs(x).max()) or 1.0
    x = x * min(20.0, 0.9 / peak)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


# ================================================================== recognition

class NetworkError(Exception):
    pass


class AuddError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"AudD #{code}: {msg}")
        self.code = code


def audd_identify(wav_bytes, token, market="us", timeout=40):
    if requests is None:
        raise RuntimeError("python3-requests not installed")
    data = {"return": "spotify", "market": market}
    if token:
        data["api_token"] = token
    try:
        r = requests.post("https://api.audd.io/", data=data, files={"file": ("clip.wav", wav_bytes, "audio/wav")},
                          timeout=timeout)
        r.raise_for_status()
        j = r.json()
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise NetworkError(str(exc))
    except (ValueError, requests.HTTPError) as exc:
        raise NetworkError(f"bad response from AudD: {exc}")
    if j.get("status") != "success":
        err = j.get("error") or {}
        raise AuddError(err.get("error_code", "?"), err.get("error_message", "unknown error"))
    res = j.get("result")
    if not res or not isinstance(res, dict):
        return None
    sp = res.get("spotify") or {}
    return {
        "artist": res.get("artist", ""),
        "title": res.get("title", ""),
        "album": res.get("album", ""),
        "release_date": res.get("release_date", ""),
        "label": res.get("label", ""),
        "timecode": res.get("timecode", ""),
        "link": ((sp.get("external_urls") or {}).get("spotify")) or res.get("song_link", ""),
        "source": "audd",
    }


def have_internet(timeout=3):
    try:
        socket.create_connection(("api.audd.io", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


# ================================================================== web

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TrackID</title><style>
:root{{--bg:#06060a;--panel:#12111a;--line:#24232f;--fg:#ececf2;--dim:#8a8a99;--amber:#ffb000;--green:#39ff14;--purple:#8c6eff}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 ui-monospace,Menlo,Consolas,monospace}}
.wrap{{max-width:780px;margin:0 auto;padding:16px}} a{{color:var(--green)}} h1{{margin:4px 0 2px;font-size:22px;letter-spacing:.1em}}
h2{{font-size:13px;color:var(--dim);letter-spacing:.12em;text-transform:uppercase;margin:26px 0 8px}}
.live{{display:flex;gap:18px;flex-wrap:wrap;color:var(--dim);font-size:13px}} .live b{{color:var(--fg);font-size:18px}}
.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}} .tabs a{{padding:6px 10px;border:1px solid var(--line);color:var(--dim);text-decoration:none}}
.tabs a.on{{background:var(--amber);color:#000;border-color:var(--amber)}}
pre{{background:var(--panel);border:1px solid var(--line);padding:12px;white-space:pre-wrap;word-break:break-word;margin:0}}
button{{font:inherit;background:var(--purple);color:#fff;border:0;padding:8px 14px;margin-top:8px;cursor:pointer}}
ul{{padding-left:18px}} .note{{color:var(--dim);font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{border-bottom:1px solid var(--line);padding:5px 4px;vertical-align:middle;text-align:left}}
th{{color:var(--dim);font-weight:normal}} input{{font:inherit;background:var(--panel);color:var(--fg);border:1px solid var(--line);padding:4px 6px;width:100%}}
input.t{{width:84px}} td.n{{color:var(--dim);width:22px}} .tag{{font-size:11px;padding:1px 6px;border:1px solid var(--line);color:var(--dim);white-space:nowrap}}
.tag.unk{{color:var(--amber);border-color:var(--amber)}} .tag.loc{{color:var(--green);border-color:var(--green)}}
td.act{{white-space:nowrap}} td.act button{{margin:0 2px 0 0;padding:4px 8px;font-size:12px;background:var(--line)}} td.act button.go{{background:var(--purple)}}
td.act button.del{{color:#ff6b8a}} tr.dup td{{opacity:.55}} audio{{height:28px;max-width:180px;vertical-align:middle}} #msg{{color:var(--amber);min-height:1.5em}}
@media (max-width:640px){{td.act{{white-space:normal}} input.t{{width:70px}}}}
</style></head><body><div class="wrap"><h1>TRACKID</h1>{body}</div>
<script>
function copyOut(){{const t=document.getElementById('out').innerText;
 (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>{{document.getElementById('cp').innerText='Copied'}})
 .catch(()=>{{const r=document.createRange();r.selectNodeContents(document.getElementById('out'));const s=getSelection();s.removeAllRanges();s.addRange(r);}});}}
async function poll(){{try{{const s=await (await fetch('/api/state')).json();
 document.getElementById('bpm').innerText=s.bpm?Math.round(s.bpm):'---';document.getElementById('mode').innerText=s.auto?'AUTO':'MANUAL';
 document.getElementById('cnt').innerText=s.count;}}catch(e){{}} setTimeout(poll,2000);}} poll();
</script></body></html>"""

FORMATS = [("plain", "Tracklist", "txt"), ("youtube", "YouTube chapters", "youtube.txt"),
           ("mixcloud", "Mixcloud", "mixcloud.txt"), ("csv", "CSV", "csv")]


EDIT_JS = r"""
const SID = document.getElementById('ed').dataset.sid;
const H = {'Content-Type': 'application/json', 'X-TrackID': '1'};
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }
function say(t) { document.getElementById('msg').innerText = t; }
async function post(path, data) {
  const r = await fetch(path, {method: 'POST', headers: H, body: JSON.stringify(Object.assign({set: SID}, data))});
  const j = await r.json().catch(() => ({ok: false, error: 'bad response'}));
  if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
function tag(e) {
  if (e.status === 'nomatch') return '<span class="tag unk">ID?</span>';
  if (e.status === 'pending') return '<span class="tag">queued</span>';
  if (e.status === 'duplicate') return '<span class="tag">repeat</span>';
  if (e.source === 'local') return '<span class="tag loc">library' + (e.speed && Math.abs(e.speed - 1) > 0.005 ? ' ' + (e.speed > 1 ? '+' : '') + Math.round(e.speed * 100 - 100) + '%' : '') + '</span>';
  if (e.source === 'manual') return '<span class="tag">manual</span>';
  return '<span class="tag">' + esc(e.via || '') + '</span>';
}
async function load() {
  const j = await (await fetch('/api/set?id=' + encodeURIComponent(SID))).json();
  let n = 0;
  const rows = j.entries.map(e => {
    const counted = e.status === 'matched' || e.status === 'nomatch';
    return '<tr data-id="' + esc(e.id) + '" class="' + (counted ? '' : 'dup') + '">' +
      '<td class="n">' + (counted ? ++n : '') + '</td>' +
      '<td><input class="t" name="start" value="' + esc(e.start) + '" title="Start in the set (h:mm:ss)"></td>' +
      '<td><input name="artist" value="' + esc(e.artist) + '" placeholder="' + (e.status === 'nomatch' ? 'ID' : 'artist') + '"></td>' +
      '<td><input name="title" value="' + esc(e.title) + '" placeholder="' + (e.status === 'nomatch' ? 'ID' : 'title') + '"></td>' +
      '<td>' + tag(e) + '</td><td class="act">' +
      '<button class="go" onclick="save(this)">Save</button>' +
      (e.status === 'duplicate' ? '<button onclick="keep(this)">Count it</button>' : '') +
      (e.clip ? '<audio controls preload="none" src="/clips/' + esc(e.clip) + '"></audio><button onclick="reid(this)">Re-ID</button>' : '') +
      '<button class="del" onclick="del(this)">Delete</button></td></tr>';
  }).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td colspan="6" class="note">Nothing logged in this set yet.</td></tr>';
}
function rowData(b) {
  const tr = b.closest('tr'); const d = {id: tr.dataset.id};
  tr.querySelectorAll('input').forEach(i => d[i.name] = i.value.trim());
  return d;
}
async function save(b) { try { await post('/api/edit', rowData(b)); say('Saved.'); load(); } catch (e) { say(e.message); } }
async function keep(b) { try { await post('/api/edit', Object.assign(rowData(b), {status: 'matched'})); say('Counted.'); load(); } catch (e) { say(e.message); } }
async function del(b) {
  if (!confirm('Delete this entry?')) return;
  try { await post('/api/delete', {id: b.closest('tr').dataset.id}); say('Deleted.'); load(); } catch (e) { say(e.message); }
}
async function reid(b) {
  say('Identifying…');
  try { const j = await post('/api/reid', {id: b.closest('tr').dataset.id});
        say(j.match ? 'Found: ' + j.artist + ' - ' + j.title : 'Still no match. Fill it in by hand if you know it.'); load(); }
  catch (e) { say(e.message); }
}
async function addRow() {
  const f = document.getElementById('add'); const d = {};
  f.querySelectorAll('input').forEach(i => d[i.name] = i.value.trim());
  try { await post('/api/add', d); f.querySelectorAll('input').forEach(i => i.value = ''); say('Added.'); load(); }
  catch (e) { say(e.message); }
}
load();
"""


def make_handler(app):
    store = app.store

    def entry_json(sid, e):
        start = store.started(sid)
        return {"id": eid(e), "status": e.get("status", ""), "artist": e.get("artist", ""), "title": e.get("title", ""),
                "start": fmt_hms(eff_start(e) - start, True) if start else "", "clock": e.get("clock", ""),
                "via": e.get("via", ""), "trigger": e.get("trigger", ""), "source": e.get("source", ""),
                "bpm": e.get("bpm"), "speed": e.get("speed"),
                "clip": os.path.basename(e["clip"]) if e.get("clip") and os.path.exists(e["clip"]) else ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            b = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

        def _sid(self, q):
            sid = (q.get("id") or [store.set_id])[0]
            return sid if sid in store.list_sets() else store.set_id

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/state":
                self._send(200, json.dumps(app.state_json()), "application/json",
                           {"Access-Control-Allow-Origin": "*"})
                return
            if u.path == "/api/set":
                sid = self._sid(q)
                self._json(200, {"set": sid, "started": store.started(sid), "current": sid == store.set_id,
                                 "entries": [entry_json(sid, e) for e in store.entries(sid)
                                             if e.get("status") in ("matched", "nomatch", "duplicate", "pending")]})
                return
            if u.path in ("/", "/set"):
                sets = store.list_sets()
                sid = self._sid(q)
                fmt = (q.get("fmt") or ["plain"])[0]
                if fmt not in [f[0] for f in FORMATS]:
                    fmt = "plain"
                tabs = "".join(f'<a class="{"on" if f == fmt else ""}" href="/?id={sid}&fmt={f}">{html.escape(n)}</a>'
                               for f, n, _ in FORMATS)
                if app.web_edit:
                    tabs += f'<a href="/edit?id={sid}">✎ Edit</a>'
                note = ""
                if fmt == "youtube":
                    note = ('<p class="note">Paste into the video description. YouTube needs the first line at 00:00, '
                            'at least 3 chapters, each 10 s or longer. Start times are estimated from where in each song the clip landed.</p>')
                elif fmt == "mixcloud":
                    note = '<p class="note">Start times for Mixcloud\'s tracklist editor (Upload → Tracklist → add each track with its start time).</p>'
                unk = len(store.unknowns(sid))
                if unk:
                    note += (f'<p class="note">{unk} track(s) couldn\'t be identified and show as '
                             f'"{html.escape(store.unknown_label)}".'
                             + (f' <a href="/edit?id={sid}">Listen and fill them in</a>.' if app.web_edit else "") + "</p>")
                live = ('<div class="live"><span>BPM <b id="bpm">---</b></span><span>MODE <b id="mode">-</b></span>'
                        '<span>TRACKS <b id="cnt">0</b></span></div>' if sid == store.set_id else "")
                rows = "".join(f'<li><a href="/?id={s}&fmt={fmt}">{s}</a> · {len(store.tracks(s))} tracks · '
                               + " ".join(f'<a href="/files/{s}.{ext}">{ext}</a>' for _, _, ext in FORMATS) + "</li>"
                               for s in sets)
                body = (f"{live}<h2>{'Current set' if sid == store.set_id else 'Set'} · {sid}</h2>{tabs}{note}"
                        f'<pre id="out">{html.escape(store.export_text(fmt, sid))}</pre>'
                        f'<button id="cp" onclick="copyOut()">Copy</button><h2>All sets</h2><ul>{rows}</ul>')
                self._send(200, PAGE.format(body=body))
                return
            if u.path == "/edit":
                if not app.web_edit:
                    self._send(403, "editing is turned off ([web] edit = false)", "text/plain")
                    return
                sid = self._sid(q)
                body = (f'<h2>Edit set · {html.escape(sid)}</h2>'
                        f'<div class="tabs"><a href="/?id={sid}">← Back to tracklist</a></div>'
                        '<p class="note">Start = where the track begins in the set (h:mm:ss). Fill in artist + title on an '
                        '"ID?" row to name it. Faded rows are repeats and queued clips, left out of the exports.</p>'
                        f'<div id="ed" data-sid="{html.escape(sid)}"><table><thead><tr><th></th><th>Start</th><th>Artist</th>'
                        '<th>Title</th><th></th><th></th></tr></thead><tbody id="rows"></tbody></table></div>'
                        '<p id="msg"></p><h2>Add a track</h2><table id="add"><tr>'
                        '<td><input class="t" name="start" placeholder="0:42:10"></td><td><input name="artist" placeholder="artist"></td>'
                        '<td><input name="title" placeholder="title"></td><td class="act"><button class="go" onclick="addRow()">Add</button></td>'
                        f'</tr></table><script>{EDIT_JS}</script>')
                self._send(200, PAGE.format(body=body))
                return
            if u.path.startswith("/clips/"):
                name = os.path.basename(u.path[7:])
                p = os.path.join(store.clip_dir, name)
                if name.endswith(".wav") and os.path.isfile(p):
                    with open(p, "rb") as fh:
                        self._send(200, fh.read(), "audio/wav")
                    return
            if u.path.startswith("/files/"):
                name = u.path[7:]
                for _, _, ext in FORMATS:
                    if name.endswith("." + ext):
                        sid = name[: -len(ext) - 1]
                        if sid in store.list_sets():
                            with open(store.path(sid, ext), encoding="utf-8") as fh:
                                self._send(200, fh.read(), "text/csv" if ext == "csv" else "text/plain; charset=utf-8",
                                           {"Content-Disposition": f'attachment; filename="{name}"'})
                            return
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            # A custom header can't be sent cross-site without a CORS preflight (which we never allow), so a
            # random web page in your browser can't post to the Pi.
            if self.headers.get("X-TrackID") != "1":
                self._json(403, {"ok": False, "error": "missing X-TrackID header"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(min(n, 65536)) or b"{}") if n else {}
            except ValueError:
                self._json(400, {"ok": False, "error": "bad JSON"})
                return
            if u.path in ("/api/identify", "/api/auto"):
                # remote control (TrackID Ink touch screen, other gadgets): same as the button
                if not app.web_remote:
                    self._json(403, {"ok": False, "error": "remote control is turned off ([web] remote = false)"})
                    return
                if u.path == "/api/identify":
                    started = not app.busy.locked()
                    if started:
                        threading.Thread(target=app.identify, kwargs={"manual": True}, daemon=True).start()
                    self._json(200, {"ok": True, "started": started})
                else:
                    want = d.get("on")
                    if want is None or bool(want) != app.auto:
                        app.toggle_auto()
                    self._json(200, {"ok": True, "auto": app.auto})
                return
            if not app.web_edit:
                self._json(403, {"ok": False, "error": "editing is turned off ([web] edit = false)"})
                return
            sid = d.get("set") or store.set_id
            if sid not in store.list_sets():
                self._json(404, {"ok": False, "error": "no such set"})
                return
            start = store.started(sid)

            def parse_start(v):
                if v in (None, ""):
                    return None
                secs = parse_timecode(v)
                if secs is None or secs < 0 or not start:
                    raise ValueError(f"start time '{v}' isn't h:mm:ss")
                return start + secs

            try:
                if u.path == "/api/edit":
                    e = store.get(sid, str(d.get("id")))
                    if not e:
                        self._json(404, {"ok": False, "error": "no such entry"})
                        return
                    ch = {}
                    artist, title = (d.get("artist") or "").strip(), (d.get("title") or "").strip()
                    if artist != e.get("artist", "") or title != e.get("title", ""):
                        ch.update(artist=artist, title=title)
                        if e.get("status") in ("nomatch", "pending") and (artist or title):
                            ch.update(status="matched", source="manual")
                        elif e.get("source") not in (None, "", "manual"):
                            ch["edited"] = True
                    if not (artist or title) and e.get("status") == "matched" and e.get("source") == "manual":
                        ch["status"] = "nomatch"                  # cleared a hand-named track: back to unknown
                    new_start = parse_start(d.get("start"))
                    cur = fmt_hms(eff_start(e) - start, True) if start else ""
                    if new_start is not None and str(d.get("start")).strip() != cur:
                        ch["start_override"] = new_start
                    if d.get("status") in ("matched", "nomatch"):
                        ch["status"] = d["status"]
                    if ch:
                        store.update(sid, eid(e), ch)
                    self._json(200, {"ok": True, "changed": sorted(ch)})
                elif u.path == "/api/delete":
                    self._json(200, {"ok": store.delete(sid, str(d.get("id")))})
                elif u.path == "/api/add":
                    ts = parse_start(d.get("start"))
                    if ts is None:
                        raise ValueError("give a start time (h:mm:ss into the set)")
                    artist, title = (d.get("artist") or "").strip(), (d.get("title") or "").strip()
                    e = {"ts": ts, "start_override": ts, "clock": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                         "via": "manual", "source": "manual", "artist": artist, "title": title,
                         "status": "matched" if (artist or title) else "nomatch"}
                    self._json(200, {"ok": True, "id": store.add(e, sid)})
                elif u.path == "/api/reid":
                    self._json(200, app.reidentify(sid, str(d.get("id"))))
                else:
                    self._json(404, {"ok": False, "error": "not found"})
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})

    return Handler


# ================================================================== app

class ConductorFeed:
    """Live BPM + beat from a Beat Hub (the Beat Commander's USB gateway), read from its /api/state."""

    def __init__(self, url):
        self.url = url.rstrip("/") + "/api/state"
        self.bpm = 0.0
        self.beat_n = 1
        self.beat_t = 0.0            # time.time() of the hub's last beat
        self.ok_t = 0.0
        self.live = False
        self.last_tb = 0.0

    @property
    def locked(self):
        return self.live and time.time() - self.ok_t < 2.0

    def start(self):
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        while True:
            try:
                with urllib.request.urlopen(self.url, timeout=1.5) as r:
                    st = json.load(r)
                now = time.time()
                if st.get("locked") and st.get("bpm"):
                    self.bpm, self.beat_n = float(st["bpm"]), int(st["beat"])
                    self.beat_t = now - st["beat_age_ms"] / 1000.0
                    self.live, self.ok_t = True, now
                else:
                    self.live = False
            except Exception:
                self.live = False
            time.sleep(0.5)

    def tick(self, now_ms):
        """Beat index 0..3 when a new beat has started since the last call, else None (same as BeatEngine.tick)."""
        if not self.bpm:
            return None
        per = 60.0 / self.bpm
        k = int((now_ms / 1000.0 - self.beat_t) // per)
        tb = self.beat_t + k * per
        if tb > self.last_tb + per / 2:
            self.last_tb = tb
            return (self.beat_n - 1 + k) % 4
        return None


class App:
    def __init__(self, board, cfg, source=None):
        self.board = board
        self.cfg = cfg
        data_dir = cfg.get("storage", "data_dir")
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(APP_DIR, data_dir)
        self.data_dir = data_dir
        self.store = SetStore(data_dir, cfg.getboolean("export", "include_unknown"),
                              cfg.get("export", "unknown_label").strip() or "ID - ID")
        self.scr = Screens()
        self.token = cfg.get("audd", "token").strip()
        self.market = cfg.get("audd", "market").strip() or "us"
        self.seconds = max(5, min(15, cfg.getint("capture", "seconds")))
        self.auto = cfg.getboolean("auto", "enabled")
        self.trigger = cfg.get("auto", "trigger").strip().lower()
        if self.trigger not in ("change", "interval"):
            self.trigger = "change"
        self.interval = max(45, cfg.getint("auto", "interval"))
        self.min_level = cfg.getfloat("auto", "min_level")
        self.min_gap = max(20, cfg.getint("auto", "min_gap"))
        self.max_interval = max(self.min_gap, cfg.getint("auto", "max_interval"))
        self.max_per_hour = max(1, cfg.getint("auto", "max_per_hour"))
        self.use_led = cfg.getboolean("display", "led")
        self.led_beat = cfg.getboolean("display", "led_beat")
        self.fps = max(4, min(20, cfg.getint("display", "fps")))
        self.keep_clips = cfg.getboolean("storage", "keep_clips")
        self.web_port = cfg.getint("web", "port") if cfg.getboolean("web", "enabled") else 0
        self.web_edit = cfg.getboolean("web", "edit")
        self.web_remote = cfg.getboolean("web", "remote")
        self.engine = BeatEngine()
        url = cfg.get("conductor", "url").strip()
        self.feed = ConductorFeed(url) if url else None
        self.detector = ChangeDetector(threshold=cfg.getfloat("auto", "sensitivity"))
        self.detector.bpm_fn = lambda: (self.clock().bpm if self.clock().locked else None)
        # local fingerprint library (your own tracks, edits, unreleased material)
        self.lib = None
        self.lib_folder = ""
        self.lib_rescan = 0
        self.lib_proc = None
        if cfg.getboolean("local", "enabled"):
            db = cfg.get("local", "db").strip() or "library.db"
            db = db if os.path.isabs(db) else os.path.join(data_dir, db)
            rng = max(0.0, min(20.0, cfg.getfloat("local", "speed_range"))) / 100.0
            self.lib = localid.Library(db, speeds=(1 - rng, 1 + rng, 0.01), keylock=cfg.getboolean("local", "keylock"),
                                       min_score=cfg.getint("local", "min_score"))
            folder = cfg.get("local", "folder").strip()
            self.lib_folder = folder if not folder or os.path.isabs(folder) else os.path.join(APP_DIR, folder)
            self.lib_rescan = max(0, cfg.getint("local", "rescan")) * 60
        devs = find_capture_devices() if cfg.get("capture", "device") == "auto" else [cfg.get("capture", "device")]
        self.cap = Capture(devs, self.engine, cfg.getfloat("capture", "gain"), source=source,
                           listeners=[self.detector] if self.trigger == "change" else [])
        self.busy = threading.Lock()
        self.retrying = threading.Lock()
        self.draw_lock = threading.Lock()
        self.running = True
        self.overlay = None          # (image builder, until)
        self.matching = False
        self.last_id_t = 0.0
        self.last_auto_t = 0.0
        self.auto_times = []         # recent auto ID times, for the hourly cap
        self.music_since = None      # when the current stretch of music began (for the first auto ID)
        self.recheck_at = None       # auto: look again after a blend / a miss
        self.rechecks = 0
        self.next_auto = time.time() + 30
        self.hue = 160
        self.beat = None
        self.flash_t = 0.0
        self.press_t = None
        self.taps = []
        self.tap_timer = None
        self.ip = ""
        self.ip_t = 0.0

    # ---- helpers ---------------------------------------------------------------------------------
    def clock(self):
        """Where BPM and beats come from: the Beat Commander (via Beat Hub) when it's live, else the mic."""
        return self.feed if self.feed and self.feed.locked else self.engine

    def bpm_source(self):
        return "conductor" if self.feed and self.feed.locked else "mic"

    def led(self, r, g, b):
        if not self.use_led:
            return
        try:
            self.board.set_rgb(int(r), int(g), int(b))
        except Exception:
            pass

    def show_card(self, builder, secs):
        self.overlay = (builder, time.time() + secs)

    def footer(self):
        now = time.time()
        if now - self.ip_t > 10:
            self.ip, self.ip_t = local_ip(), now
        if not self.cap.ok:
            return "mic: " + (self.cap.error[:28] if self.cap.error else "starting")
        if not self.ip:
            return "offline · IDs queue"
        return f"{self.ip}:{self.web_port}" if self.web_port else self.ip

    def state_json(self):
        tr = self.store.tracks()
        c = self.clock()
        st = {"version": VERSION, "auto": self.auto, "trigger": self.trigger,
              "bpm": round(c.bpm, 1) if c.locked else None, "bpm_source": self.bpm_source(),
              "level": round(self.engine.level, 4), "set": self.store.set_id, "count": len(tr),
              "unknown": len(self.store.unknowns()),
              "last": ({k: tr[-1].get(k) for k in ("artist", "title", "bpm", "clock", "source")} if tr else None),
              "pending": len(self.store.pending()), "matching": self.matching,
              "library": self.lib.count() if self.lib else None,
              "auto_ids_last_hour": len([t for t in self.auto_times if time.time() - t < 3600])}
        if self.trigger == "change":
            st["change"] = self.detector.state()
        return st

    # ---- button gestures -------------------------------------------------------------------------
    def on_press(self):
        self.press_t = time.time()

    def on_release(self):
        t0, self.press_t = self.press_t, None
        if t0 is None:
            return
        now = time.time()
        if now - t0 >= LONG_PRESS_S:
            self.taps = []
            threading.Thread(target=self.new_set, daemon=True).start()
            return
        self.taps = [t for t in self.taps if now - t < DOUBLE_TAP_S] + [now]
        if len(self.taps) >= 2:
            self.taps = []
            if self.tap_timer:
                self.tap_timer.cancel()
            self.toggle_auto()
            return
        self.tap_timer = threading.Timer(DOUBLE_TAP_S, self._single_tap)
        self.tap_timer.daemon = True
        self.tap_timer.start()

    def _single_tap(self):
        if self.taps:
            self.taps = []
            threading.Thread(target=self.identify, kwargs={"manual": True}, daemon=True).start()

    def toggle_auto(self):
        self.auto = not self.auto
        self.next_auto = time.time() + 20
        self.music_since = None
        self.recheck_at = None
        log.info("AUTO %s", "on" if self.auto else "off")
        acc = GREEN if self.auto else PURPLE
        if not self.auto:
            lines = ["Tap to identify. Double tap to turn AUTO back on."]
        elif self.trigger == "change":
            lines = ["Identifies each new track shortly after it starts, "
                     f"and at least every {self.max_interval // 60} min while music plays.", "Double tap again to stop."]
        else:
            lines = [f"Identifies the last {self.seconds} s every {self.interval // 60} min "
                     f"{self.interval % 60:02d} s while music is playing.", "Double tap again to stop."]
        self.show_card(lambda: self.scr.message(acc, "MODE", "AUTO ON" if self.auto else "AUTO OFF", lines), 2.5)

    def new_set(self):
        sid = self.store.new_set()
        log.info("new set %s", sid)
        self.led(120, 90, 255)
        start = datetime.fromtimestamp(self.store.started()).strftime("%a %b %d · %H:%M")
        self.show_card(lambda: self.scr.message(PURPLE, "NEW SET", "New set started",
                                                [start, "Tracks from here go into a fresh list."]), 2.5)

    # ---- recognition -------------------------------------------------------------------------------
    def recognize(self, x, wav):
        """Your own library first (offline, free), then AudD. Raises NetworkError / AuddError from AudD."""
        if self.lib is not None:
            try:
                t0 = time.time()
                r = self.lib.match(x)
                if r:
                    log.info("library match in %.2f s (score %s, speed %s)", time.time() - t0, r["score"], r["speed"])
                    return r
            except Exception:
                log.exception("library match failed")
        return audd_identify(wav, self.token, self.market)

    def _save_clip(self, wav, ts):
        clip = os.path.join(self.store.clip_dir, datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S_%f") + ".wav")
        with open(clip, "wb") as fh:
            fh.write(wav)
        return clip

    # ---- identify ----------------------------------------------------------------------------------
    def identify(self, manual=False, reason="tap"):
        if not self.busy.acquire(blocking=False):
            return
        try:
            self._identify(manual, reason)
        except Exception as exc:
            log.exception("identify failed")
            msg = str(exc)[:140]                  # `exc` is gone once the except block ends; the card renders later
            self.show_card(lambda: self.scr.message(RED, "ERROR", "Something broke", [msg], self.footer()), 5)
        finally:
            self.matching = False
            self.busy.release()

    def _identify(self, manual, reason="tap"):
        x, t_clip = self.cap.last(self.seconds)
        if x is None:
            have = self.cap.buffered_s()
            if manual:
                self.show_card(lambda: self.scr.message(AMBER, "WAIT", "Still listening",
                                                        [f"Need {self.seconds} s of audio, have {have:.0f} s.",
                                                         "Try again in a moment."]), 2.5)
            return
        if manual and float(np.sqrt(np.mean(x * x))) < 0.0005:
            self.show_card(lambda: self.scr.message(AMBER, "QUIET", "Nothing to hear",
                                                    ["The last few seconds were silent.",
                                                     "Point the mic at the speaker."], self.footer()), 3)
            return
        self.matching = True
        self.last_id_t = time.time()
        if not manual:
            self.last_auto_t = self.last_id_t
            self.auto_times = [t for t in self.auto_times if self.last_id_t - t < 3600] + [self.last_id_t]
        self.led(255, 140, 0)
        wav = clip_wav(x)
        ts = t_clip
        sid = self.store.set_id
        entry = {"ts": ts, "clock": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                 "bpm": round(self.clock().bpm, 1) if self.clock().locked else None,
                 "via": "tap" if manual else "auto"}
        if not manual:
            entry["trigger"] = reason
        try:
            result = self.recognize(x, wav)
        except NetworkError as exc:
            self._queue(entry, wav, sid, manual, "No internet right now.")
            log.info("offline, queued (%s)", exc)
            return
        except AuddError as exc:
            log.warning("%s", exc)
            if exc.code in (901, 902):
                self._queue(entry, wav, sid, manual, "AudD limit reached.")
                return
            if exc.code == 900:
                self.show_card(lambda: self.scr.message(RED, "ERROR", "Bad AudD token",
                                                        ["Check [audd] token in config.ini, then:",
                                                         "sudo systemctl restart trackid"], self.footer()), 6)
                return
            result = None
        self._finish(result, entry, sid, manual, wav, reason)

    def _queue(self, entry, wav, sid, manual, why):
        entry.update(status="pending", clip=self._save_clip(wav, entry["ts"]))
        self.store.add(entry, sid)
        self.led(0, 80, 255)
        if manual:
            n = len(self.store.pending())
            self.show_card(lambda: self.scr.message(BLUE, "SAVED", "Saved for later",
                                                    [why, f"{n} clip(s) will be identified automatically "
                                                          "when the Pi is back online."], self.footer()), 3.5)

    def _apply_result(self, entry, result):
        entry.update(result)
        tc = parse_timecode(result.get("timecode"))
        if tc is not None and 0 <= tc <= 900:
            entry["est_start"] = entry["ts"] - tc                 # where the song actually started
        entry.pop("timecode", None)

    def _recheck(self, after):
        """AUTO: the change was real but this ID didn't land on the new track (blend, miss): look again soon."""
        if self.trigger == "change" and self.rechecks < 2:
            self.rechecks += 1
            self.recheck_at = time.time() + after

    def _finish(self, result, entry, sid, manual, wav=None, reason="tap"):
        if result is None:
            if not manual and reason == "change":
                self._recheck(60)
            last = self.store.last_logged(sid)
            if not manual and reason != "change" and last is not None and last.get("status") == "nomatch":
                return                                            # same unknown track still playing: log it once
            entry["status"] = "nomatch"
            if wav:
                entry["clip"] = self._save_clip(wav, entry["ts"])  # kept so you can listen / re-identify later
            self.store.add(entry, sid)
            log.info("no match (%s), logged as unknown", entry["via"])
            if manual:
                self.led(200, 0, 160)
                self.show_card(lambda: self.scr.message(PINK, "NO MATCH", "Unknown track",
                                                        ["Logged as ID - ID. Listen and name it later on the web page.",
                                                         "Or get closer to the speaker and tap again."],
                                                        self.footer()), 3.5)
            return
        dup = self.store.same_as_last(result, sid)
        if not manual and (dup or self.store.seen_in_set(result, sid)):
            if dup and reason in ("change", "recheck"):
                self._recheck(45)                                 # mid-blend: the old track still won
            return                                                # auto: still the same track, stay quiet
        self.rechecks = 0
        self._apply_result(entry, result)
        entry["status"] = "duplicate" if dup else "matched"
        self.store.add(entry, sid)
        log.info("%s (%s/%s via %s): %s - %s", entry["status"], entry["via"], reason, entry.get("source"),
                 result["artist"], result["title"])
        n = len(self.store.tracks(sid))
        rows = self.store.timeline(sid)
        off = next((fmt_hms(o, True) for o, e in rows if eid(e) == entry["id"]), "--:--")
        e = dict(entry, offset=off)
        if e.get("source") == "local" and not e.get("label"):
            sp = e.get("speed") or 1.0
            e["label"] = "Your library" + (f" · {sp * 100 - 100:+.0f}%{' key lock' if e.get('keylock') else ''}"
                                           if abs(sp - 1.0) > 0.005 else "")
        self.led(0, 255, 90)
        self.show_card(lambda: self.scr.result(e, n, dup, time.time()), 6)

    def reidentify(self, sid, entry_id):
        """Web editor: run an unknown / queued entry's saved clip through the library and AudD again."""
        e = self.store.get(sid, entry_id)
        if not e or not e.get("clip") or not os.path.exists(e["clip"]):
            return {"ok": False, "error": "no saved clip for this entry"}
        wav = open(e["clip"], "rb").read()
        try:
            result = self.recognize(wav_to_float(wav), wav)
        except (NetworkError, AuddError) as exc:
            return {"ok": False, "error": str(exc)}
        if result is None:
            return {"ok": True, "match": False}
        ch = dict(e)
        self._apply_result(ch, result)
        ch["status"] = "matched"
        if not self.keep_clips:
            try:
                os.remove(e["clip"])
            except OSError:
                pass
            ch["clip"] = None
        self.store.update(sid, entry_id, ch)
        return {"ok": True, "match": True, "artist": result["artist"], "title": result["title"]}

    # ---- background ---------------------------------------------------------------------------------
    def retry_pending(self):
        items = self.store.pending()
        if not items or not have_internet():
            return
        for sid, e in items:
            clip = e.get("clip", "")
            if not os.path.exists(clip):
                self.store.update(sid, eid(e), {"status": "nomatch", "note": "clip missing", "clip": None})
                continue
            wav = open(clip, "rb").read()
            try:
                result = self.recognize(wav_to_float(wav), wav)
            except (NetworkError, AuddError) as exc:
                log.info("retry deferred: %s", exc)
                return
            if result is None:
                self.store.update(sid, eid(e), {"status": "nomatch"})   # clip stays: listen / re-ID from the editor
                continue
            ch = dict(e)
            self._apply_result(ch, result)
            ch["status"] = "duplicate" if self.store.same_as_last(result, sid, before=e["ts"]) else "matched"
            ch["clip"] = None
            self.store.update(sid, eid(e), ch)
            log.info("late match: %s - %s", result["artist"], result["title"])
            if not self.keep_clips:
                try:
                    os.remove(clip)
                except OSError:
                    pass

    def _retry_worker(self):
        if not self.retrying.acquire(blocking=False):
            return
        try:
            self.retry_pending()
        except Exception:
            log.exception("retry failed")
        finally:
            self.retrying.release()

    def _scan_library(self):
        """Index new files dropped into the library folder, in a low-priority child process."""
        if not self.lib or not self.lib_folder or not os.path.isdir(self.lib_folder):
            return
        if self.lib_proc and self.lib_proc.poll() is None:
            return
        cmd = ["nice", "-n", "19", sys.executable, os.path.join(APP_DIR, "localid.py"), "--db", self.lib.path,
               "add", self.lib_folder]
        try:
            self.lib_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning("library scan failed to start: %s", exc)

    def auto_due(self, now):
        """AUTO decision. Returns the reason to identify now, or None."""
        playing = self.engine.level >= self.min_level
        if not playing:
            self.music_since = None
            return None
        if self.music_since is None:
            self.music_since = now
        if len([t for t in self.auto_times if now - t < 3600]) >= self.max_per_hour:
            return None                                           # hourly budget spent
        if self.trigger == "interval":
            if now >= self.next_auto:
                self.next_auto = now + self.interval
                return "interval"
            return None
        since = now - self.last_id_t
        ct = self.detector.change_t
        if ct is not None and now - ct >= self.seconds + 2:     # the clip now lies after the change
            if since >= self.min_gap:
                self.detector.consume()
                self.rechecks = 0
                self.recheck_at = None
                return "change"
            if since < 5:                                         # a tap just covered it
                self.detector.consume()
        if self.recheck_at and now >= self.recheck_at and since >= 30:
            self.recheck_at = None
            return "recheck"
        if self.last_auto_t == 0 and self.last_id_t == 0 and now - self.music_since >= 30:
            return "start"                                        # first track of the session
        if self.last_id_t and since >= self.max_interval and now - self.music_since >= 20:
            return "fallback"
        return None

    def background(self):
        next_retry = time.time() + 15
        next_scan = time.time() + 5
        while self.running:
            now = time.time()
            if now >= next_retry:
                next_retry = now + RETRY_EVERY_S
                threading.Thread(target=self._retry_worker, daemon=True).start()
            if self.lib and now >= next_scan:
                next_scan = now + (self.lib_rescan or 1e12)
                self._scan_library()
            if self.auto and not self.busy.locked():
                why = self.auto_due(now)
                if why:
                    log.info("auto ID (%s)", why)
                    threading.Thread(target=self.identify, kwargs={"reason": why}, daemon=True).start()
            elif not self.auto and self.trigger == "change" and self.detector.change_t is not None:
                self.detector.consume()                            # manual mode: keep the detector current
            time.sleep(0.5)

    # ---- render loop ---------------------------------------------------------------------------------
    def frame(self, now):
        if self.overlay and now < self.overlay[1]:
            return self.overlay[0]()
        self.overlay = None
        tr = self.store.tracks()
        flash = max(0.0, 1.0 - (now - self.flash_t) * 2.5)
        sc = list(zip(np.roll(self.engine.scope, -self.engine.scope_pos).tolist(),
                      np.roll(self.engine.scope_on, -self.engine.scope_pos).tolist()))
        playing = self.engine.level >= self.min_level
        watch = None
        next_in = None
        if self.auto and playing:
            if self.trigger == "change":
                ct = self.detector.change_t
                watch = {"nov": self.detector.novelty, "thr": self.detector.threshold,
                         "id_in": max(0.0, ct + self.seconds + 2 - now) if ct is not None else None}
            else:
                next_in = max(0.0, self.next_auto - now)
        st = self.store.started()
        c = self.clock()
        s = {"auto": self.auto, "set_label": datetime.fromtimestamp(st).strftime("%H:%M") if st else "", "count": len(tr),
             "bpm": c.bpm, "locked": c.locked, "src": self.bpm_source(), "beat": self.beat, "flash": flash,
             "hue": self.hue, "level": self.engine.level, "scope": sc, "last": tr[-1] if tr else None,
             "next_in": next_in, "watch": watch, "interval": self.interval, "matching": self.matching,
             "pending": len(self.store.pending()), "footer": self.footer()}
        return self.scr.idle(s, now)

    def run(self):
        try:
            self.board.set_backlight(self.cfg.getint("display", "brightness"))
        except Exception:
            pass
        self.board.on_button_press(self.on_press)
        self.board.on_button_release(self.on_release)
        self._push(self.scr.splash(VERSION))
        self.cap.start()
        if self.feed:
            self.feed.start()
        self.start_web()
        if not self.token:
            log.warning("no AudD token set; anonymous requests are very limited")
        threading.Thread(target=self.background, daemon=True).start()
        period = 1.0 / self.fps
        led_off = 0.0
        while self.running:
            t0 = time.time()
            c = self.clock()
            b = c.tick(t0 * 1000.0) if c.locked else None
            if b is not None:
                self.beat = b
                self.flash_t = t0
                if b == 0:
                    self.hue = (self.hue + 40) % 256
                if self.led_beat and not self.matching:
                    r, g, bb = hue_rgb(self.hue)
                    k = 0.5 if b == 0 else 0.15
                    self.led(r * k, g * k, bb * k)
                    led_off = t0 + 0.12
            if led_off and t0 >= led_off and not self.matching:
                self.led(0, 0, 0)
                led_off = 0.0
            self._push(self.frame(t0))
            time.sleep(max(0.0, period - (time.time() - t0)))

    def _push(self, img):
        with self.draw_lock:
            self.board.draw_image(0, 0, W, H, to_rgb565(img))

    def start_web(self):
        if not self.web_port:
            return
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", self.web_port), make_handler(self))
        except OSError as exc:
            log.warning("web view disabled: %s", exc)
            self.web_port = 0
            return
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("web view on port %d", self.web_port)

    def stop(self, *_):
        self.running = False
        self.cap.running = False


# ================================================================== entry points

def load_board():
    runtime = os.environ.get("WHISPLAY_RUNTIME", os.path.expanduser("~/Whisplay/runtime"))
    if runtime not in sys.path:
        sys.path.append(runtime)
    from whisplay_client import create_whisplay_hardware  # PiSugar driver (daemon-aware)
    return create_whisplay_hardware(app_id="trackid", display_name="TrackID", icon="T",
                                    launch_command=f"{sys.executable} {os.path.abspath(__file__)}",
                                    launch_cwd=APP_DIR, use_daemon_default_log=True)


def render_preview(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    s = Screens()
    rng = np.random.default_rng(3)
    scope = [(float(v), bool(on)) for v, on in zip(
        np.where(np.arange(208) % 29 < 2, 0.85, rng.random(208) * 0.12), np.arange(208) % 29 == 0)]
    last = {"artist": "Fred again.., Skrillex & Four Tet", "title": "Baby again.. (Extended Club Mix that keeps going)",
            "album": "Baby again..", "release_date": "2023-02-23", "label": "Atlantic Records UK",
            "bpm": 126.0, "offset": "00:41:07"}
    base = {"auto": True, "set_label": "21:04", "count": 7, "bpm": 126.4, "locked": True, "beat": 0, "flash": 0.9, "hue": 200,
            "level": 0.11, "scope": scope, "last": last, "next_in": 72, "interval": 150, "matching": False,
            "pending": 0, "footer": "192.168.1.42:8080"}
    frames = {
        "0_splash": s.splash(VERSION),
        "1_auto_downbeat": s.idle(base, 0.0),
        "2_auto_offbeat": s.idle(dict(base, beat=2, flash=0.1), 2.6),
        "3_manual_matching": s.idle(dict(base, auto=False, matching=True, beat=1, flash=0.3), 1.0),
        "2b_conductor_link": s.idle(dict(base, src="conductor", beat=1, flash=0.2), 1.3),
        "4_manual_waiting": s.idle(dict(base, auto=False, locked=False, level=0.002, last=None, count=0,
                                        scope=[(0.0, False)] * 208, pending=2, footer="offline · IDs queue"), 0),
        "1b_auto_watching": s.idle(dict(base, next_in=None, watch={"nov": 0.22, "thr": 0.4, "id_in": None}), 0.4),
        "1c_auto_new_track": s.idle(dict(base, next_in=None, watch={"nov": 0.61, "thr": 0.4, "id_in": 7.2}), 0.8),
        "5_result": s.result(last, 8, False, 0),
        "5b_result_library": s.result({"artist": "DJ Anubis", "title": "Darkwatch (Unreleased VIP Edit)", "bpm": 128.0,
                                       "label": "Your library · +3%", "offset": "01:12:44"}, 9, False, 0),
        "6_duplicate": s.result(last, 8, True, 0),
        "7_auto_on": s.message(GREEN, "MODE", "AUTO ON", ["Identifies the last 10 s every 2 min 30 s while music is playing.",
                                                          "Double tap again to stop."]),
        "8_no_match": s.message(PINK, "NO MATCH", "Unknown track", ["Logged as ID - ID. Listen and name it later on the web page.",
                                                                   "Or get closer to the speaker and tap again."],
                                "192.168.1.42:8080"),
        "9_queued": s.message(BLUE, "SAVED", "Saved for later", ["No internet right now.",
                                                                "3 clip(s) will be identified automatically when the Pi is back online."],
                              "offline · IDs queue"),
    }
    for name, img in frames.items():
        img.save(os.path.join(out_dir, name + ".png"))
        to_rgb565(img)
    print(f"wrote {len(frames)} frames to {out_dir}")


def main():
    ap = argparse.ArgumentParser(description="TrackID v2 - Whisplay setlist logger")
    ap.add_argument("--config", default=os.path.join(APP_DIR, "config.ini"))
    ap.add_argument("--preview", metavar="DIR", help="render all screens to PNGs and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.preview:
        render_preview(args.preview)
        return
    cfg = load_config(args.config)
    board = load_board()
    app = App(board, cfg)
    signal.signal(signal.SIGTERM, app.stop)
    signal.signal(signal.SIGINT, app.stop)
    log.info("TrackID %s up, capture devices %s", VERSION, app.cap.devices)
    try:
        app.run()
    finally:
        try:
            board.set_rgb(0, 0, 0)
            board.fill_screen(0)
            board.set_backlight(0)
        except Exception:
            pass
        if hasattr(board, "cleanup"):
            try:
                board.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    main()
