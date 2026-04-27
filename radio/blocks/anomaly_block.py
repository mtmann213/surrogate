"""
Anomaly Block — GNU Radio block.

Applies live anomaly impairments to the IQ sample stream.
Reads from a shared AnomalyInjector state object each work() call so that
GUI updates take effect within one processing buffer.

Input/Output: complex64
"""
from __future__ import annotations

import numpy as np
from gnuradio import gr
import pmt

from core.anomaly_injector import AnomalyInjector


class AnomalyBlock(gr.sync_block):

    def __init__(self, injector: AnomalyInjector, sample_rate: float):
        gr.sync_block.__init__(
            self,
            name="Anomaly Block",
            in_sig=[np.complex64],
            out_sig=[np.complex64],
        )
        self._injector = injector
        self._sample_rate = sample_rate
        self._phase_acc = 0.0  # For CFO phase continuity

    def work(self, input_items, output_items):
        in0 = input_items[0]
        out = output_items[0]
        n = len(in0)

        samples = in0.copy()

        s = self._injector.state
        if not s.enabled:
            out[:] = samples
            return n

        # Apply IQ imbalance
        if s.iq_enabled:
            samples = self._injector.apply_iq_imbalance(samples)

        # Apply power fade
        if s.fade_enabled:
            samples = self._injector.apply_fade(samples, self._sample_rate)

        # Apply CFO (with phase continuity)
        if s.carrier_freq_offset_hz != 0.0:
            t = (np.arange(n) + self._phase_acc) / self._sample_rate
            rotation = np.exp(1j * 2 * np.pi * s.carrier_freq_offset_hz * t).astype(np.complex64)
            samples = (samples * rotation).astype(np.complex64)
            self._phase_acc += n
            if self._phase_acc > self._sample_rate * 1000:
                self._phase_acc = 0.0  # Prevent overflow

        out[:] = samples
        return n
