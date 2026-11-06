#!/usr/bin/env python3
"""TrackID v2 - always-listening track ID + setlist logger for a Pi Zero 2W with the PiSugar Whisplay HAT.

The mic is always recording into a 20 s ring buffer, so an ID uses the last 10 s instantly.
Live BPM comes from the W10 Beat Conductor's beat engine (beat.py) running on the same audio.

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
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

try:
    import requests
except ImportError:
    requests = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
from beat import BeatEngine, RATE  # noqa: E402
from screens import Screens, W, H, to_rgb565, hue_rgb, GREEN, PURPLE, BLUE, RED, PINK, AMBER, CYAN  # noqa: E402

VERSION = "2.0"
RING_S = 20                 # seconds of audio kept
DOUBLE_TAP_S = 0.35
LONG_PRESS_S = 1.5
RETRY_EVERY_S = 45
log = logging.getLogger("trackid")

DEFAULT_CONFIG = {
    "audd": {"token": "", "market": "us"},
    "capture": {"seconds": "10", "device": "auto", "gain": "1.0"},
    "auto": {"enabled": "false", "interval": "150", "min_level": "0.01"},
    "display": {"brightness": "70", "led": "true", "led_beat": "true", "fps": "12"},
    "web": {"enabled": "true", "port": "8080"},
    "storage": {"data_dir": "data", "keep_clips": "false"},
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

class SetStore:
    """One JSONL file per set. Exports are regenerated on every change."""

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "sets")
        self.clip_dir = os.path.join(data_dir, "clips")
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(self.clip_dir, exist_ok=True)
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

    def same_as_last(self, r, sid=None, before=None):
        tr = [t for t in self.tracks(sid) if before is None or t["ts"] < before]
        if not tr:
            return False
        a = tr[-1]
        return (a.get("artist", "").lower(), a.get("title", "").lower()) == (r.get("artist", "").lower(), r.get("title", "").lower())

    def seen_in_set(self, r, sid=None):
        key = (r.get("artist", "").lower(), r.get("title", "").lower())
        return any((t.get("artist", "").lower(), t.get("title", "").lower()) == key for t in self.tracks(sid))

    def add(self, e, sid=None):
        with self.lock:
            sid = sid or self.set_id
            es = self.entries(sid)
            es.append(e)
            self._write(sid, es)

    def update(self, sid, ts, changes):
        with self.lock:
            es = self.entries(sid)
            for e in es:
                if e.get("ts") == ts:
                    e.update(changes)
            self._write(sid, es)

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
    def timeline(self, sid=None):
        """Tracks with offsets from the set start. Uses AudD's position-in-song to estimate when each
        track actually started; offsets never go backwards."""
        sid = sid or self.set_id
        start = self.started(sid)
        rows, last = [], 0
        for e in self.tracks(sid):
            t = e.get("est_start", e["ts"])
            off = max(0, int(t - start)) if start else 0
            off = max(off, last)
            last = off
            rows.append((off, e))
        return rows

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
                lines.append(f"{fmt_hms(off, long_set)} {e.get('artist', '?')} - {e.get('title', '?')}")
            return "\n".join(lines) + "\n"
        if fmt == "mixcloud":
            return "".join(f"{fmt_hms(off, True)}  {e.get('artist', '?')} – {e.get('title', '?')}\n" for off, e in rows) \
                or "(no tracks yet)\n"
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["n", "start", "logged", "artist", "title", "album", "year", "label", "bpm", "link"])
            for i, (off, e) in enumerate(rows, 1):
                w.writerow([i, fmt_hms(off, True), e.get("clock", ""), e.get("artist", ""), e.get("title", ""),
                            e.get("album", ""), (e.get("release_date") or "")[:4], e.get("label", ""),
                            f"{e['bpm']:.0f}" if e.get("bpm") else "", e.get("link", "")])
            return buf.getvalue()
        start = self.started(sid)
        head = f"Tracklist - {datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M') if start else sid}\n\n"
        body = "".join(f"{i:02d}. [{fmt_hms(off, True)}] {e.get('artist', '?')} - {e.get('title', '?')}"
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

    def __init__(self, devices, engine, gain=1.0, source=None):
        super().__init__(daemon=True)
        self.devices = devices
        self.engine = engine
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


def make_handler(app):
    store = app.store

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            b = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/state":
                self._send(200, json.dumps(app.state_json()), "application/json",
                           {"Access-Control-Allow-Origin": "*"})
                return
            if u.path in ("/", "/set"):
                sets = store.list_sets()
                sid = (q.get("id") or [store.set_id])[0]
                if sid not in sets:
                    sid = store.set_id
                fmt = (q.get("fmt") or ["plain"])[0]
                if fmt not in [f[0] for f in FORMATS]:
                    fmt = "plain"
                tabs = "".join(f'<a class="{"on" if f == fmt else ""}" href="/?id={sid}&fmt={f}">{html.escape(n)}</a>'
                               for f, n, _ in FORMATS)
                note = ""
                if fmt == "youtube":
                    note = ('<p class="note">Paste into the video description. YouTube needs the first line at 00:00, '
                            'at least 3 chapters, each 10 s or longer. Start times are estimated from where in each song the clip landed.</p>')
                elif fmt == "mixcloud":
                    note = '<p class="note">Start times for Mixcloud\'s tracklist editor (Upload → Tracklist → add each track with its start time).</p>'
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

    return Handler


# ================================================================== app

class App:
    def __init__(self, board, cfg, source=None):
        self.board = board
        self.cfg = cfg
        data_dir = cfg.get("storage", "data_dir")
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(APP_DIR, data_dir)
        self.store = SetStore(data_dir)
        self.scr = Screens()
        self.token = cfg.get("audd", "token").strip()
        self.market = cfg.get("audd", "market").strip() or "us"
        self.seconds = max(5, min(15, cfg.getint("capture", "seconds")))
        self.auto = cfg.getboolean("auto", "enabled")
        self.interval = max(45, cfg.getint("auto", "interval"))
        self.min_level = cfg.getfloat("auto", "min_level")
        self.use_led = cfg.getboolean("display", "led")
        self.led_beat = cfg.getboolean("display", "led_beat")
        self.fps = max(4, min(20, cfg.getint("display", "fps")))
        self.keep_clips = cfg.getboolean("storage", "keep_clips")
        self.web_port = cfg.getint("web", "port") if cfg.getboolean("web", "enabled") else 0
        self.engine = BeatEngine()
        devs = find_capture_devices() if cfg.get("capture", "device") == "auto" else [cfg.get("capture", "device")]
        self.cap = Capture(devs, self.engine, cfg.getfloat("capture", "gain"), source=source)
        self.busy = threading.Lock()
        self.retrying = threading.Lock()
        self.draw_lock = threading.Lock()
        self.running = True
        self.overlay = None          # (image builder, until)
        self.matching = False
        self.last_id_t = 0.0
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
        return {"version": VERSION, "auto": self.auto, "bpm": round(self.engine.bpm, 1) if self.engine.locked else None,
                "level": round(self.engine.level, 4), "set": self.store.set_id, "count": len(tr),
                "last": ({k: tr[-1].get(k) for k in ("artist", "title", "bpm", "clock")} if tr else None),
                "pending": len(self.store.pending()), "matching": self.matching}

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
        log.info("AUTO %s", "on" if self.auto else "off")
        acc = GREEN if self.auto else PURPLE
        lines = ([f"Identifies the last {self.seconds} s every {self.interval // 60} min "
                  f"{self.interval % 60:02d} s while music is playing.", "Double tap again to stop."]
                 if self.auto else ["Tap to identify. Double tap to turn AUTO back on."])
        self.show_card(lambda: self.scr.message(acc, "MODE", "AUTO ON" if self.auto else "AUTO OFF", lines), 2.5)

    def new_set(self):
        sid = self.store.new_set()
        log.info("new set %s", sid)
        self.led(120, 90, 255)
        start = datetime.fromtimestamp(self.store.started()).strftime("%a %b %d · %H:%M")
        self.show_card(lambda: self.scr.message(PURPLE, "NEW SET", "New set started",
                                                [start, "Tracks from here go into a fresh list."]), 2.5)

    # ---- identify ----------------------------------------------------------------------------------
    def identify(self, manual=False):
        if not self.busy.acquire(blocking=False):
            return
        try:
            self._identify(manual)
        except Exception as exc:
            log.exception("identify failed")
            self.show_card(lambda: self.scr.message(RED, "ERROR", "Something broke", [str(exc)[:140]], self.footer()), 5)
        finally:
            self.matching = False
            self.busy.release()

    def _identify(self, manual):
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
        self.led(255, 140, 0)
        wav = clip_wav(x)
        ts = t_clip
        sid = self.store.set_id
        entry = {"ts": ts, "clock": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                 "bpm": round(self.engine.bpm, 1) if self.engine.locked else None,
                 "via": "tap" if manual else "auto"}
        try:
            result = audd_identify(wav, self.token, self.market)
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
        self._finish(result, entry, sid, manual)

    def _queue(self, entry, wav, sid, manual, why):
        clip = os.path.join(self.store.clip_dir, datetime.fromtimestamp(entry["ts"]).strftime("%Y%m%d_%H%M%S_%f") + ".wav")
        with open(clip, "wb") as fh:
            fh.write(wav)
        entry.update(status="pending", clip=clip)
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

    def _finish(self, result, entry, sid, manual):
        if result is None:
            if manual:
                entry["status"] = "nomatch"
                self.store.add(entry, sid)
                self.led(200, 0, 160)
                self.show_card(lambda: self.scr.message(PINK, "NO MATCH", "No match",
                                                        ["Get closer to the speaker and try again.",
                                                         "Unreleased edits and live blends often won't match."],
                                                        self.footer()), 3.5)
            return                                                # auto misses aren't logged: no noise
        dup = self.store.same_as_last(result, sid)
        if not manual and (dup or self.store.seen_in_set(result, sid)):
            return                                                # auto: still the same track, stay quiet
        self._apply_result(entry, result)
        entry["status"] = "duplicate" if dup else "matched"
        self.store.add(entry, sid)
        log.info("%s (%s): %s - %s", entry["status"], entry["via"], result["artist"], result["title"])
        n = len(self.store.tracks(sid))
        rows = self.store.timeline(sid)
        off = next((fmt_hms(o, True) for o, e in rows if e.get("ts") == entry["ts"]), "--:--")
        e = dict(entry, offset=off)
        self.led(0, 255, 90)
        self.show_card(lambda: self.scr.result(e, n, dup, time.time()), 6)

    # ---- background ---------------------------------------------------------------------------------
    def retry_pending(self):
        items = self.store.pending()
        if not items or not have_internet():
            return
        for sid, e in items:
            clip = e.get("clip", "")
            if not os.path.exists(clip):
                self.store.update(sid, e["ts"], {"status": "nomatch", "note": "clip missing"})
                continue
            try:
                result = audd_identify(open(clip, "rb").read(), self.token, self.market)
            except (NetworkError, AuddError) as exc:
                log.info("retry deferred: %s", exc)
                return
            if result is None:
                self.store.update(sid, e["ts"], {"status": "nomatch"})
            else:
                ch = dict(e)
                self._apply_result(ch, result)
                ch["status"] = "duplicate" if self.store.same_as_last(result, sid, before=e["ts"]) else "matched"
                ch.pop("clip", None)
                self.store.update(sid, e["ts"], ch)
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

    def background(self):
        next_retry = time.time() + 15
        while self.running:
            now = time.time()
            if now >= next_retry:
                next_retry = now + RETRY_EVERY_S
                threading.Thread(target=self._retry_worker, daemon=True).start()
            if self.auto and now >= self.next_auto and not self.busy.locked():
                if self.engine.level >= self.min_level:
                    self.next_auto = now + self.interval
                    threading.Thread(target=self.identify, daemon=True).start()
                else:
                    self.next_auto = now + 5                      # quiet: check again shortly
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
        next_in = max(0.0, self.next_auto - now) if self.auto and self.engine.level >= self.min_level else None
        st = self.store.started()
        s = {"auto": self.auto, "set_label": datetime.fromtimestamp(st).strftime("%H:%M") if st else "", "count": len(tr),
             "bpm": self.engine.bpm, "locked": self.engine.locked, "beat": self.beat, "flash": flash,
             "hue": self.hue, "level": self.engine.level, "scope": sc, "last": tr[-1] if tr else None,
             "next_in": next_in, "interval": self.interval, "matching": self.matching,
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
        self.start_web()
        if not self.token:
            log.warning("no AudD token set; anonymous requests are very limited")
        threading.Thread(target=self.background, daemon=True).start()
        period = 1.0 / self.fps
        led_off = 0.0
        while self.running:
            t0 = time.time()
            b = self.engine.tick(t0 * 1000.0) if self.engine.locked else None
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
        "4_manual_waiting": s.idle(dict(base, auto=False, locked=False, level=0.002, last=None, count=0,
                                        scope=[(0.0, False)] * 208, pending=2, footer="offline · IDs queue"), 0),
        "5_result": s.result(last, 8, False, 0),
        "6_duplicate": s.result(last, 8, True, 0),
        "7_auto_on": s.message(GREEN, "MODE", "AUTO ON", ["Identifies the last 10 s every 2 min 30 s while music is playing.",
                                                          "Double tap again to stop."]),
        "8_no_match": s.message(PINK, "NO MATCH", "No match", ["Get closer to the speaker and try again.",
                                                              "Unreleased edits and live blends often won't match."],
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
