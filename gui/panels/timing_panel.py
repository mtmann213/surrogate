"""Burst Timing Configuration Panel."""
from __future__ import annotations
from PyQt5.QtWidgets import (
    QWidget, QFormLayout, QDoubleSpinBox, QGroupBox,
    QVBoxLayout, QPushButton, QLabel, QFrame
)
from PyQt5.QtCore import pyqtSignal, Qt


class TimingPanel(QWidget):
    config_changed = pyqtSignal(dict)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_config(cfg)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Auto-computed info
        info_grp = QGroupBox("Auto-Computed Parameters")
        info_layout = QVBoxLayout(info_grp)
        note = QLabel(
            "<b>Burst duration and RF bandwidth are calculated automatically</b> "
            "from the frame and modulation settings — you cannot enter them manually. "
            "Change chip rate, code length, or frame size to affect these values."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555; font-size: 10px; padding: 2px;")
        info_layout.addWidget(note)

        self.derived_label = QLabel("Burst: —  |  Bandwidth: —  |  Throughput: —")
        self.derived_label.setStyleSheet(
            "font-family: monospace; font-size: 11px; "
            "background: #f0f0f0; padding: 4px; border: 1px solid #ccc;"
        )
        self.derived_label.setWordWrap(True)
        info_layout.addWidget(self.derived_label)
        layout.addWidget(info_grp)

        # Editable timing parameters
        grp = QGroupBox("Burst Timing")
        form = QFormLayout(grp)

        self.transition = QDoubleSpinBox()
        self.transition.setRange(0.0, 5.0)
        self.transition.setSingleStep(0.1)
        self.transition.setDecimals(2)
        self.transition.setSuffix(" ms")
        self.transition.setToolTip(
            "Guard time added after each burst before the next hop.\n"
            "Gives the USRP time to retune to the next frequency.\n"
            "0.5 ms is sufficient for B210 frequency changes of < 100 MHz.\n"
            "Increase to 1–2 ms if you see tune misses in logs."
        )
        form.addRow("Transition / Guard:", self.transition)

        self.preamble_dur = QDoubleSpinBox()
        self.preamble_dur.setRange(0.1, 5.0)
        self.preamble_dur.setSingleStep(0.1)
        self.preamble_dur.setDecimals(2)
        self.preamble_dur.setSuffix(" ms")
        self.preamble_dur.setToolTip(
            "Informational: approximate duration of the preamble section.\n"
            "Actual preamble length in chips is set in Frame → Preamble Bits.\n"
            "At 1 Mchip/s, 32 bits = 0.032 ms."
        )
        form.addRow("Preamble Duration (info):", self.preamble_dur)

        self.jitter = QDoubleSpinBox()
        self.jitter.setRange(0.0, 500.0)
        self.jitter.setSingleStep(1.0)
        self.jitter.setDecimals(1)
        self.jitter.setSuffix(" µs σ")
        self.jitter.setToolTip(
            "Random timing jitter added to each burst start (Gaussian, std-dev σ).\n"
            "Used to simulate imperfect burst synchronisation or clock drift.\n"
            "Set to 0 for deterministic burst timing (recommended for initial testing).\n"
            "Values > 100 µs may cause the RX to miss preambles."
        )
        form.addRow("Timing Jitter (σ):", self.jitter)

        layout.addWidget(grp)

        apply_btn = QPushButton("Apply Timing Config")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)
        layout.addStretch()

    def _load_config(self, cfg):
        t = cfg.timing
        self.transition.setValue(t.transition_time_ms)
        self.preamble_dur.setValue(t.preamble_duration_ms)
        self.jitter.setValue(t.timing_jitter_us)
        self._refresh_derived(cfg)

    def _refresh_derived(self, cfg):
        try:
            from core.config_manager import compute_frame_stats
            s = compute_frame_stats(cfg)
            self.derived_label.setText(
                f"Burst: {s['burst_ms']:.2f} ms  "
                f"({s['total_chips']:,} chips @ {cfg.modulation.chip_rate_sps/1e6:.3f} Mchip/s)  |  "
                f"Period: {s['period_ms']:.2f} ms  |  "
                f"Bandwidth: {s['bandwidth_hz']/1e6:.3f} MHz  |  "
                f"Throughput: {s['throughput_bps']:.0f} bps  |  "
                f"sps: {s['sps']:.0f}  |  "
                f"Proc. gain: {s['proc_gain_db']:.1f} dB"
            )
        except Exception:
            pass

    def _apply(self):
        self.config_changed.emit({
            "timing": {
                "transition_time_ms": self.transition.value(),
                "preamble_duration_ms": self.preamble_dur.value(),
                "timing_jitter_us": self.jitter.value(),
            }
        })
