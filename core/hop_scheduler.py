"""
Frequency hop scheduler.

Both TX and RX instantiate this with the same seed and derive an identical
hop sequence independently — no over-the-air synchronization of the pattern
is needed. Timing recovery from the preamble aligns the hop slot boundaries.
"""
from __future__ import annotations

import threading
import time
import logging
from typing import List, Optional

import numpy as np

log = logging.getLogger(__name__)

_HOP_SPACING_HZ = 1.0e6  # 1 MHz default spacing when auto-generating


def _auto_frequencies(center: float, bandwidth: float,
                       spacing: float = _HOP_SPACING_HZ) -> List[float]:
    """Generate a set of hop frequencies centred on `center` within `bandwidth`."""
    half = bandwidth / 2.0
    n_hops = max(1, int(half / spacing))
    freqs = [center + i * spacing for i in range(-n_hops, n_hops + 1)]
    return sorted(set(freqs))


class HopScheduler:
    """
    Generates and tracks the hop sequence.
    Thread-safe — GUI and flowgraph threads can call freely.
    """

    def __init__(self, hopping_cfg, rf_cfg):
        self._cfg = hopping_cfg
        self._rf_cfg = rf_cfg
        self._lock = threading.Lock()
        self._index = 0
        self._sequence: np.ndarray = np.array([], dtype=np.float64)
        self._build_sequence()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def next_frequency(self) -> float:
        """Return the next hop frequency and advance the index."""
        with self._lock:
            freq = float(self._sequence[self._index % len(self._sequence)])
            self._index += 1
            return freq

    def frequency_at(self, hop_index: int) -> float:
        """Return the frequency for an absolute hop index (for RX pre-computation)."""
        with self._lock:
            return float(self._sequence[hop_index % len(self._sequence)])

    def current_index(self) -> int:
        with self._lock:
            return self._index

    def reset(self) -> None:
        with self._lock:
            self._index = 0

    def update_seed(self, seed: int) -> None:
        with self._lock:
            self._cfg.hop_seed = seed
            self._build_sequence()
            self._index = 0
        log.info("Hop seed updated to 0x%X; sequence rebuilt", seed)

    def update_frequencies(self, frequencies: List[float]) -> None:
        with self._lock:
            self._cfg.hop_frequencies = frequencies
            self._build_sequence()
        log.info("Hop frequencies updated: %d entries", len(frequencies))

    def get_all_frequencies(self) -> List[float]:
        with self._lock:
            return self._sequence.tolist()

    def rebuild(self) -> None:
        """Rebuild the hop sequence from current config settings.
        Called during flowgraph restart after config changes."""
        with self._lock:
            self._build_sequence()
            self._index = 0
        log.info("Hop sequence rebuilt (enabled=%s, type=%s, n_freqs=%d)",
                 self._cfg.enabled, self._cfg.hop_type, len(self._sequence))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_sequence(self) -> None:
        """Rebuild the hop sequence from current config (call with lock held)."""
        if not self._cfg.enabled:
            # Hopping disabled: always return center frequency.
            self._sequence = np.array([self._rf_cfg.center_frequency], dtype=np.float64)
            log.debug("Hopping disabled — fixed on %.3f MHz",
                      self._rf_cfg.center_frequency / 1e6)
            return

        freqs = self._cfg.hop_frequencies
        if not freqs:
            freqs = _auto_frequencies(
                self._rf_cfg.center_frequency,
                self._rf_cfg.tx_bandwidth,
            )
            log.debug("Auto-generated %d hop frequencies", len(freqs))

        freqs_arr = np.array(freqs, dtype=np.float64)
        hop_type = self._cfg.hop_type
        seed = self._cfg.hop_seed

        if hop_type == "sequential":
            self._sequence = np.tile(freqs_arr, 10000)
        elif hop_type == "custom":
            if self._cfg.custom_pattern:
                self._sequence = np.array(self._cfg.custom_pattern, dtype=np.float64)
            else:
                self._sequence = freqs_arr
        else:  # random (default)
            rng = np.random.default_rng(seed)
            self._sequence = rng.choice(freqs_arr, size=100_000, replace=True)

        log.debug("Hop sequence built: type=%s, len=%d, seed=0x%X",
                  hop_type, len(self._sequence), seed)
