"""
Anomaly injection state machine.

The AnomalyInjector holds all anomaly parameters in an atomically-updated
dataclass. GNU Radio blocks and the TX path read these values each processing
call, so changes from the GUI take effect within one burst period.
"""
from __future__ import annotations

import threading
import time
import logging
import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class AnomalyState:
    enabled: bool = False
    ber_injection: float = 0.0
    carrier_freq_offset_hz: float = 0.0
    extra_timing_jitter_us: float = 0.0
    # Burst dropout
    dropout_enabled: bool = False
    dropout_probability: float = 0.0
    # Power fade
    fade_enabled: bool = False
    fade_depth_db: float = 20.0
    fade_rate_hz: float = 5.0
    # IQ imbalance
    iq_enabled: bool = False
    iq_amplitude_db: float = 0.0
    iq_phase_deg: float = 0.0
    # Interference
    interference_enabled: bool = False
    interference_source_type: str = "awgn"
    interference_file_path: str = ""
    interference_zmq_address: str = ""
    interference_tone_freq_hz: float = 0.0
    interference_awgn_bw_hz: float = 1.0e6
    interference_power_db: float = -20.0


class AnomalyInjector:
    """
    Thread-safe anomaly parameter store.
    GUI calls update_*(); flowgraph blocks call apply_*() or read state directly.
    """

    def __init__(self, anomaly_cfg):
        self._lock = threading.RLock()
        self._state = self._cfg_to_state(anomaly_cfg)
        self._fade_phase = 0.0          # Internal phase accumulator for fade
        self._last_fade_time = time.time()

    # ------------------------------------------------------------------
    # State access (lock-free read for hot path — Python GIL protects reads)
    # ------------------------------------------------------------------

    @property
    def state(self) -> AnomalyState:
        return self._state

    # ------------------------------------------------------------------
    # Apply methods (called from flowgraph work() functions)
    # ------------------------------------------------------------------

    def should_drop_burst(self) -> bool:
        s = self._state
        if not s.enabled or not s.dropout_enabled:
            return False
        return np.random.random() < s.dropout_probability

    def apply_ber(self, bits: np.ndarray) -> np.ndarray:
        """Flip bits with probability ber_injection."""
        s = self._state
        if not s.enabled or s.ber_injection <= 0:
            return bits
        mask = (np.random.random(len(bits)) < s.ber_injection).astype(np.uint8)
        return (bits ^ mask).astype(np.uint8)

    def apply_cfo(self, samples: np.ndarray, sample_rate: float) -> np.ndarray:
        """Apply carrier frequency offset to IQ samples."""
        s = self._state
        if not s.enabled or s.carrier_freq_offset_hz == 0:
            return samples
        t = np.arange(len(samples)) / sample_rate
        rotation = np.exp(1j * 2 * np.pi * s.carrier_freq_offset_hz * t).astype(np.complex64)
        return (samples * rotation).astype(np.complex64)

    def apply_iq_imbalance(self, samples: np.ndarray) -> np.ndarray:
        """Apply amplitude and phase IQ imbalance."""
        s = self._state
        if not s.enabled or not s.iq_enabled:
            return samples
        amp = 10 ** (s.iq_amplitude_db / 20.0)
        phi = np.deg2rad(s.iq_phase_deg)
        I = samples.real
        Q = samples.imag
        I_out = I + Q * np.sin(phi)
        Q_out = Q * amp * np.cos(phi)
        return (I_out + 1j * Q_out).astype(np.complex64)

    def apply_fade(self, samples: np.ndarray, sample_rate: float) -> np.ndarray:
        """Apply a sinusoidal power fade (simplified Rician/Rayleigh approximation)."""
        s = self._state
        if not s.enabled or not s.fade_enabled:
            return samples
        n = len(samples)
        t = np.arange(n) / sample_rate + self._fade_phase / (2 * np.pi * s.fade_rate_hz + 1e-9)
        fade_linear = 10 ** (-s.fade_depth_db / 20.0 * 0.5 * (1 + np.sin(2 * np.pi * s.fade_rate_hz * t)))
        # Advance phase accumulator
        self._fade_phase = (self._fade_phase + 2 * np.pi * s.fade_rate_hz * n / sample_rate) % (2 * np.pi)
        return (samples * fade_linear.astype(np.float32)).astype(np.complex64)

    def timing_jitter_samples(self, sample_rate: float) -> int:
        """Return integer sample offset for timing jitter."""
        s = self._state
        if not s.enabled:
            return 0
        jitter_us = s.extra_timing_jitter_us
        if jitter_us <= 0:
            return 0
        jitter_s = np.random.normal(0, jitter_us * 1e-6)
        return int(jitter_s * sample_rate)

    # ------------------------------------------------------------------
    # Update from GUI / config
    # ------------------------------------------------------------------

    def update_from_config(self, anomaly_cfg) -> None:
        with self._lock:
            self._state = self._cfg_to_state(anomaly_cfg)

    def set_enabled(self, enabled: bool) -> None:
        self._state.enabled = enabled

    def set_ber(self, ber: float) -> None:
        self._state.ber_injection = float(np.clip(ber, 0.0, 1.0))

    def set_cfo(self, hz: float) -> None:
        self._state.carrier_freq_offset_hz = float(hz)

    def set_dropout(self, enabled: bool, probability: float) -> None:
        self._state.dropout_enabled = enabled
        self._state.dropout_probability = float(np.clip(probability, 0.0, 1.0))

    def set_fade(self, enabled: bool, depth_db: float, rate_hz: float) -> None:
        self._state.fade_enabled = enabled
        self._state.fade_depth_db = float(depth_db)
        self._state.fade_rate_hz = float(rate_hz)

    def set_iq_imbalance(self, enabled: bool, amp_db: float, phase_deg: float) -> None:
        self._state.iq_enabled = enabled
        self._state.iq_amplitude_db = float(amp_db)
        self._state.iq_phase_deg = float(phase_deg)

    def set_interference(self, enabled: bool, source_type: str = "awgn",
                         power_db: float = -20.0, **kwargs) -> None:
        self._state.interference_enabled = enabled
        self._state.interference_source_type = source_type
        self._state.interference_power_db = float(power_db)
        for k, v in kwargs.items():
            attr = f"interference_{k}"
            if hasattr(self._state, attr):
                setattr(self._state, attr, v)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _cfg_to_state(cfg) -> AnomalyState:
        s = AnomalyState()
        s.enabled = cfg.enabled
        s.ber_injection = cfg.ber_injection
        s.carrier_freq_offset_hz = cfg.carrier_freq_offset_hz
        s.extra_timing_jitter_us = cfg.extra_timing_jitter_us
        s.dropout_enabled = cfg.burst_dropout.enabled
        s.dropout_probability = cfg.burst_dropout.dropout_probability
        s.fade_enabled = cfg.power_fade.enabled
        s.fade_depth_db = cfg.power_fade.fade_depth_db
        s.fade_rate_hz = cfg.power_fade.fade_rate_hz
        s.iq_enabled = cfg.iq_imbalance.enabled
        s.iq_amplitude_db = cfg.iq_imbalance.amplitude_db
        s.iq_phase_deg = cfg.iq_imbalance.phase_deg
        s.interference_enabled = cfg.interference.enabled
        s.interference_source_type = cfg.interference.source_type
        s.interference_file_path = cfg.interference.file_path
        s.interference_zmq_address = cfg.interference.zmq_address
        s.interference_tone_freq_hz = cfg.interference.tone_freq_hz
        s.interference_awgn_bw_hz = cfg.interference.awgn_bandwidth_hz
        s.interference_power_db = cfg.interference.relative_power_db
        return s
