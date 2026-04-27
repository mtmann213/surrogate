"""Frequency Hopping Configuration Panel."""
from __future__ import annotations
from PyQt5.QtWidgets import (
    QWidget, QFormLayout, QDoubleSpinBox, QComboBox, QGroupBox,
    QVBoxLayout, QPushButton, QLabel, QTextEdit, QSpinBox, QCheckBox
)
from PyQt5.QtCore import pyqtSignal
import re


class HopPanel(QWidget):
    config_changed = pyqtSignal(dict)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_config(cfg.hopping)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # How hopping works
        how_grp = QGroupBox("How Frequency Hopping Works")
        how_layout = QVBoxLayout(how_grp)
        how_label = QLabel(
            "<b>Both TX and RX independently pre-compute the same hop sequence</b> from "
            "the shared <b>Hop Seed</b> using a PRNG (numpy default_rng). No over-the-air "
            "coordination is needed — as long as both sides start at the same time and "
            "advance at the same burst rate, they stay on the same frequency.<br><br>"
            "<b>Hop Types:</b><br>"
            "• <b>random</b> — PRNG sequence derived from Hop Seed. TX and RX must use "
            "the same seed. Each burst, both advance to the next frequency in the sequence.<br>"
            "• <b>sequential</b> — cycles through the Hop Frequencies list in order "
            "(0→1→2→…→0→…).<br>"
            "• <b>custom</b> — follows the Custom Pattern list exactly, repeating.<br><br>"
            "<b>Hop Frequencies:</b> leave empty to auto-generate from "
            "center_frequency ± N×tx_bandwidth steps (typically 8–16 channels).<br><br>"
            "<b>Tip:</b> disable hopping for initial loopback testing — enable it only "
            "after RX frames appear with hopping off."
        )
        how_label.setWordWrap(True)
        how_label.setStyleSheet("font-size: 10px; color: #333; padding: 2px;")
        how_layout.addWidget(how_label)
        layout.addWidget(how_grp)

        grp = QGroupBox("Frequency Hopping")
        form = QFormLayout(grp)

        self.hop_enable = QCheckBox("Enable frequency hopping")
        self.hop_enable.setToolTip(
            "When disabled, TX and RX stay on center_frequency for every burst.\n"
            "Disable during initial setup and loopback testing.\n"
            "Enable only after confirming RX frames are received with hopping off."
        )
        form.addRow(self.hop_enable)

        self.hop_type = QComboBox()
        self.hop_type.addItems(["random", "sequential", "custom"])
        self.hop_type.setToolTip(
            "random:     PRNG sequence from hop_seed. Both sides must share the same seed.\n"
            "sequential: cycle through hop_frequencies in order.\n"
            "custom:     follow the custom_pattern list (repeats when exhausted)."
        )
        form.addRow("Hop Type:", self.hop_type)

        self.hop_seed = QSpinBox()
        self.hop_seed.setRange(0, 2**31 - 1)
        self.hop_seed.setToolTip(
            "PRNG seed for random hop type. TX and RX MUST use the same value.\n"
            "Changing this on one side only causes immediate hop desynchronisation.\n"
            "Default: 0xDEADBEEF (3,735,928,559). Any 32-bit integer is valid."
        )
        form.addRow("Hop Seed:", self.hop_seed)

        self.hop_rate = QDoubleSpinBox()
        self.hop_rate.setRange(0, 10000)
        self.hop_rate.setDecimals(1)
        self.hop_rate.setSuffix(" hops/s  (0 = auto)")
        self.hop_rate.setToolTip(
            "How often to change frequency (hops per second).\n"
            "0 = auto: derived from burst duration (one hop per burst).\n"
            "Manual values are useful when TX and RX have different burst periods\n"
            "but should still hop in sync. In most cases leave at 0."
        )
        form.addRow("Hop Rate:", self.hop_rate)

        freq_lbl = QLabel("Hop Frequencies (Hz, one per line):")
        freq_lbl.setToolTip(
            "List of carrier frequencies to hop between (one per line, in Hz).\n"
            "Leave empty to auto-generate from center_frequency ± N×tx_bandwidth:\n"
            "  e.g. center=915 MHz, bw=1.35 MHz → [913.65, 915, 916.35, ...] MHz\n"
            "Example manual list:\n"
            "  902000000\n  905000000\n  908000000\n  915000000"
        )
        form.addRow(freq_lbl)
        self.freq_list = QTextEdit()
        self.freq_list.setFixedHeight(120)
        self.freq_list.setPlaceholderText(
            "Leave empty to auto-generate from center_frequency ± N×bandwidth\n"
            "Or enter one frequency per line in Hz:\n"
            "902000000\n905000000\n908000000"
        )
        self.freq_list.setToolTip(
            "One frequency per line in Hz. Leave empty to auto-generate.\n"
            "Auto-generates 8 channels spaced by tx_bandwidth around center_frequency."
        )
        form.addRow(self.freq_list)

        custom_lbl = QLabel("Custom Pattern (Hz, one per line, hop_type=custom):")
        custom_lbl.setToolTip(
            "Explicit hop sequence used when Hop Type = custom.\n"
            "The system will follow this list in order, repeating when exhausted.\n"
            "Useful for deterministic hopping patterns required by external protocols."
        )
        form.addRow(custom_lbl)
        self.custom_pattern = QTextEdit()
        self.custom_pattern.setFixedHeight(80)
        self.custom_pattern.setPlaceholderText("915000000\n912500000\n917500000")
        form.addRow(self.custom_pattern)

        layout.addWidget(grp)

        apply_btn = QPushButton("Apply Hopping Config")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)
        layout.addStretch()

    def _load_config(self, hop):
        self.hop_enable.setChecked(hop.enabled)
        self.hop_type.setCurrentText(hop.hop_type)
        self.hop_seed.setValue(hop.hop_seed & (2**31 - 1))
        self.hop_rate.setValue(hop.hop_rate_hz)
        if hop.hop_frequencies:
            self.freq_list.setPlainText("\n".join(str(f) for f in hop.hop_frequencies))
        if hop.custom_pattern:
            self.custom_pattern.setPlainText("\n".join(str(f) for f in hop.custom_pattern))

    def _parse_freq_list(self, text: str):
        freqs = []
        for line in text.strip().splitlines():
            line = line.strip()
            if line:
                try:
                    freqs.append(float(line))
                except ValueError:
                    pass
        return freqs

    def _apply(self):
        self.config_changed.emit({
            "hopping": {
                "enabled": self.hop_enable.isChecked(),
                "hop_type": self.hop_type.currentText(),
                "hop_seed": self.hop_seed.value(),
                "hop_rate_hz": self.hop_rate.value(),
                "hop_frequencies": self._parse_freq_list(self.freq_list.toPlainText()),
                "custom_pattern": self._parse_freq_list(self.custom_pattern.toPlainText()),
            }
        })
