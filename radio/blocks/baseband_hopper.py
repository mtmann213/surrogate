"""
BasebandHopper — gr.sync_block that applies frequency-hopping rotation in baseband.

Architecture
------------
Both TX and RX stay on a single fixed RF center frequency. The hopping is
implemented entirely in baseband as a complex rotation:

    TX: out_sample[n] = in_sample[n] * exp(+j * 2π * Δf_k * t_n)
    RX: out_sample[n] = in_sample[n] * exp(-j * 2π * Δf_k * t_n)

where Δf_k = hop_freq[k] - center_frequency, and k changes every period_samples
(burst + guard).

Synchronization
---------------
On a single B210, both hoppers use their GR sample counter (_sample_count).
The counters differ by the propagation delay D (~500 samples), which is ≪ the
hop period (~79k samples), so hop indices agree on both sides nearly always.
The residual phase rotation exp(-j * 2π * Δf * D / fs) is a constant per hop
that the Costas loop tracks.  Phase jumps at hop boundaries (~1 ms re-lock
transient, ~1% of a hop period) are the only loss mechanism.

The GR sample counter is used instead of get_mimo_time_ns() because UHD device
time read inside work() has buffer-latency jitter that creates phase
discontinuities at every work() call for non-zero Δf, causing ~70% frame loss
on non-center hops.

Constraint
----------
|Δf_max| + signal_BW/2 ≤ sample_rate / 2.  With chip_rate=250 kHz,
rolloff=0.35, signal_BW=337.5 kHz, and sample_rate=1 MHz,
|Δf| ≤ 331.25 kHz.  Default ±300 kHz is within bounds.

Convention
----------
- TX mode applies +Δf_k
- RX mode applies -Δf_k
"""
from __future__ import annotations

import logging

import numpy as np
from gnuradio import gr

from core.hop_scheduler import HopScheduler

log = logging.getLogger(__name__)


class BasebandHopper(gr.sync_block):

    def __init__(
        self,
        hop_scheduler: HopScheduler,
        sample_rate: float,
        burst_duration_s: float,
        guard_duration_s: float,
        center_frequency: float,
        mode: str = "tx",
    ):
        gr.sync_block.__init__(
            self,
            name="Baseband Hopper",
            in_sig=[np.complex64],
            out_sig=[np.complex64],
        )

        self._scheduler = hop_scheduler
        self._sample_rate = sample_rate
        self._period_s = burst_duration_s + guard_duration_s
        self._center_freq = center_frequency
        self._mode = mode
        self._rotate_sign = 1.0 if mode == "tx" else -1.0

        self._period_samples = int(self._period_s * sample_rate)
        self._sample_count = 0

    def work(self, input_items, output_items):
        n = len(input_items[0])
        out = output_items[0]
        inp = input_items[0]

        if n == 0:
            return 0

        # Short-circuit: single frequency means no rotation needed.
        if len(self._scheduler.get_all_frequencies()) == 1:
            out[:] = inp
            self._sample_count += n
            return n

        cnt = self._sample_count

        idx_arr = np.arange(cnt, cnt + n, dtype=np.int64)
        hop_idx = idx_arr // self._period_samples
        rel_idx = (idx_arr - hop_idx * self._period_samples).astype(np.float64)

        uh = np.sort(np.unique(hop_idx))
        max_hop = int(uh[-1])

        cum_df = np.zeros(max_hop + 2, dtype=np.float64)
        for h in range(1, max_hop + 1):
            df_prev = self._scheduler.frequency_at(int(h - 1)) - self._center_freq
            cum_df[h] = cum_df[h - 1] + df_prev

        phases = np.zeros(n, dtype=np.float64)
        for h in uh:
            hop_freq = self._scheduler.frequency_at(int(h))
            delta_f = hop_freq - self._center_freq
            slope = 2.0 * np.pi * self._rotate_sign * delta_f / self._sample_rate
            offset_phase = 2.0 * np.pi * self._rotate_sign * cum_df[int(h)] * self._period_samples / self._sample_rate
            mask = hop_idx == h
            phases[mask] = offset_phase + slope * rel_idx[mask]

        rot = np.exp(1j * phases).astype(np.complex64)
        out[:] = inp * rot
        self._sample_count += n

        return n