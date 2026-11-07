"""TrackID v2 screens: 240x280 frames for the PiSugar Whisplay (rounded top corners)."""
import math
import os

from PIL import Image, ImageDraw, ImageFont

try:
    import numpy as np
except ImportError:
    np = None

W, H = 240, 280
TOP = 24

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"]


def font(size, bold=True, mono=False):
    name = ("DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf") if mono else \
           ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


BG = (6, 6, 9)
PANEL = (18, 17, 26)
FG = (238, 238, 244)
DIM = (128, 128, 144)
FAINT = (52, 52, 66)
AMBER = (255, 176, 0)
GREEN = (57, 255, 20)
PURPLE = (140, 110, 255)
BLUE = (60, 150, 255)
RED = (255, 60, 70)
PINK = (235, 70, 200)
CYAN = (40, 200, 230)


def hue_rgb(h):
    h = (h % 256) / 256.0 * 6
    i = int(h)
    f = h - i
    q, t = int(255 * (1 - f)), int(255 * f)
    return [(255, t, 0), (q, 255, 0), (0, 255, t), (0, q, 255), (t, 0, 255), (255, 0, q)][i % 6]


def scale(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


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


def to_rgb565(img):
    img = img.convert("RGB")
    if np is not None:
        a = np.asarray(img, dtype=np.uint16)
        v = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
        return v.astype(">u2").tobytes()
    out = bytearray()
    for r, g, b in img.getdata():
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out += bytes(((v >> 8) & 0xFF, v & 0xFF))
    return bytes(out)


class Screens:
    def __init__(self):
        self.f_hdr = font(12)
        self.f_bpm = font(54)
        self.f_bpmu = font(13)
        self.f_title = font(19)
        self.f_mid = font(15)
        self.f_small = font(12, bold=False)
        self.f_mono = font(11, bold=False, mono=True)
        self.f_big = font(26)
        self._marquee_cache = {}

    # ---- pieces -----------------------------------------------------------------------------------
    def _header(self, d, auto, set_label):
        d.text((16, TOP - 8), f"SET {set_label}" if set_label else "TRACKID", font=self.f_hdr, fill=DIM)
        chip = "AUTO" if auto else "MANUAL"
        col = GREEN if auto else PURPLE
        w = tw(d, chip, self.f_hdr) + 14
        x = W - 16 - w
        d.rounded_rectangle((x, TOP - 10, x + w, TOP + 8), radius=9, fill=col if auto else None, outline=col)
        d.text((x + 7, TOP - 8), chip, font=self.f_hdr, fill=(0, 0, 0) if auto else col)

    def _marquee(self, d, img, text, x, y, max_w, f, fill, t):
        if tw(d, text, f) <= max_w:
            d.text((x, y), text, font=f, fill=fill)
            return
        key = (text, id(f), fill)
        strip = self._marquee_cache.get(key)
        if strip is None:
            gap = "     ·     "
            full = text + gap
            wfull = tw(d, full, f)
            l, t0, r, b = d.textbbox((0, 0), text, font=f)
            strip = Image.new("RGB", (wfull * 2, b + 4), BG)
            sd = ImageDraw.Draw(strip)
            sd.text((0, 0), full + full, font=f, fill=fill)
            self._marquee_cache = {key: (strip, wfull)}
            strip = (strip, wfull)
        im, wfull = strip
        hold = 1.5                                            # pause at the start of each lap
        speed = 35.0                                          # px/s
        lap = wfull / speed + hold
        ph = t % lap
        off = int(max(0.0, ph - hold) * speed)
        crop = im.crop((off, 0, off + max_w, im.height))
        img.paste(crop, (x, y))

    # ---- idle / live --------------------------------------------------------------------------------
    def idle(self, s, t):
        """s: dict with auto, set_n, count, bpm, locked, beat (0..3 or None), flash (0..1), hue, level,
        scope (list of (v, on)), last (entry or None), next_in (s or None), interval, matching, pending, footer."""
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        hue = hue_rgb(s["hue"])
        flash = s["flash"]
        if flash > 0.02 and s["locked"]:                       # beat glow behind the BPM
            d.rounded_rectangle((10, 34, W - 10, 120), radius=14, fill=scale(hue, 0.10 + 0.22 * flash))
        self._header(d, s["auto"], s["set_label"])

        # BPM
        if s["locked"]:
            bs = f"{s['bpm']:.0f}"
            col = scale(FG, 1) if flash < 0.6 else hue
        else:
            bs, col = "---", FAINT
        bw = tw(d, bs, self.f_bpm)
        d.text(((W - bw) // 2 - 12, 40), bs, font=self.f_bpm, fill=col)
        d.text(((W + bw) // 2 - 6, 80), "BPM", font=self.f_bpmu, fill=DIM)
        if s["locked"] and s.get("src") == "conductor":         # BPM comes from the Beat Commander, not the mic
            d.text(((W + bw) // 2 - 6, 62), "LINK", font=self.f_bpmu, fill=GREEN)
        # beat pips
        for i in range(4):
            cx, cy, r = W // 2 - 36 + i * 24, 112, 6 if i == 0 else 5
            if s["locked"] and s["beat"] == i:
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=hue)
            else:
                d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=FAINT, width=2)

        # onset scope + level meter
        d.rounded_rectangle((12, 126, W - 12, 168), radius=8, fill=PANEL)
        sc = s["scope"]
        n = len(sc)
        for i, (v, on) in enumerate(sc[-(W - 32):]):
            h = int(v * 26)
            if h:
                x = 16 + i
                d.line((x, 158 - h, x, 158), fill=hue if on else FAINT)
        lv = min(1.0, s["level"] * 6)
        d.rectangle((16, 161, 16 + int((W - 32) * lv), 164), fill=AMBER if lv > 0.9 else GREEN)

        # auto countdown / status line
        y = 176
        if s["matching"]:
            dots = "." * (1 + int(t * 3) % 3)
            d.text((16, y), "MATCHING" + dots, font=self.f_hdr, fill=AMBER)
        elif s["auto"] and s["next_in"] is not None:
            frac = 1 - max(0.0, min(1.0, s["next_in"] / max(1, s["interval"])))
            d.text((16, y), f"NEXT ID {int(s['next_in']) // 60}:{int(s['next_in']) % 60:02d}", font=self.f_hdr, fill=GREEN)
            d.rounded_rectangle((110, y + 4, W - 16, y + 9), radius=3, fill=FAINT)
            d.rounded_rectangle((110, y + 4, 110 + int((W - 126) * frac), y + 9), radius=3, fill=GREEN)
        elif s["auto"]:
            d.text((16, y), "AUTO: WAITING FOR MUSIC", font=self.f_hdr, fill=DIM)
        else:
            d.text((16, y), "TAP TO IDENTIFY", font=self.f_hdr, fill=DIM)
        if s["pending"]:
            p = f"{s['pending']} QUEUED"
            d.text((W - 16 - tw(d, p, self.f_hdr), y), p, font=self.f_hdr, fill=BLUE)

        # last track
        y = 198
        d.text((16, y), f"LAST · {s['count']} LOGGED", font=self.f_hdr, fill=scale(hue, 0.9) if s["last"] else DIM)
        if s["last"]:
            self._marquee(d, img, s["last"].get("title", ""), 16, y + 16, W - 32, self.f_mid, FG, t)
            self._marquee(d, img, s["last"].get("artist", ""), 16, y + 36, W - 32, self.f_small, DIM, t + 0.7)
        else:
            d.text((16, y + 16), "nothing yet", font=self.f_mid, fill=FAINT)
        # footer
        d.line((16, H - 22, W - 16, H - 22), fill=FAINT)
        ft = s["footer"]
        d.text(((W - tw(d, ft, self.f_mono)) // 2, H - 18), ft, font=self.f_mono, fill=DIM)
        return img

    # ---- cards ---------------------------------------------------------------------------------------
    def result(self, e, number, duplicate, t):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        acc = CYAN if duplicate else GREEN
        d.rectangle((0, 0, W, 4), fill=acc)
        hdr = "ALREADY LOGGED" if duplicate else f"#{number} LOGGED"
        d.text((16, TOP - 8), hdr, font=self.f_hdr, fill=acc)
        if e.get("bpm"):
            b = f"{e['bpm']:.0f} BPM"
            d.text((W - 16 - tw(d, b, self.f_hdr), TOP - 8), b, font=self.f_hdr, fill=DIM)
        y = 46
        for ln in wrap(d, e.get("title", ""), self.f_title, W - 32, 3):
            d.text((16, y), ln, font=self.f_title, fill=FG)
            y += 25
        y += 4
        for ln in wrap(d, e.get("artist", ""), self.f_mid, W - 32, 2):
            d.text((16, y), ln, font=self.f_mid, fill=acc)
            y += 20
        y += 8
        meta = [m for m in (e.get("album") if e.get("album") != e.get("title") else "",
                            (e.get("release_date") or "")[:4]) if m]
        if meta:
            for ln in wrap(d, " · ".join(meta), self.f_small, W - 32, 2):
                d.text((16, y), ln, font=self.f_small, fill=DIM)
                y += 16
        if e.get("label"):
            d.text((16, y), fit(d, e["label"], self.f_small, W - 32), font=self.f_small, fill=DIM)
        d.line((16, H - 22, W - 16, H - 22), fill=FAINT)
        ft = f"in the set at {e.get('offset', '--:--')}"
        d.text(((W - tw(d, ft, self.f_mono)) // 2, H - 18), ft, font=self.f_mono, fill=DIM)
        return img

    def message(self, acc, header, big, lines, footer=""):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, W, 4), fill=acc)
        d.text((16, TOP - 8), header, font=self.f_hdr, fill=acc)
        y = 52
        for ln in wrap(d, big, self.f_big, W - 32, 2):
            d.text((16, y), ln, font=self.f_big, fill=acc)
            y += 32
        y += 10
        for line in lines:
            for ln in wrap(d, line, self.f_small, W - 32, 4):
                d.text((16, y), ln, font=self.f_small, fill=FG)
                y += 17
            y += 6
        if footer:
            d.line((16, H - 22, W - 16, H - 22), fill=FAINT)
            d.text(((W - tw(d, footer, self.f_mono)) // 2, H - 18), footer, font=self.f_mono, fill=DIM)
        return img

    def splash(self, version):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        for i in range(4):
            c = hue_rgb(160 + i * 40)
            d.rounded_rectangle((44 + i * 40, 92, 72 + i * 40, 100), radius=4, fill=c)
        f = font(34)
        s = "TRACKID"
        d.text(((W - tw(d, s, f)) // 2, 116), s, font=f, fill=FG)
        s = f"v{version} · ANUBIS DARKWATCH"
        d.text(((W - tw(d, s, self.f_hdr)) // 2, 160), s, font=self.f_hdr, fill=PURPLE)
        s = "listening…"
        d.text(((W - tw(d, s, self.f_small)) // 2, 190), s, font=self.f_small, fill=DIM)
        return img
