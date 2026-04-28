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
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional

from PyQt5.QtCore import QObject, pyqtSignal

from core.config_manager import ConfigManager, SurrogateConfig
from core.hop_scheduler import HopScheduler
from core.frame_generator import FrameGenerator
from core.fec_codec import FECCodec
from core.anomaly_injector import AnomalyInjector
from radio.hop_timing import HopTimingController

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
        """Query the USRP hardware every 2s to report actual frequency."""
        while self._running:
            time.sleep(2.0)
            with self._lock:
                if not self._tx_fg or not self._rx_fg:
                    continue
                
                try:
                    # Query the actual center frequency from the hardware driver
                    tx_freq = 0.0
                    rx_freq = 0.0
                    if self._tx_fg._uhd_sink:
                        tx_freq = self._tx_fg._uhd_sink.get_center_freq(0)
                    if self._rx_fg._uhd_src and hasattr(self._rx_fg._uhd_src, "get_center_freq"):
                        rx_freq = self._rx_fg._uhd_src.get_center_freq(0)
                    
                    if tx_freq > 0 or rx_freq > 0:
                        log.info("[HW-STATE] Actual Freq: TX=%.3f MHz, RX=%.3f MHz", 
                                 tx_freq/1e6, rx_freq/1e6)
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
        if self._hop_timing is not None:
            s.hop_index = self._hop_timing.current_hop_index()
            s.current_hop_freq = self._hop_timing.current_hop_freq()
        else:
            s.hop_index = 0
            s.current_hop_freq = self._hop_scheduler.frequency_at(0)
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
        if self._hop_timing is not None:
            return self._hop_timing.current_hop_freq()
        return self._hop_scheduler.frequency_at(0)

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
        self._hop_timing: Optional[HopTimingController] = None

    def _build_flowgraphs(self) -> None:
        from radio.tx_flowgraph import TXFlowgraph
        from radio.rx_flowgraph import RXFlowgraph

        cfg = self._cm.config
        self._tx_fg = TXFlowgraph(
            cfg, self._hop_scheduler, self._frame_gen,
            self._fec_codec, self._anomaly_injector,
            self._on_tx_frame
        )

        self._rx_fg = RXFlowgraph(
            cfg, self._hop_scheduler, self._frame_gen,
            self._fec_codec, self._dispatch_frame
        )

        # ----------------------------------------------------------------
        # Configure the shared FPGA epoch BEFORE starting either flowgraph.
        # set_start_time() must be called pre-start, otherwise UHD ignores
        # it and falls back to streaming-on-feed behaviour. Pre-queueing the
        # first batch of timed retunes also has to happen here so the very
        # first guard interval (between bursts 0 and 1) is covered.
        # ----------------------------------------------------------------
        if cfg.hopping.enabled and not cfg.rf.simulation:
            burst_s = cfg.timing.burst_duration_ms / 1000.0
            guard_s = cfg.timing.transition_time_ms / 1000.0
            self._hop_timing = HopTimingController(
                self._hop_scheduler,
                cfg.rf.sample_rate, burst_s, guard_s,
                tx_channel=cfg.rf.tx_channel,
                rx_channel=cfg.rf.rx_channel,
            )
            self._hop_timing.attach(
                tx_uhd=self._tx_fg._uhd_sink,
                rx_uhd=self._rx_fg._uhd_src,
            )
            self._hop_timing.configure_epoch()
            self._hop_timing.start()
        else:
            self._hop_timing = None

        # Start RX first so its UHD source is up and tuned to hop[0] before
        # the TX UHD sink emits its first burst.
        self._rx_fg.start()
        self._tx_fg.start()
        log.info("Flowgraphs started")

    def _stop_flowgraphs(self) -> None:
        if self._hop_timing is not None:
            try:
                self._hop_timing.stop()
            except Exception as exc:
                log.error("Error stopping HopTimingController: %s", exc)
            self._hop_timing = None
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
        # Authoritative hop state lives in HopTimingController (FPGA clock).
        if self._hop_timing is not None:
            idx = self._hop_timing.current_hop_index()
            hop_freq = self._hop_scheduler.frequency_at(idx)
        else:
            idx = 0
            hop_freq = self._hop_scheduler.frequency_at(0)

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

