"""
Thread-safe CSV frame log writer.
"""
from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import Optional

COLUMNS = [
    "timestamp_utc",
    "frame_id",
    "direction",
    "hop_freq_hz",
    "burst_duration_ms",
    "snr_db",
    "ber_measured",
    "fec_ok",
    "payload_hex",
    "anomaly_flags",
]


class CSVWriter:

    def __init__(self, file_path: str):
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._file = None
        self._writer = None
        self._open()

    def _open(self) -> None:
        exists = self._path.exists()
        self._file = open(self._path, "a", newline="", buffering=1)
        self._writer = csv.DictWriter(self._file, fieldnames=COLUMNS)
        if not exists:
            self._writer.writeheader()

    def write_row(self, **kwargs) -> None:
        row = {k: "" for k in COLUMNS}
        row.update(kwargs)
        with self._lock:
            try:
                self._writer.writerow(row)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
