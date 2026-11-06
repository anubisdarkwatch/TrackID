"""Beat engine from the W10 Beat Conductor, ported to Python/numpy (same maths, same constants).

Onset strength (log-energy rise, kick band + full band) every 8 ms hop, tempo by autocorrelation
over ~6 s, beat clock phase-locked to strong onsets. Feed it 16 kHz mono float samples (-1..1).
"""
import math
import threading

import numpy as np

RATE = 16000
HOP = 128                 # 8 ms at 16 kHz
HOP_MS = 8.0
OSS_N = 768               # 6.1 s of onset strength
SCOPE_N = 200             # onset scope points kept for the screen


class BeatEngine:
    def __init__(self, gate=0.0015):
        self.gate = gate                     # below this RMS level: silence, no onsets
        self.lock = threading.Lock()
        self.oss = np.zeros(OSS_N, dtype=np.float32)
        self.pos = 0
        self.hops = 0
        self.lp = 0.0
        self.prev_low = -100.0
        self.prev_full = -100.0
        self.level = 0.0
        self.mean = 0.0
        self.var = 1e-4
        self.last_onset_ms = -1e9
        self.bpm = 120.0
        self.bpm_est = 0.0
        self.conf = 0.0
        self.est_hist = [0.0, 0.0, 0.0]
        self.est_n = 0
        self.locked = False                  # true once a confident tempo has been found
        self.next_beat = 0.0                 # ms (same clock as the timestamps fed in)
        self.phase_adj = 0.0
        self.beat_num = 0
        self.scope = np.zeros(SCOPE_N, dtype=np.float32)
        self.scope_on = np.zeros(SCOPE_N, dtype=bool)
        self.scope_pos = 0
        self._carry = np.zeros(0, dtype=np.float32)
        self._carry_t = 0.0

    # ---- feed audio -------------------------------------------------------------------------
    def feed(self, x, t_ms):
        """x: float32 mono samples at 16 kHz, t_ms: time of x[0] (ms)."""
        if self._carry.size:
            x = np.concatenate([self._carry, x])
            t_ms = self._carry_t
        n = (x.size // HOP) * HOP
        for i in range(0, n, HOP):
            self._hop(x[i:i + HOP], t_ms + i * 1000.0 / RATE)
        self._carry = x[n:].copy()
        self._carry_t = t_ms + n * 1000.0 / RATE

    def _hop(self, x, t_ms):
        # kick band: one-pole low-pass (~150 Hz), the same filter as the W10, run sample by sample
        a = 0.057
        lp = self.lp
        el = 0.0
        for s in x.tolist():
            lp += a * (s - lp)
            el += lp * lp
        self.lp = lp
        el /= x.size
        ef = float(np.dot(x, x)) / x.size
        self.level = self.level * 0.8 + math.sqrt(ef) * 0.2
        ldb = 10 * math.log10(el + 1e-10)
        fdb = 10 * math.log10(ef + 1e-10)
        o = max(0.0, ldb - self.prev_low) + 0.5 * max(0.0, fdb - self.prev_full)
        self.prev_low, self.prev_full = ldb, fdb
        if self.level < self.gate:
            o = 0.0
        with self.lock:
            self.oss[self.pos] = o
            self.pos = (self.pos + 1) % OSS_N
            self.hops += 1
        self.mean = self.mean * 0.99 + o * 0.01
        dv = o - self.mean
        self.var = self.var * 0.99 + dv * dv * 0.01
        thr = self.mean + 2.0 * math.sqrt(self.var)
        on = o > thr and o > 1.5 and t_ms - self.last_onset_ms > 220
        if on:
            self.last_onset_ms = t_ms
        if self.hops % 2 == 0:
            self.scope[self.scope_pos] = min(o / 12.0, 1.0)
            self.scope_on[self.scope_pos] = on
            self.scope_pos = (self.scope_pos + 1) % SCOPE_N
        elif on:
            self.scope_on[(self.scope_pos - 1) % SCOPE_N] = True
        if self.hops % 64 == 0:
            self._estimate(t_ms)

    # ---- tempo + phase (every ~0.5 s) -----------------------------------------------------------
    def _estimate(self, t_now):
        with self.lock:
            x = np.roll(self.oss, -self.pos).astype(np.float64)        # oldest -> newest
            pos = self.pos
            oss = self.oss.copy()
        x -= x.mean()
        r0 = float(np.dot(x, x))
        if r0 < 1e-6:
            self.conf = 0.0
            return
        lmin, lmax = 39, 108                                           # 190 .. 70 BPM at 8 ms hops
        full = np.correlate(x, x, mode="full")[OSS_N - 1:]             # full[L] = sum x[i] x[i-L]

        def acf(L):
            return float(full[L]) if L < OSS_N else 0.0
        sc = {}
        for L in range(lmin - 1, lmax + 2):
            r = acf(L) + 0.5 * acf(2 * L) + 0.33 * acf(3 * L) + 0.25 * acf(4 * L)
            b = 60000.0 / (L * HOP_MS)
            o = math.log2(b / 125.0)                                   # mild prior toward dance tempos
            sc[L] = r * math.exp(-0.5 * (o * o) / (0.45 * 0.45))
        best = max(range(lmin, lmax + 1), key=lambda L: sc[L])
        a, b, c = sc[best - 1], sc[best], sc[best + 1]
        den = a - 2 * b + c
        d = 0.5 * (a - c) / den if den != 0 else 0.0
        d = max(-0.5, min(0.5, d))
        self.bpm_est = 60000.0 / ((best + d) * HOP_MS)
        self.conf = b / (2.08 * r0)
        if self.conf < 0.08:
            return
        self.est_hist[self.est_n % 3] = self.bpm_est
        self.est_n += 1
        if self.est_n >= 3:                                            # median of the last 3 estimates
            med = sorted(self.est_hist)[1]
            if abs(med - self.bpm) / self.bpm > 0.04:
                self.bpm = med
            else:
                self.bpm = self.bpm * 0.8 + med * 0.2
            self.locked = True
        # phase: fold recent onset strength at the beat period, align the clock to the strongest slot
        P = 60000.0 / self.bpm / HOP_MS
        Pi = int(P + 0.5)
        best_s, best_k = -1.0, 0
        for k in range(Pi):
            s = 0.0
            j = 0
            while True:
                back = k + int(j * P + 0.5)
                if back >= OSS_N // 2:
                    break
                idx = (pos - 1 - back) % OSS_N
                v = oss[idx] + 0.5 * oss[(idx - 1) % OSS_N] + 0.5 * oss[(idx + 1) % OSS_N]
                s += v * v
                j += 1
            if s > best_s:
                best_s, best_k = s, k
        period = 60000.0 / self.bpm
        last_beat = t_now - best_k * HOP_MS - HOP_MS
        err = math.fmod(self.next_beat - last_beat, period)
        if err > period / 2:
            err -= period
        if err < -period / 2:
            err += period
        self.phase_adj -= err if abs(err) > period * 0.15 else err * 0.5

    # ---- beat clock (call often from the UI loop) -----------------------------------------------
    def tick(self, now_ms):
        """Advance the beat clock. Returns the beat index in the bar (0..3) when a beat lands, else None."""
        if self.phase_adj:
            self.next_beat += self.phase_adj
            self.phase_adj = 0.0
        if now_ms >= self.next_beat:
            period = 60000.0 / self.bpm
            self.beat_num += 1
            self.next_beat += period
            if now_ms - self.next_beat > 0:
                self.next_beat = now_ms + period
            return self.beat_num % 4
        return None
