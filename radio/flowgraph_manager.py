"""
Flowgraph Manager.

Owns the TX and RX flowgraph lifetimes and maintains a thread-safe LinkStats
snapshot that the GUI polls at its refresh rate.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

import numpy as np
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional

from PyQt5.QtCore import QObject, pyqtSignal

from core.config_manager import ConfigManager, SurrogateConfig
from core.hop_scheduler import HopScheduler
from core.frame_generator import FrameGenerator
from core.fec_codec import FECCodec
from core.anomaly_injector import AnomalyInjector
from radio.blocks.baseband_hopper import BasebandHopper

log = logging.getLogger(__name__)

_RESTART_REQUIRED_KEYS = {
    "rf.sample_rate", "rf.tx_bandwidth", "rf.rx_bandwidth",
    "rf.tx_channel", "rf.rx_channel",
    "modulation.type", "modulation.chip_rate_sps",
    "modulation.spreading.code_type", "modulation.spreading.code_length",
    "modulation.pulse_shaping.rolloff", "modulation.pulse_shaping.span_symbols",
    "frame.fec.primary_type", "frame.fec.cc.rate_inv",
    "frame.fec.cc.constraint_length", "frame.fec.cc.polynomials",
    "anomaly.interference.source_type", "anomaly.interference.file_path",
    "hopping.enabled", "hopping.hop_type", "hopping.hop_seed",
}

_MAX_EVENTS = 12          # recent-events ring buffer size
_RATE_WINDOW_S = 5.0      # rolling window for frames/sec calculation


@dataclass
class LinkStats:
    # Frame counters
    tx_frames: int = 0
    rx_frames: int = 0
    rx_fec_ok: int = 0
    rx_fec_err: int = 0

    # Rates (frames/sec over _RATE_WINDOW_S)
    tx_rate: float = 0.0
    rx_rate: float = 0.0

    # RF metrics
    last_snr: float = 0.0
    snr_history: List[float] = field(default_factory=list)   # last 30 values

    # Hopping
    current_hop_freq: float = 0.0
    hop_index: int = 0

    # Link health
    packet_loss_pct: float = 0.0   # (tx-rx)/tx * 100, only meaningful in loopback
    running: bool = False

    # Recent event log strings (newest last)
    recent_events: List[str] = field(default_factory=list)


class _RateTracker:
    """Tracks event timestamps in a rolling window to compute events/sec."""
    def __init__(self, window_s: float = _RATE_WINDOW_S):
        self._window = window_s
        self._times: Deque[float] = deque()

    def record(self) -> None:
        now = time.monotonic()
        self._times.append(now)
        cutoff = now - self._window
        while self._times and self._times[0] < cutoff:
            self._times.popleft()

    def rate(self) -> float:
        now = time.monotonic()
        cutoff = now - self._window
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
        return len(self._times) / self._window


class FlowgraphSignals(QObject):
    """Bridge for radio thread events to GUI thread."""
    frame_received = pyqtSignal(bytes, float, float, bool)  # payload, ts, snr, fec_ok
    tx_transmitted = pyqtSignal(int, bytes, float)         # frame_id, payload, ts


class FlowgraphManager:

    def __init__(self, config_manager: ConfigManager, logger):
        self._cm = config_manager
        self._logger = logger
        self._lock = threading.Lock()
        self._running = False
        self.signals = FlowgraphSignals()

        cfg = config_manager.config
        self._build_core_objects(cfg)

        self._tx_fg = None
        self._rx_fg = None

        # Stats
        self._stats = LinkStats()
        self._tx_tracker = _RateTracker()
        self._rx_tracker = _RateTracker()
        self._events: Deque[str] = deque(maxlen=_MAX_EVENTS)

        config_manager.register_listener(self._on_config_changed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._build_core_objects(self._cm.config)
            self._build_flowgraphs()
            self._running = True
            
            # Start hardware monitor
            self._hw_monitor_thread = threading.Thread(
                target=self._hw_monitor_worker, name="HW-Monitor", daemon=True
            )
            self._hw_monitor_thread.start()
            
        self._stats.running = True

    def _hw_monitor_worker(self) -> None:
        """Query the USRP hardware every 2s to report actual frequency and RX state."""
        while self._running:
            time.sleep(2.0)
            with self._lock:
                if not self._tx_fg or not self._rx_fg:
                    continue
                
                try:
                    tx_freq = 0.0
                    rx_freq = 0.0
                    if self._tx_fg._uhd_sink:
                        tx_freq = self._tx_fg._uhd_sink.get_center_freq(0)
                    if self._rx_fg._uhd_src and hasattr(self._rx_fg._uhd_src, "get_center_freq"):
                        rx_freq = self._rx_fg._uhd_src.get_center_freq(0)
                    
                    agc_gain = self._rx_fg.get_agc_gain() if hasattr(self._rx_fg, 'get_agc_gain') else 0.0
                    rx_snr = self._rx_fg.get_snr()
                    rx_frames = self._rx_fg.get_frame_count()
                    
                    if tx_freq > 0 or rx_freq > 0:
                        log.info(
                            "[HW-STATE] TX=%.3f MHz  RX=%.3f MHz  "
                            "AGC_gain=%.1f dB  RX_snr=%.1f dB  RX_frames=%d",
                            tx_freq/1e6, rx_freq/1e6,
                            20*np.log10(agc_gain) if agc_gain > 0 else 0.0,
                            rx_snr, rx_frames,
                        )
                except Exception as exc:
                    log.debug("HW Monitor error: %s", exc)

    def stop(self) -> None:
        with self._lock:
            self._stop_flowgraphs()
            self._running = False
        self._stats.running = False

    def restart(self) -> None:
        log.info("Restarting flowgraphs")
        with self._lock:
            self._stop_flowgraphs()
            self._build_core_objects(self._cm.config)
            self._build_flowgraphs()

    def get_stats(self) -> LinkStats:
        """Return a snapshot of current link statistics (safe to call from GUI thread)."""
        s = self._stats
        s.tx_frames = self._tx_fg.frame_count if self._tx_fg else 0
        s.rx_frames = self._rx_fg.get_frame_count() if self._rx_fg else 0
        s.last_snr = self._rx_fg.get_snr() if self._rx_fg else 0.0
        s.tx_rate = self._tx_tracker.rate()
        s.rx_rate = self._rx_tracker.rate()
        s.hop_index = self._hop_scheduler.current_index()
        s.current_hop_freq = self._hop_scheduler.frequency_at(s.hop_index)
        if s.tx_frames > 0:
            s.packet_loss_pct = max(0.0, (s.tx_frames - s.rx_fec_ok) / s.tx_frames * 100)
        s.recent_events = list(self._events)
        s.running = self._running
        return s

    # Kept for backwards compat with status panel
    def get_tx_frame_count(self) -> int:
        return self._tx_fg.frame_count if self._tx_fg else 0

    def get_rx_frame_count(self) -> int:
        return self._rx_fg.get_frame_count() if self._rx_fg else 0

    def get_snr(self) -> float:
        return self._rx_fg.get_snr() if self._rx_fg else 0.0

    def get_current_hop_freq(self) -> float:
        idx = self._hop_scheduler.current_index()
        return self._hop_scheduler.frequency_at(idx)

    # ------------------------------------------------------------------
    # Live parameter updates (no restart required)
    # ------------------------------------------------------------------

    def set_tx_gain(self, db: float) -> None:
        self._cm.update({"rf": {"tx_gain": db}})
        if self._tx_fg:
            self._tx_fg.set_tx_gain(db)

    def set_rx_gain(self, db: float) -> None:
        self._cm.update({"rf": {"rx_gain": db}})
        if self._rx_fg:
            self._rx_fg.set_rx_gain(db)

    def set_anomaly_state(self, updates: dict) -> None:
        self._cm.update({"anomaly": updates})
        self._anomaly_injector.update_from_config(self._cm.config.anomaly)

    def set_hop_frequencies(self, freqs: List[float]) -> None:
        self._hop_scheduler.update_frequencies(freqs)

    def set_hop_seed(self, seed: int) -> None:
        self._hop_scheduler.update_seed(seed)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_core_objects(self, cfg: SurrogateConfig) -> None:
        self._fec_codec = FECCodec(cfg.frame.fec)
        self._frame_gen = FrameGenerator(cfg.frame, self._fec_codec)
        # Single shared scheduler. Both TX and RX index by absolute hop number
        # via frequency_at(N); HopTimingController is the sole authority on
        # which N is current at any FPGA time.
        self._hop_scheduler = HopScheduler(cfg.hopping, cfg.rf)
        self._anomaly_injector = AnomalyInjector(cfg.anomaly)
        self._hop_timing = None  # legacy; kept as None for any remaining references

    def _build_flowgraphs(self) -> None:
        from radio.tx_flowgraph import TXFlowgraph
        from radio.rx_flowgraph import RXFlowgraph

        cfg = self._cm.config
        burst_s = cfg.timing.burst_duration_ms / 1000.0
        guard_s = cfg.timing.transition_time_ms / 1000.0

        # Create BasebandHopper instances for TX and RX when hopping is enabled.
        # Both are driven by the same hop scheduler and share the same burst/guard
        # timing. TX applies +Δf_k rotation, RX applies -Δf_k rotation.
        hopper_tx = None
        hopper_rx = None
        self._hop_timing = None  # legacy RF-hopping controller (unused in baseband path)

        if cfg.hopping.enabled:
            # Pass UHD handles so hoppers can use time-anchored indexing
            # for TX/RX synchronization.
            hopper_tx = BasebandHopper(
                self._hop_scheduler, cfg.rf.sample_rate,
                burst_s, guard_s, cfg.rf.center_frequency,
                mode="tx",
                uhd_handle=None,  # set after flowgraph build (needs sink)
            )
            hopper_rx = BasebandHopper(
                self._hop_scheduler, cfg.rf.sample_rate,
                burst_s, guard_s, cfg.rf.center_frequency,
                mode="rx",
                uhd_handle=None,  # set after flowgraph build (needs source)
            )
        else:
            hopper_tx = None
            hopper_rx = None

        self._tx_fg = TXFlowgraph(
            cfg, self._hop_scheduler, self._frame_gen,
            self._fec_codec, self._anomaly_injector,
            self._on_tx_frame,
            baseband_hopper=hopper_tx,
        )

        self._rx_fg = RXFlowgraph(
            cfg, self._hop_scheduler, self._frame_gen,
            self._fec_codec, self._dispatch_frame,
            baseband_hopper=hopper_rx,
        )

        # Start RX first so its UHD source is up and tuned to hop[0] before
        # the TX UHD sink emits its first burst.
        self._rx_fg.start()
        self._tx_fg.start()

        if cfg.hopping.enabled:
            hopper_tx.set_start_time()
            hopper_rx.set_start_time()

        log.info("Flowgraphs started")

    def _stop_flowgraphs(self) -> None:
        # Baseband hoppers are GR blocks owned by the flowgraphs — stopping
        # the flowgraph chain is sufficient to clean them up.
        for fg in (self._tx_fg, self._rx_fg):
            if fg:
                try:
                    fg.stop()
                    fg.wait()
                except Exception as exc:
                    log.error("Error stopping flowgraph: %s", exc)
        self._tx_fg = None
        self._rx_fg = None

    def _on_config_changed(self, cfg: SurrogateConfig) -> None:
        self._anomaly_injector.update_from_config(cfg.anomaly)
        if self._tx_fg:
            self._tx_fg.set_tx_gain(cfg.rf.tx_gain)
        if self._rx_fg:
            self._rx_fg.set_rx_gain(cfg.rf.rx_gain)

    def _on_tx_frame(self, frame_id: int, payload: bytes, timestamp: float) -> None:
        self._tx_tracker.record()
        idx = self._hop_scheduler.current_index()
        hop_freq = self._hop_scheduler.frequency_at(idx)

        self._logger.log_tx_frame(frame_id, payload, timestamp, hop_freq)
        
        # Dispatch via signals (for GUI/Qt thread)
        self.signals.tx_transmitted.emit(frame_id, payload, timestamp)

        ts = time.strftime("%H:%M:%S")
        self._events.append(f"{ts}  TX #{frame_id}  idx={idx}  {hop_freq/1e6:.3f} MHz")

    def _dispatch_frame(self, payload: bytes, timestamp: float,
                        snr: float, fec_ok: bool) -> None:
        # Update SNR history (keep last 30) for the graph regardless of FEC
        self._stats.snr_history.append(snr)
        if len(self._stats.snr_history) > 30:
            self._stats.snr_history.pop(0)

        # Log to terminal always (so the user can see what's failing)
        self._logger.log_rx_frame(payload, timestamp, snr, fec_ok)

        # Hop retunes are driven by HopTimingController on the FPGA clock;
        # the RX path no longer needs to react to preamble detection to
        # advance the hop index.

        if not fec_ok:
            self._stats.rx_fec_err += 1
            return

        self._rx_tracker.record()
        self._stats.rx_fec_ok += 1

        ts = time.strftime("%H:%M:%S")
        payload_preview = payload.hex()[:8] if payload else "----"
        self._events.append(
            f"{ts}  RX  SNR={snr:.1f}dB  {payload_preview}"
        )

        # Dispatch via signals (for GUI/Qt thread)
        self.signals.frame_received.emit(payload, timestamp, snr, fec_ok)

