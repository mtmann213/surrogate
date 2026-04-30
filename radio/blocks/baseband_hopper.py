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
On a single B210, TX and RX share the same sample clock. Both hoppers use the
GNU Radio sample counter (_sample_count), which tracks within a few samples
across both flowgraphs. The propagation delay (< 1 ms ≪ hop period) creates a
constant per-hop phase offset that the Costas loop tracks naturally.

Constraint
----------
|Δf_max| ≤ sample_rate / 2  (Nyquist).  With the default hop set
(±300 kHz) and sample_rate = 500 kHz the constraint is just satisfied
(±300 kHz < 250 kHz Nyquist limit, so the outermost hops are aliased).
Reduce ±Δf_max or increase sample_rate for full Nyquist compliance.

Convention
----------
- TX mode applies +Δf_k  (shifts signal up/down in baseband → UHD sink at
  center_freq emits it at hop_freq[k] on air)
- RX mode applies -Δf_k  (UHD source at center_freq captures air signal at
  hop_freq[k] and this block rotates it back to baseband for demod)
"""
from __future__ import annotations

import logging
from typing import Optional

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
        uhd_handle=None,
    ):
        gr.sync_block.__init__(
            self,
            name="Baseband Hopper",
            in_sig=[np.complex64],
            out_sig=[np.complex64],
        )

        self._scheduler = hop_scheduler
        self._sample_rate = sample_rate
        self._burst_s = burst_duration_s
        self._guard_s = guard_duration_s
        self._period_s = burst_duration_s + guard_duration_s
        self._center_freq = center_frequency
        self._mode = mode
        self._rotate_sign = 1.0 if mode == "tx" else -1.0
        self._uhd = uhd_handle

        self._period_samples = int(self._period_s * sample_rate)
        self._sample_count = 0

        # Phase accumulator for continuous phase across hop boundaries
        self._phase = 0.0

    def set_start_time(self) -> None:
        log.debug(
            "[BasebandHopper %s] using GR sample counter", self._mode
        )

    def _elapsed_samples(self) -> int:
        return self._sample_count

    def work(self, input_items, output_items):
        n = len(input_items[0])
        out = output_items[0]
        inp = input_items[0]

        if n == 0:
            return 0

        # If hopping is disabled, the hop scheduler's sequence has one entry
        # (the center frequency), so delta_f is always 0.  Short-circuit to
        # avoid the per-sample rotation overhead.
        if len(self._scheduler.get_all_frequencies()) == 1:
            out[:] = inp
            self._sample_count += n
            return n

        cnt = self._elapsed_samples()

        # Vectorised phase computation — no Python per-sample for-loop.
        # Continuous phase: the phase accumulator does NOT reset at hop
        # boundaries.  This avoids phase discontinuities (~pi radians) when
        # delta_f changes sign, which would confuse the Costas loop and cause
        # preamble detection failures at hop transitions.
        #
        # Phase at sample s: phi[s] = sum over all hops h of
        #   2*pi * rotate_sign * delta_f_h / fs * min(period_samples, max(0, s - h*period_samples))
        # This is equivalent to: phi[s] = 2*pi * rotate_sign / fs * sum_h (delta_f_h * t_h_in_hop)
        #
        # Closed form: for sample s in hop H at offset r within the period:
        #   phi[s] = 2*pi*rs/fs * (sum_{h<H} delta_f_h * period_samples  +  delta_f_H * r)
        #          = 2*pi*rs/fs * (period_samples * cumulative_df[H]  +  delta_f_H * r)
        # where cumulative_df[H] = sum of delta_f for hops 0..H-1

        idx_arr = np.arange(cnt, cnt + n, dtype=np.int64)
        hop_idx = idx_arr // self._period_samples
        rel_idx = (idx_arr - hop_idx * self._period_samples).astype(np.float64)

        # Pre-compute cumulative delta_f for all unique hop indices
        uh = np.sort(np.unique(hop_idx))
        max_hop = int(uh[-1])

        # Build cumulative delta_f array: cum_df[h] = sum of delta_f for hops 0..h-1
        # This gives the phase offset at the start of hop h due to all previous hops.
        cum_df = np.zeros(max_hop + 2, dtype=np.float64)
        for h in range(1, max_hop + 1):
            df_prev = self._scheduler.frequency_at(int(h - 1)) - self._center_freq
            cum_df[h] = cum_df[h - 1] + df_prev

        phases = np.zeros(n, dtype=np.float64)
        for h in uh:
            hop_freq = self._scheduler.frequency_at(int(h))
            delta_f = hop_freq - self._center_freq
            slope = 2.0 * np.pi * self._rotate_sign * delta_f / self._sample_rate
            # Phase offset from all previous hops: cum_df[h] * period_samples
            offset_phase = 2.0 * np.pi * self._rotate_sign * cum_df[int(h)] * self._period_samples / self._sample_rate
            mask = hop_idx == h
            phases[mask] = offset_phase + slope * rel_idx[mask]

        rot = np.exp(1j * phases).astype(np.complex64)
        out[:] = inp * rot
        self._sample_count += n

        return n