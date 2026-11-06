#!/usr/bin/env python3
"""TrackID Ink - a now-playing sign for TrackID on a 2.13" e-paper (250x122, black/white):
the Waveshare 2.13" e-Paper HAT (V2, V3 or V4 panel), or the Adafruit 2.13" Monochrome E-Ink Bonnet (#4687).

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
import signal
import sys
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0"
W, H = 250, 122
log = logging.getLogger("trackid-ink")

DEFAULTS = {
    "trackid": {"url": "http://trackid.local:8080", "poll": "2"},
    "display": {"panel": "waveshare", "waveshare_model": "V4", "adafruit_driver": "ssd1680b", "bpm_step": "1", "bpm_min_gap": "6",
                "full_refresh_every": "30", "full_refresh_minutes": "15", "flip": "false"},
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

def render(st, online, prev=None):
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


# ================================================================== app

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

    running = [True]
    signal.signal(signal.SIGTERM, lambda *a: running.__setitem__(0, False))
    signal.signal(signal.SIGINT, lambda *a: running.__setitem__(0, False))
    log.info("TrackID Ink %s, following %s", VERSION, url)

    shown_key, bpm_shown, bpm_t = None, None, 0.0
    history = []                        # last tracks seen, for the "before:" line
    misses = 0
    while running[0]:
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
            now = time.time()
            if b is None:
                bpm_shown = None
            elif bpm_shown is None or (abs(b - bpm_shown) >= bpm_step and now - bpm_t >= bpm_gap):
                bpm_shown, bpm_t = round(b), now            # BPM changes only every few seconds: less flicker
        if st is None and online:
            time.sleep(poll)
            continue
        view = dict(st, bpm=bpm_shown) if st else None
        key = view_key(view, online, bpm_shown)
        if key != shown_key:
            prev = history[-2] if len(history) >= 2 else None
            try:
                panel.show(render(view, online, prev))
                shown_key = key
            except Exception:
                log.exception("display update failed")
        try:
            panel.idle()
        except Exception:
            log.exception("panel sleep failed")
        time.sleep(poll)
    try:
        panel.clear()
    except Exception:
        pass


if __name__ == "__main__":
    main()
