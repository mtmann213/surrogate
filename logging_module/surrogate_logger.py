"""
Unified surrogate logger.

Provides:
  - Python logging to file + console
  - CSV frame log via CSVWriter
  - Transmission event log entries
  - Timing/hop event log entries
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from logging_module.csv_writer import CSVWriter

_frame_id_counter = 0


class SurrogateLogger:

    def __init__(self, log_cfg):
        self._cfg = log_cfg
        self._csv: Optional[CSVWriter] = None
        self._log_queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False

        self._setup_logging(log_cfg)

        if log_cfg.csv.enabled:
            self._csv = CSVWriter(log_cfg.csv.file)

        self._py_log = logging.getLogger("surrogate")
        self._start_worker()

    # ------------------------------------------------------------------
    # Public API (thread-safe, non-blocking)
    # ------------------------------------------------------------------

    def log_tx_frame(self, frame_id: int, payload: bytes,
                     timestamp: float, hop_freq: float) -> None:
        if not self._cfg.log_transmission:
            return
        self._py_log.info(
            "[TX] Frame #%d | freq=%.3f MHz | payload=%s",
            frame_id, hop_freq / 1e6, payload.hex()[:32] + "..."
        )
        if self._csv:
            self._log_queue.put(("csv_tx", {
                "timestamp_utc": _fmt_ts(timestamp),
                "frame_id": frame_id,
                "direction": "TX",
                "hop_freq_hz": f"{hop_freq:.1f}",
                "payload_hex": payload.hex()[:64],
            }))

    def log_rx_frame(self, payload: bytes, timestamp: float,
                     snr: float, fec_ok: bool) -> None:
        if not self._cfg.log_transmission:
            return
        global _frame_id_counter
        _frame_id_counter += 1
        self._py_log.info(
            "[RX] Frame #%d | SNR=%.1f dB | FEC=%s | payload=%s",
            _frame_id_counter, snr, "OK" if fec_ok else "ERR",
            payload.hex()[:32] + "..." if payload else "None"
        )
        if self._csv:
            self._log_queue.put(("csv_rx", {
                "timestamp_utc": _fmt_ts(timestamp),
                "frame_id": _frame_id_counter,
                "direction": "RX",
                "snr_db": f"{snr:.2f}",
                "fec_ok": int(fec_ok),
                "payload_hex": payload.hex()[:64] if payload else "",
            }))

    def log_hop_event(self, freq: float, hop_index: int, timestamp: float) -> None:
        if not self._cfg.log_timing:
            return
        self._py_log.debug("[HOP] idx=%d freq=%.3f MHz t=%s",
                           hop_index, freq / 1e6, _fmt_ts(timestamp))

    def log_anomaly(self, description: str) -> None:
        self._py_log.warning("[ANOMALY] %s", description)

    def close(self) -> None:
        self._running = False
        if self._csv:
            self._csv.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _setup_logging(self, cfg) -> None:
        if not cfg.enabled:
            return
        level = getattr(logging, cfg.log_level.upper(), logging.INFO)
        log_dir = Path(cfg.log_file).parent
        log_dir.mkdir(parents=True, exist_ok=True)

        fmt = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        root = logging.getLogger()
        root.setLevel(level)

        # File handler
        fh = logging.FileHandler(cfg.log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)

        # Console handler
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    def _start_worker(self) -> None:
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._csv_worker, name="CSVWriter", daemon=True
        )
        self._worker_thread.start()

    def _csv_worker(self) -> None:
        """Drain the CSV queue asynchronously so log calls never block."""
        while self._running or not self._log_queue.empty():
            try:
                kind, row = self._log_queue.get(timeout=0.5)
                if self._csv:
                    self._csv.write_row(**row)
            except queue.Empty:
                continue
            except Exception:
                pass


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
