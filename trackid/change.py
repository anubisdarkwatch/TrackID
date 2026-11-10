"""Track-change detector for TrackID's AUTO mode.

Every 0.5 s it takes a spectrum of the mic audio and reduces it to two small descriptors:
  - chroma (12 pitch classes): the harmony / key of what's playing
  - timbre (10 log-spaced bands, loudness removed): the shape of the mix (bass-heavy, bright, ...)
It compares the last few seconds against the stretch of music before it. When the two differ by more than
the threshold for a few seconds in a row, a track change is reported, so AUTO identifies right after a
transition instead of on a fixed timer.

Cheap on a Pi Zero 2W: one 8192-point FFT every half second.
"""
import collections
import threading
import time

import numpy as np

RATE = 16000
FRAME = RATE // 2           # 0.5 s per feature frame
NFFT = 8192
RECENT_S = 10.0             # "now": the last few seconds
REF_S = 30.0                # "before": the stretch preceding them
HISTORY_S = 90.0


def _bands():
    freqs = np.fft.rfftfreq(NFFT, 1.0 / RATE)
    edges = np.geomspace(60, 6000, 11)
    band_idx = [np.nonzero((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(edges[:-1], edges[1:])]
    sel = np.nonzero((freqs >= 110) & (freqs <= 2000))[0]
    pc = (np.rint(12 * np.log2(freqs[sel] / 440.0)).astype(int) + 9) % 12      # 0 = C
    return band_idx, sel, pc


BANDS, CHROMA_SEL, CHROMA_PC = _bands()
WIN = np.hanning(NFFT).astype(np.float32)


def features(x):
    """0.5 s of 16 kHz audio -> (chroma[12], timbre[10]) or None when too quiet."""
    if float(np.sqrt(np.mean(x * x))) < 1e-4:
        return None
    buf = np.zeros(NFFT, dtype=np.float32)
    n = min(x.size, NFFT)
    buf[:n] = x[-n:]
    p = np.abs(np.fft.rfft(buf * WIN)) ** 2
    chroma = np.bincount(CHROMA_PC, weights=np.sqrt(p[CHROMA_SEL]), minlength=12)
    chroma /= (np.linalg.norm(chroma) + 1e-9)
    tb = np.array([10 * np.log10(p[i].mean() + 1e-12) for i in BANDS])
    tb -= tb.mean()                                          # loudness out, shape stays
    return chroma, tb


class ChangeDetector:
    def __init__(self, threshold=0.4, hold_s=3.0, gate=0.004, recent_s=RECENT_S, ref_s=REF_S):
        self.recent_s, self.ref_s = recent_s, ref_s
        self.threshold = threshold
        self.hold_frames = max(1, int(hold_s * 2))
        self.gate = gate
        self.lock = threading.Lock()
        self.hist = collections.deque(maxlen=int(HISTORY_S * 2))   # (t, chroma, timbre, bpm)
        self._buf = np.zeros(0, dtype=np.float32)
        self.novelty = 0.0
        self.parts = (0.0, 0.0, 0.0)
        self.above = 0
        self.change_t = None          # wall time the current change started (set once confirmed)
        self.changes = 0
        self.bpm_fn = None            # optional callable -> current locked BPM or None

    # ---- audio in -----------------------------------------------------------------------------------
    def feed(self, x):
        self._buf = np.concatenate([self._buf, x])
        while self._buf.size >= FRAME:
            fr, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            self._frame(fr, time.time())

    def _frame(self, fr, now):
        if float(np.sqrt(np.mean(fr * fr))) < self.gate:
            return                                           # silence: pause, don't treat it as a new track
        f = features(fr)
        if f is None:
            return
        bpm = None
        if self.bpm_fn:
            try:
                bpm = self.bpm_fn()
            except Exception:
                bpm = None
        with self.lock:
            self.hist.append((now, f[0], f[1], bpm))
            self._update(now)

    def _update(self, now):
        nr, nf = int(self.recent_s * 2), int(self.ref_s * 2)
        if len(self.hist) < nr + nf // 2:                    # not enough of the current track yet
            self.novelty, self.above = 0.0, 0
            return
        h = list(self.hist)
        rec, ref = h[-nr:], h[-nr - nf:-nr]
        c_r = np.mean([e[1] for e in rec], axis=0)
        c_b = np.mean([e[1] for e in ref], axis=0)
        t_r = np.mean([e[2] for e in rec], axis=0)
        t_b = np.mean([e[2] for e in ref], axis=0)
        dc = 1.0 - float(np.dot(c_r, c_b) / (np.linalg.norm(c_r) * np.linalg.norm(c_b) + 1e-9))
        dt = float(np.sqrt(np.mean((t_r - t_b) ** 2))) / 6.0                 # ~6 dB RMS shape change -> 1
        br = [e[3] for e in rec if e[3]]
        bb = [e[3] for e in ref if e[3]]
        db = 0.0
        if len(br) > nr // 2 and len(bb) > nf // 2:
            a, b = float(np.median(br)), float(np.median(bb))
            # tempo moved and stayed moved; ignore half/double-time readings
            r = max(a, b) / min(a, b)
            if min(abs(r - 1), abs(r - 2)) > 0.025:
                db = min(1.0, (min(abs(r - 1), abs(r - 2)) - 0.025) * 20)
        self.parts = (dc, dt, db)
        self.novelty = 1.6 * dc + 0.5 * dt + 0.5 * db
        if self.novelty > self.threshold:
            self.above += 1
            if self.above == self.hold_frames and self.change_t is None:
                self.change_t = h[-self.above][0]           # when the difference first showed
                self.changes += 1
        else:
            self.above = 0

    # ---- control from the app ------------------------------------------------------------------------
    def consume(self):
        """The app has acted on the change: forget the old track so the comparison restarts on the new one."""
        with self.lock:
            t = self.change_t
            self.change_t = None
            self.above = 0
            if t is not None:
                while self.hist and self.hist[0][0] < t:
                    self.hist.popleft()
            self.novelty = 0.0

    def reset(self):
        with self.lock:
            self.hist.clear()
            self.change_t = None
            self.above = 0
            self.novelty = 0.0

    def state(self):
        return {"novelty": round(self.novelty, 3), "threshold": self.threshold,
                "parts": [round(p, 3) for p in self.parts], "change_pending": self.change_t is not None,
                "changes": self.changes}
