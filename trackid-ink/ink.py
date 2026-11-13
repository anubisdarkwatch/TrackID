#!/usr/bin/env python3
"""TrackID Ink - a now-playing sign for TrackID on a 2.13" e-paper (250x122, black/white):
the Waveshare 2.13" e-Paper HAT (V2, V3 or V4 panel), the Waveshare 2.13" Touch e-Paper HAT, or the
Adafruit 2.13" Monochrome E-Ink Bonnet (#4687).

With the Touch e-Paper HAT the sign is also a remote for TrackID:
  tap = identify now, hold 1 s = AUTO on/off, swipe = Now Playing <-> recent tracks.

Runs on its own Pi (a Zero W / WH or Zero 2W: the e-paper HAT and the Whisplay can't share one Pi).
Every couple of seconds it asks TrackID (http://trackid.local:8080/api/state) what's playing and redraws when
something changes: the track and artist, the BPM, AUTO/MANUAL, the track count, and the previous track.

No beat flashing: e-paper takes ~0.3 s for a partial update. Partial updates for changes, a full clean refresh
every so often to clear ghosting, and the panel sleeps after a quiet minute.

Preview without hardware:  python3 ink.py --preview out/
"""
from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.1"
W, H = 250, 122
log = logging.getLogger("trackid-ink")

DEFAULTS = {
    "trackid": {"url": "http://trackid.local:8080", "poll": "2"},
    "display": {"panel": "waveshare", "waveshare_model": "V4", "adafruit_driver": "ssd1680b", "bpm_step": "1", "bpm_min_gap": "6",
                "full_refresh_every": "30", "full_refresh_minutes": "15", "flip": "false"},
    "touch": {"enabled": "false", "hold_s": "1.0", "list_timeout": "30"},
}

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"]


def font(size, bold=True):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


F_TAG = font(11)
F_TITLE = font(19)
F_TITLE_S = font(15)
F_ARTIST = font(13, bold=False)
F_BPM = font(30)
F_BPMU = font(10)
F_SMALL = font(10, bold=False)


def tw(d, s, f):
    l, _, r, _ = d.textbbox((0, 0), s, font=f)
    return r - l


def fit(d, s, f, max_w):
    if tw(d, s, f) <= max_w:
        return s
    while s and tw(d, s + "…", f) > max_w:
        s = s[:-1]
    return s.rstrip() + "…"


def wrap(d, s, f, max_w, max_lines):
    words, lines, cur = (s or "").split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if tw(d, t, f) <= max_w:
            cur = t
            continue
        if cur:
            lines.append(cur)
        cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = fit(d, lines[-1] + " …", f, max_w)
    return [fit(d, ln, f, max_w) for ln in lines] or [""]


# ================================================================== layout

