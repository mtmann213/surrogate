"""
IQ Recorder.

Supports cf32 (raw interleaved float32), i8 (raw interleaved int8), and
SigMF (cf32 data + JSON metadata sidecar).

Recording is started/stopped without flowgraph restart by connecting/disconnecting
a file_sink via the TX/RX flowgraph valve blocks.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from gnuradio import blocks as gr_blocks, gr

log = logging.getLogger(__name__)


class IQRecorder:

    def __init__(self, cfg, sample_rate: float, center_freq: float):
        self._cfg = cfg
        self._sample_rate = sample_rate
        self._center_freq = center_freq
        self._tx_sink: Optional[gr_blocks.file_sink] = None
        self._rx_sink: Optional[gr_blocks.file_sink] = None
        self._tx_file: Optional[Path] = None
        self._rx_file: Optional[Path] = None
        self._sigmf_meta: dict = {}
        self._recording = False
        self._start_time: Optional[float] = None

    def build_tx_sink(self) -> Optional[gr_blocks.file_sink]:
        """Build and return a GR file sink for TX IQ recording."""
        if not self._cfg.record_tx:
            return None
        path = self._make_path("tx")
        self._tx_file = path
        sink = self._make_sink(path)
        self._tx_sink = sink
        return sink

    def build_rx_sink(self) -> Optional[gr_blocks.file_sink]:
        """Build and return a GR file sink for RX IQ recording."""
        if not self._cfg.record_rx:
            return None
        path = self._make_path("rx")
        self._rx_file = path
        sink = self._make_sink(path)
        self._rx_sink = sink
        return sink

    def start(self) -> None:
        self._recording = True
        self._start_time = time.time()
        log.info("IQ recording started")

    def stop(self) -> None:
        if not self._recording:
            return
        self._recording = False
        if self._cfg.format == "sigmf":
            self._write_sigmf_metadata()
        log.info("IQ recording stopped")

    def update_center_freq(self, freq_hz: float) -> None:
        """Update center frequency annotation in sigmf metadata."""
        self._center_freq = freq_hz

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_path(self, direction: str) -> Path:
        out_dir = Path(self._cfg.output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = ".sigmf-data" if self._cfg.format == "sigmf" else f".{self._cfg.format}"
        return out_dir / f"surrogate_{direction}_{ts}{ext}"

    def _make_sink(self, path: Path) -> gr_blocks.file_sink:
        fmt = self._cfg.format
        if fmt in ("cf32", "sigmf"):
            return gr_blocks.file_sink(gr.sizeof_gr_complex, str(path), False)
        elif fmt == "i8":
            # Caller must insert a float_to_char conversion block before this sink
            return gr_blocks.file_sink(gr.sizeof_char * 2, str(path), False)
        else:
            raise ValueError(f"Unknown IQ format: {fmt}")

    def _write_sigmf_metadata(self) -> None:
        """Write SigMF .sigmf-meta sidecar next to each recorded file."""
        for path in [p for p in [self._tx_file, self._rx_file] if p]:
            meta_path = path.with_suffix(".sigmf-meta")
            meta = {
                "global": {
                    "core:datatype": "cf32_le",
                    "core:sample_rate": self._sample_rate,
                    "core:version": "1.0.0",
                    "core:recorder": "SurrogateDatalink",
                    "core:hw": "USRP B2xx",
                    "surrogate:center_frequency_hz": self._center_freq,
                },
                "captures": [{
                    "core:sample_start": 0,
                    "core:frequency": self._center_freq,
                    "core:datetime": datetime.fromtimestamp(
                        self._start_time, tz=timezone.utc
                    ).isoformat() if self._start_time else "",
                }],
                "annotations": [],
            }
            try:
                with open(meta_path, "w") as f:
                    json.dump(meta, f, indent=2)
                log.info("SigMF metadata written: %s", meta_path)
            except Exception as exc:
                log.error("Failed to write SigMF metadata: %s", exc)