def render(st, online, prev=None, toast=None):
    """st: TrackID /api/state dict (or None). Returns a 250x122 1-bit image (black on white)."""
    img = Image.new("1", (W, H), 255)
    d = ImageDraw.Draw(img)
    BLACK, WHITE = 0, 255

    # header bar
    d.rectangle((0, 0, W, 17), fill=BLACK)
    d.text((5, 2), "NOW PLAYING", font=F_TAG, fill=WHITE)
    if not online or not st:
        tag = "OFFLINE"
    elif st.get("matching"):
        tag = "MATCHING…"
    else:
        tag = "AUTO" if st.get("auto") else "MANUAL"
    count = f"#{st.get('count', 0)}" if st else ""
    right = f"{tag}  {count}".strip()
    d.text((W - 5 - tw(d, right, F_TAG), 2), right, font=F_TAG, fill=WHITE)

    # BPM block (right)
    bx = W - 66
    d.line((bx - 6, 24, bx - 6, 92), fill=BLACK)
    bpm = st.get("bpm") if (st and online) else None
    bs = f"{bpm:.0f}" if bpm else "---"
    d.text((bx + (62 - tw(d, bs, F_BPM)) // 2, 30), bs, font=F_BPM, fill=BLACK)
    d.text((bx + (62 - tw(d, "BPM", F_BPMU)) // 2, 66), "BPM", font=F_BPMU, fill=BLACK)
    if st and st.get("pending"):
        q = f"{st['pending']} queued"
        d.text((bx + (62 - tw(d, q, F_SMALL)) // 2, 80), q, font=F_SMALL, fill=BLACK)

    # track (left)
    maxw = bx - 14
    last = st.get("last") if st else None
    if not online:
        lines = ["TrackID", "not reachable"]
        for i, ln in enumerate(lines):
            d.text((6, 26 + i * 24), ln, font=F_TITLE, fill=BLACK)
        d.text((6, 76), fit(d, "check Wi-Fi / [trackid] url", F_SMALL, maxw), font=F_SMALL, fill=BLACK)
    elif last:
        t_lines = wrap(d, last.get("title", ""), F_TITLE, maxw, 2)
        f = F_TITLE
        if len(t_lines) == 2 and tw(d, last.get("title", ""), F_TITLE) > maxw * 2 - 10:
            t_lines, f = wrap(d, last.get("title", ""), F_TITLE_S, maxw, 2), F_TITLE_S
        y = 22
        for ln in t_lines:
            d.text((6, y), ln, font=f, fill=BLACK)
            y += 22 if f is F_TITLE else 18
        y += 2
        for ln in wrap(d, last.get("artist", ""), F_ARTIST, maxw, 2):
            d.text((6, y), ln, font=F_ARTIST, fill=BLACK)
            y += 16
    else:
        d.text((6, 30), "Waiting for", font=F_TITLE, fill=BLACK)
        d.text((6, 54), "the first track", font=F_TITLE, fill=BLACK)

    # footer: previous track + time logged
    d.line((0, 98, W, 98), fill=BLACK)
    if prev:
        s = f"before: {prev.get('artist', '?')} – {prev.get('title', '?')}"
    elif last and last.get("clock"):
        s = f"logged {last['clock']}" + (f"  ·  {last['bpm']:.0f} BPM" if last.get("bpm") else "")
    else:
        s = "TrackID Ink v" + VERSION
    d.text((6, 104), fit(d, s, F_SMALL, W - 12), font=F_SMALL, fill=BLACK)
    if toast:
        draw_toast(d, toast)
    return img


def draw_toast(d, text):
    """Touch feedback: an inverted strip over the footer."""
    d.rectangle((0, 99, W, H), fill=0)
    d.text(((W - tw(d, text, F_TAG)) // 2, 104), fit(d, text, F_TAG, W - 10), font=F_TAG, fill=255)


def render_list(st, online, entries, toast=None):
    """Second page (swipe): the most recent tracks of the current set, newest first."""
    img = Image.new("1", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 17), fill=0)
    d.text((5, 2), "THIS SET", font=F_TAG, fill=255)
    right = (f"{'AUTO' if st.get('auto') else 'MANUAL'}  #{st.get('count', 0)}" if (st and online) else "OFFLINE")
    d.text((W - 5 - tw(d, right, F_TAG), 2), right, font=F_TAG, fill=255)
    rows = [e for e in (entries or []) if e.get("status") in ("matched", "nomatch")][-5:][::-1]
    if not rows:
        d.text((6, 40), "No tracks yet" if online else "TrackID not reachable", font=F_TITLE_S, fill=0)
    y = 21
    for e in rows:
        name = ("ID - ID" if e.get("status") == "nomatch"
                else f"{e.get('artist', '?')} - {e.get('title', '?')}")
        t = e.get("start") or ""
        d.text((5, y), t, font=F_SMALL, fill=0)
        d.text((52, y - 1), fit(d, name, F_ARTIST, W - 57), font=F_ARTIST, fill=0)
        y += 19
    if toast:
        draw_toast(d, toast)
    elif rows:
        d.line((0, 116, W, 116), fill=0)
    return img


# ================================================================== panel

class Panel:
    """Waveshare 2.13" e-Paper (V2, V3 or V4 panel) with partial updates and a periodic full refresh.
    The panel stays awake while updates keep coming (fast partials) and sleeps after a quiet minute;
    the first update after sleeping is a full refresh."""

    SLEEP_AFTER_S = 60

    def __init__(self, model, full_every, full_minutes, flip):
        import importlib
        sys.path.insert(0, APP_DIR)
        self.model = model if model in ("V2", "V3", "V4") else "V4"
        self.epd = importlib.import_module(f"waveshare_epd.epd2in13_{self.model}").EPD()
        self.full_every = full_every
        self.full_s = full_minutes * 60
        self.flip = flip
        self.partials = 0
        self.last_full = 0.0
        self.last_show = 0.0
        self.awake = False
        self.in_part_mode = False
        log.info("Waveshare 2.13\" %s panel", self.model)

    def _buf(self, img):
        if self.flip:
            img = img.rotate(180)
        return self.epd.getbuffer(img)

    def _init(self, partial):
        if self.model == "V2":
            self.epd.init(self.epd.PART_UPDATE if partial else self.epd.FULL_UPDATE)
        elif not partial:
            self.epd.init()
        self.in_part_mode = partial

    def show(self, img):
        now = time.time()
        if not self.awake or self.partials >= self.full_every or now - self.last_full > self.full_s:
            self._init(False)
            self.epd.displayPartBaseImage(self._buf(img))   # full refresh, also the base for partials
            self.partials, self.last_full, self.awake = 0, now, True
            log.info("full refresh")
        else:
            if self.model == "V2" and not self.in_part_mode:
                self._init(True)
            self.epd.displayPartial(self._buf(img))
            self.partials += 1
        self.last_show = now

    def idle(self):
        """Call regularly: puts the panel to sleep after a quiet spell."""
        if self.awake and time.time() - self.last_show > self.SLEEP_AFTER_S:
            self.epd.sleep()
            self.awake = False
            self.in_part_mode = False

    def clear(self):
        self._init(False)
        self.epd.Clear(0xFF)
        self.epd.sleep()
        self.awake = False


class AdafruitPanel:
    """Adafruit 2.13" Monochrome E-Ink Bonnet (#4687): CS CE0, DC D22, RST D27, BUSY D17.
    Adafruit's driver only does full refreshes (~2-3 s with a flash), so updates are kept to real changes."""

    DRIVERS = {"ssd1680b": ("adafruit_epd.ssd1680b", "Adafruit_SSD1680B"),          # current panel (GDEY0213B74)
               "ssd1680": ("adafruit_epd.ssd1680", "Adafruit_SSD1680"),
               "ssd1680_legacy": ("adafruit_epd.ssd1680_legacy", "Adafruit_SSD1680_Legacy"),
               "ssd1675b": ("adafruit_epd.ssd1675b", "Adafruit_SSD1675B")}          # oldest bonnets

    def __init__(self, driver, flip):
        import importlib
        import board
        import busio
        from digitalio import DigitalInOut
        mod, cls = self.DRIVERS.get(driver, self.DRIVERS["ssd1680b"])
        Driver = getattr(importlib.import_module(mod), cls)
        spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
        self.epd = Driver(122, 250, spi, cs_pin=DigitalInOut(board.D8), dc_pin=DigitalInOut(board.D22),
                          sramcs_pin=None, rst_pin=DigitalInOut(board.D27), busy_pin=DigitalInOut(board.D17))
        self.epd.rotation = 3 if flip else 1
        log.info("Adafruit bonnet, driver %s", cls)

    def show(self, img):
        self.epd.image(img.convert("RGB"))
        self.epd.display()

    def idle(self):
        pass

    def clear(self):
        from adafruit_epd.epd import Adafruit_EPD
        self.epd.fill(Adafruit_EPD.WHITE)
        self.epd.display()


class Touch:
    """GT1151 capacitive touch on the Waveshare 2.13" Touch e-Paper HAT: I2C bus 1 at 0x14, reset on GPIO22,
    interrupt (low = touch data waiting) on GPIO27. A thread polls it and turns touches into gestures:
    "tap", "hold" and "swipe" (finger moved sideways across the screen), pushed onto `events`."""

    ADDR = 0x14

    def __init__(self, flip, hold_s=1.0):
        import gpiozero
        try:
            from smbus import SMBus
        except ImportError:
            from smbus2 import SMBus
        self.bus = SMBus(1)
        self.trst = gpiozero.LED(22)
        self.flip = flip
        self.hold_s = hold_s
        self.events = queue.Queue()
        self.trst.on(); time.sleep(0.1)
        self.trst.off(); time.sleep(0.1)
        self.trst.on(); time.sleep(0.1)
        ver = self._read(0x8140, 4)
        log.info("touch controller GT%s", "".join(chr(b) for b in ver if 32 <= b < 127) or "?")
        threading.Thread(target=self._run, daemon=True).start()

    # GT1151 registers are 16-bit: write the address high byte, then low byte (+ data)
    def _write(self, reg, val):
        self.bus.write_word_data(self.ADDR, (reg >> 8) & 0xFF, (reg & 0xFF) | ((val & 0xFF) << 8))

    def _read(self, reg, n):
        self.bus.write_byte_data(self.ADDR, (reg >> 8) & 0xFF, reg & 0xFF)
        return [self.bus.read_byte(self.ADDR) for _ in range(n)]

    def _point(self):
        """(x, y) in the sign's landscape coordinates (0..249, 0..121) of the first finger, or None."""
        st = self._read(0x814E, 1)[0]
        if not st & 0x80:
            return "none"
        n = st & 0x0F
        p = None
        if 1 <= n <= 5:
            b = self._read(0x814F, 8)
            px, py = (b[2] << 8) | b[1], (b[4] << 8) | b[3]       # panel is portrait: px 0..121, py 0..249
            p = (py, 121 - px) if self.flip else (249 - py, px)
        self._write(0x814E, 0)
        return p

    def _run(self):
        down = None           # (t0, x0, y0)
        last = None           # (t, x, y)
        held = False
        while True:
            try:
                p = self._point()                             # cheap at this rate; no need to rely on INT
            except OSError:
                time.sleep(0.5)
                continue
            now = time.time()
            if isinstance(p, tuple):
                if down is None:
                    down, held = (now, p[0], p[1]), False
                last = (now, p[0], p[1])
                if not held and now - down[0] >= self.hold_s and abs(p[0] - down[1]) < 25:
                    held = True
                    self.events.put("hold")                   # fire while still pressed: feels responsive
            elif down is not None and (p is None or now - last[0] > 0.15):
                dx = last[1] - down[1]
                if not held:
                    if abs(dx) >= 50:
                        self.events.put("swipe")
                    elif now - down[0] < self.hold_s:
                        self.events.put("tap")
                down = last = None
            time.sleep(0.03)


# ================================================================== app

def post(url, path, body=None, timeout=4):
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json", "X-TrackID": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}"}


def fetch_set(url, timeout=3):
    with urllib.request.urlopen(url.rstrip("/") + "/api/set", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")).get("entries", [])


def fetch_state(url, timeout=3):
    with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def view_key(st, online, bpm_shown):
    """What's on screen, so we only redraw when it changes. BPM is compared after rounding/hysteresis."""
    if not online or not st:
        return ("offline",)
    last = st.get("last") or {}
    return (last.get("artist"), last.get("title"), st.get("count"), st.get("auto"), bool(st.get("matching")),
            st.get("pending"), bpm_shown)


def main():
    ap = argparse.ArgumentParser(description="TrackID Ink - e-paper now-playing sign")
    ap.add_argument("--config", default=os.path.join(APP_DIR, "config.ini"))
    ap.add_argument("--preview", metavar="DIR", help="render sample screens to PNG and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.preview:
        os.makedirs(args.preview, exist_ok=True)
        last = {"artist": "Fred again.., Skrillex & Four Tet", "title": "Baby again..", "bpm": 126.0, "clock": "23:41:07"}
        prev = {"artist": "Aerosmith", "title": "Walk This Way"}
        long = {"artist": "Some Artist With An Extremely Long Collaborative Name feat. Another One",
                "title": "An Absurdly Long Extended Club Mix Title That Keeps Going (Original Mix)", "bpm": 124.0}
        samples = {
            "1_now_playing": (render({"auto": True, "count": 8, "bpm": 126.4, "last": last}, True, prev)),
            "2_long_names": (render({"auto": False, "count": 9, "bpm": 124.0, "last": long, "pending": 2}, True, last)),
            "3_matching": (render({"auto": True, "count": 8, "bpm": 127.0, "matching": True, "last": last}, True)),
            "4_waiting": (render({"auto": False, "count": 0, "bpm": None, "last": None}, True)),
            "5_offline": (render(None, False)),
            "6_touch_identifying": (render({"auto": True, "count": 8, "bpm": 126.4, "last": last}, True, prev,
                                           "IDENTIFYING…")),
            "7_touch_list": (render_list({"auto": True, "count": 6}, True, [
                {"status": "matched", "start": "0:00:00", "artist": "Karissa Hobbs", "title": "Let's Go Fishin'"},
                {"status": "matched", "start": "0:04:05", "artist": "Kevin MacLeod", "title": "Vibe Ace"},
                {"status": "nomatch", "start": "0:09:31"},
                {"status": "matched", "start": "0:13:12", "artist": "DJ Anubis", "title": "Darkwatch (VIP Edit)"},
                {"status": "matched", "start": "0:18:40", "artist": "Fred again.., Skrillex & Four Tet",
                 "title": "Baby again.."}])),
            "8_touch_auto_off": (render_list({"auto": False, "count": 6}, True, [
                {"status": "matched", "start": "0:00:00", "artist": "Karissa Hobbs", "title": "Let's Go Fishin'"}],
                "AUTO OFF")),
        }
        for name, img in samples.items():
            img.convert("L").save(os.path.join(args.preview, name + ".png"))
        print(f"wrote {len(samples)} previews to {args.preview}")
        return

    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULTS)
    cfg.read(args.config)
    url = cfg.get("trackid", "url")
    poll = max(1.0, cfg.getfloat("trackid", "poll"))
    bpm_step = max(1, cfg.getint("display", "bpm_step"))
    bpm_gap = max(0, cfg.getint("display", "bpm_min_gap"))
    flip = cfg.getboolean("display", "flip")
    if cfg.get("display", "panel").strip().lower().startswith("adafruit"):
        panel = AdafruitPanel(cfg.get("display", "adafruit_driver").strip().lower(), flip)
        bpm_gap = max(bpm_gap, 20)                           # full refreshes only: change the BPM less often
    else:
        panel = Panel(cfg.get("display", "waveshare_model").strip().upper(),
                      max(5, cfg.getint("display", "full_refresh_every")),
                      max(1, cfg.getint("display", "full_refresh_minutes")), flip)

    touch = None
    if cfg.getboolean("touch", "enabled"):
        try:
            touch = Touch(flip, max(0.4, cfg.getfloat("touch", "hold_s")))
        except Exception as exc:
            log.warning("touch screen not found (%s): running as a plain sign. Is I2C on? (sudo raspi-config)", exc)
    list_timeout = max(5, cfg.getint("touch", "list_timeout"))

    running = [True]
    signal.signal(signal.SIGTERM, lambda *a: running.__setitem__(0, False))
    signal.signal(signal.SIGINT, lambda *a: running.__setitem__(0, False))
    log.info("TrackID Ink %s, following %s", VERSION, url)

    shown_key, bpm_shown, bpm_t = None, None, 0.0
    history = []                        # last tracks seen, for the "before:" line
    misses = 0
    page, page_t = "now", 0.0           # touch: "now" or "list" (recent tracks)
    entries, entries_t = [], 0.0
    toast, toast_until = None, 0.0
    st, online, next_poll = None, False, 0.0
    while running[0]:
        now = time.time()
        # ---- touch gestures -> TrackID
        while touch is not None and not touch.events.empty():
            ev = touch.events.get()
            log.info("touch: %s", ev)
            try:
                if ev == "tap":
                    r = post(url, "/api/identify")
                    toast = ("IDENTIFYING…" if r.get("started") else "ALREADY MATCHING") if r.get("ok") \
                        else (r.get("error") or "TrackID said no")[:40]
                elif ev == "hold":
                    r = post(url, "/api/auto")
                    toast = (f"AUTO {'ON' if r.get('auto') else 'OFF'}") if r.get("ok") \
                        else (r.get("error") or "TrackID said no")[:40]
                    if r.get("ok") and st:
                        st["auto"] = r.get("auto")
                elif ev == "swipe":
                    page = "list" if page == "now" else "now"
                    page_t, entries_t = now, 0.0
                    toast = None
            except Exception as exc:
                toast = "TrackID not reachable"
                log.warning("remote %s failed: %s", ev, exc)
            if ev != "swipe":
                toast_until = now + 4
            next_poll = 0.0                               # show the result right away
        if toast and now > toast_until:
            toast = None
        if page == "list" and now - page_t > list_timeout:
            page = "now"                                  # back to Now Playing after a while
        # ---- poll TrackID
        if now >= next_poll:
            next_poll = now + poll
            try:
                st = fetch_state(url)
                online, misses = True, 0
            except Exception as exc:
                misses += 1
                st, online = None, misses < 3   # ride out a couple of dropped polls before showing OFFLINE
                if misses == 3:
                    log.warning("TrackID not reachable at %s: %s", url, exc)
            if st:
                last = st.get("last")
                if last and (not history or (history[-1].get("artist"), history[-1].get("title")) != (last.get("artist"), last.get("title"))):
                    history = (history + [last])[-2:]
                b = st.get("bpm")
                if b is None:
                    bpm_shown = None
                elif bpm_shown is None or (abs(b - bpm_shown) >= bpm_step and now - bpm_t >= bpm_gap):
                    bpm_shown, bpm_t = round(b), now        # BPM changes only every few seconds: less flicker
            if page == "list" and online and now - entries_t > 10:
                try:
                    entries, entries_t = fetch_set(url), now
                except Exception:
                    pass
        if st is None and online:
            time.sleep(0.05)
            continue
        view = dict(st, bpm=bpm_shown) if st else None
        if page == "list":
            key = ("list", online, view and view.get("auto"), view and view.get("count"),
                   tuple((e.get("id"), e.get("artist"), e.get("title"), e.get("status")) for e in entries[-5:]), toast)
        else:
            key = view_key(view, online, bpm_shown) + (toast,)
        if key != shown_key:
            prev = history[-2] if len(history) >= 2 else None
            try:
                img = (render_list(view, online, entries, toast) if page == "list"
                       else render(view, online, prev, toast))
                panel.show(img)
                shown_key = key
            except Exception:
                log.exception("display update failed")
        try:
            panel.idle()
        except Exception:
            log.exception("panel sleep failed")
        time.sleep(0.05 if touch is not None else poll)
    try:
        panel.clear()
    except Exception:
        pass


if __name__ == "__main__":
    main()
