"""Modulation and Spreading Configuration Panel."""
from __future__ import annotations
from PyQt5.QtWidgets import (
    QWidget, QFormLayout, QDoubleSpinBox, QComboBox, QGroupBox,
    QVBoxLayout, QPushButton, QLabel, QLineEdit, QSpinBox, QTextEdit
)
from PyQt5.QtCore import pyqtSignal


class ModulationPanel(QWidget):
    config_changed = pyqtSignal(dict)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._build_ui()
        self._load_config(cfg.modulation)
        self._refresh_derived(cfg)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Modulation
        mod_grp = QGroupBox("Modulation")
        mod_form = QFormLayout(mod_grp)

        self.mod_type = QComboBox()
        self.mod_type.addItems(["bpsk"])
        self.mod_type.setToolTip(
            "BPSK: Binary Phase Shift Keying. 1 bit/symbol. Current hardware runtime mode.\n\n"
            "DSSS Theory: Bits are XORed with the spreading sequence.\n"
            "Bit '0' = Chips transmitted as-is.\n"
            "Bit '1' = Chips are inverted (180° phase flip)."
        )
        mod_form.addRow("Type (Chip Mod):", self.mod_type)

        self.bit_rate = QDoubleSpinBox()
        self.bit_rate.setRange(1e3, 10e6)
        self.bit_rate.setSingleStep(1e3)
        self.bit_rate.setDecimals(0)
        self.bit_rate.setSuffix(" bps")
        self.bit_rate.setToolTip(
            "Informational target bit rate. Actual throughput is determined by\n"
            "chip_rate / code_length / FEC_rate and is shown in the Derived Stats box.\n"
            "This field does not change any signal parameter."
        )
        mod_form.addRow("Bit Rate (info):", self.bit_rate)

        self.chip_rate = QDoubleSpinBox()
        self.chip_rate.setRange(1e3, 10e6)
        self.chip_rate.setSingleStep(100e3)
        self.chip_rate.setDecimals(0)
        self.chip_rate.setSuffix(" chips/s")
        self.chip_rate.setToolTip(
            "Rate at which chips (spreading code symbols) are transmitted.\n"
            "Must divide evenly into sample_rate: sps = sample_rate / chip_rate must be an integer.\n"
            "Increasing chip_rate shortens burst duration and requires wider RF bandwidth.\n"
            "Default: 1 MHz with 8 MHz sample rate → sps = 8 (recommended minimum).\n"
            "The config validator will snap chip_rate to the nearest integer-sps value."
        )
        mod_form.addRow("Chip Rate:", self.chip_rate)
        layout.addWidget(mod_grp)

        # Spreading
        sp_grp = QGroupBox("DSSS Spreading")
        sp_form = QFormLayout(sp_grp)

        self.code_type = QComboBox()
        self.code_type.addItems(["gold", "msequence", "kasami", "custom"])
        self.code_type.setToolTip(
            "gold: XOR of two LFSR sequences — good cross-correlation properties, "
            "best choice for most uses.\n"
            "msequence: single maximal-length LFSR sequence — simpler, good autocorrelation.\n"
            "kasami: decimated m-sequence — requires even LFSR degree.\n"
            "custom: supply your own 0/1 sequence in the Custom Code box."
        )
        sp_form.addRow("Code Type:", self.code_type)

        self.code_length = QSpinBox()
        self.code_length.setRange(1, 1023)
        self.code_length.setToolTip(
            "Number of chips per coded bit (spreading factor / processing gain).\n"
            "Processing gain = 10*log10(code_length) dB.\n"
            "  code_length=7   → 8.5 dB gain\n"
            "  code_length=31  → 14.9 dB gain (default)\n"
            "  code_length=127 → 21.0 dB gain\n"
            "Longer codes give more interference rejection but longer bursts.\n"
            "Must equal 2^degree - 1 for Gold/m-sequence codes."
        )
        sp_form.addRow("Code Length (chips/bit):", self.code_length)

        self.code_seed = QSpinBox()
        self.code_seed.setRange(0, 2**31 - 1)
        self.code_seed.setToolTip(
            "Seed / starting state for the LFSR or code index for Gold codes.\n"
            "TX and RX must use the same code_seed to generate matching spreading codes.\n"
            "Changing this requires a system restart."
        )
        sp_form.addRow("Code Seed / Index:", self.code_seed)

        self.degree = QSpinBox()
        self.degree.setRange(2, 12)
        self.degree.setToolTip(
            "LFSR register length (degree N).\n"
            "code_length must equal 2^N - 1 for maximal-length sequences.\n"
            "degree=5 → code_length=31 (default)\n"
            "degree=7 → code_length=127\n"
            "degree=10 → code_length=1023"
        )
        sp_form.addRow("LFSR Degree:", self.degree)

        self.poly1 = QLineEdit()
        self.poly1.setPlaceholderText("e.g. 0x25")
        self.poly1.setToolTip(
            "First primitive polynomial for Gold/m-sequence (hex).\n"
            "Defines the LFSR feedback taps. Must be a primitive polynomial over GF(2).\n"
            "degree=5 defaults: poly1=0x25 (x⁵+x²+1), poly2=0x37 (x⁵+x⁴+x³+x²+1)\n"
            "See https://en.wikipedia.org/wiki/Primitive_polynomial_(field_theory) for tables."
        )
        sp_form.addRow("Poly 1 (hex):", self.poly1)

        self.poly2 = QLineEdit()
        self.poly2.setPlaceholderText("e.g. 0x37 (Gold only)")
        self.poly2.setToolTip(
            "Second primitive polynomial — used only for Gold codes.\n"
            "Must be a different primitive polynomial of the same degree as Poly 1."
        )
        sp_form.addRow("Poly 2 (hex):", self.poly2)

        self.custom_code = QTextEdit()
        self.custom_code.setFixedHeight(60)
        self.custom_code.setPlaceholderText("0,1,1,0,1,... (custom code_type only)")
        self.custom_code.setToolTip(
            "Custom spreading code as comma-separated 0s and 1s.\n"
            "Only used when Code Type = custom.\n"
            "Length should match code_length for proper spreading."
        )
        sp_form.addRow("Custom Code:", self.custom_code)
        layout.addWidget(sp_grp)

        # Pulse shaping
        ps_grp = QGroupBox("Pulse Shaping")
        ps_form = QFormLayout(ps_grp)

        self.filter_type = QComboBox()
        self.filter_type.addItems(["rrc", "rect"])
        self.filter_type.setToolTip(
            "rrc: Root Raised Cosine filter — band-limits the signal and minimises ISI.\n"
            "     Required for reliable M&M timing recovery on the RX side.\n"
            "rect: Rectangular pulse — no filtering. Simple but uses more bandwidth."
        )
        ps_form.addRow("Filter:", self.filter_type)

        self.rolloff = QDoubleSpinBox()
        self.rolloff.setRange(0.1, 1.0)
        self.rolloff.setSingleStep(0.05)
        self.rolloff.setDecimals(2)
        self.rolloff.setToolTip(
            "RRC excess bandwidth factor α (0.1–1.0).\n"
            "RF bandwidth = chip_rate × (1 + α).\n"
            "Lower α → narrower bandwidth, sharper filter, more taps needed.\n"
            "Higher α → wider bandwidth, easier timing recovery.\n"
            "α=0.35 is a good balance for most applications."
        )
        ps_form.addRow("Roll-off α:", self.rolloff)

        self.span = QSpinBox()
        self.span.setRange(4, 32)
        self.span.setToolTip(
            "RRC filter span in symbols (chips).\n"
            "Longer span → better stopband rejection but more filter delay and CPU.\n"
            "span=11 is standard for α=0.35. Minimum recommended: 6."
        )
        ps_form.addRow("Filter Span (symbols):", self.span)
        layout.addWidget(ps_grp)

        # Derived stats
        stats_grp = QGroupBox("Derived Frame Statistics (auto-computed, read-only)")
        stats_layout = QVBoxLayout(stats_grp)
        self.stats_label = QLabel("—")
        self.stats_label.setStyleSheet(
            "font-family: monospace; font-size: 10px; "
            "background: #f8f8f8; padding: 4px; border: 1px solid #ccc;"
        )
        self.stats_label.setWordWrap(True)
        stats_layout.addWidget(self.stats_label)
        layout.addWidget(stats_grp)

        apply_btn = QPushButton("Apply Modulation Config")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)
        layout.addStretch()

    def _load_config(self, mod):
        self.mod_type.setCurrentText(mod.type)
        self.bit_rate.setValue(mod.bit_rate_bps)
        self.chip_rate.setValue(mod.chip_rate_sps)
        sp = mod.spreading
        self.code_type.setCurrentText(sp.code_type)
        self.code_length.setValue(sp.code_length)
        self.code_seed.setValue(sp.code_seed & (2**31 - 1))
        self.degree.setValue(sp.degree)
        self.poly1.setText(sp.poly1)
        self.poly2.setText(sp.poly2)
        if sp.custom_code:
            self.custom_code.setPlainText(",".join(str(x) for x in sp.custom_code))
        ps = mod.pulse_shaping
        self.filter_type.setCurrentText(ps.filter_type)
        self.rolloff.setValue(ps.rolloff)
        self.span.setValue(ps.span_symbols)

    def _refresh_derived(self, cfg):
        try:
            from core.config_manager import compute_frame_stats
            s = compute_frame_stats(cfg)
            self.stats_label.setText(
                f"Total chips/frame: {s['total_chips']:,}  "
                f"(preamble + {s['coded_bits']} coded bits × {cfg.modulation.spreading.code_length} chips/bit)\n"
                f"Burst duration:    {s['burst_ms']:.3f} ms  "
                f"(auto-set in BurstGate and FrameFeeder)\n"
                f"Frame period:      {s['period_ms']:.3f} ms  "
                f"(burst + {cfg.timing.transition_time_ms} ms guard)\n"
                f"RF bandwidth:      {s['bandwidth_hz']/1e6:.3f} MHz  "
                f"(symbol_rate × (1+α))\n"
                f"Samples/symbol:{s['sps']:.0f}  "
                f"(sample_rate / symbol_rate — must be integer)\n"
                f"Processing gain:   {s['proc_gain_db']:.1f} dB  "
                f"(10·log10({cfg.modulation.spreading.code_length}))\n"
                f"Payload throughput:{s['throughput_bps']:.0f} bps"
            )
        except Exception:
            pass

    def _parse_custom_code(self):
        txt = self.custom_code.toPlainText().strip()
        if not txt:
            return []
        return [int(x.strip()) for x in txt.split(",") if x.strip() in ("0", "1")]

    def _apply(self):
        self.config_changed.emit({
            "modulation": {
                "type": self.mod_type.currentText(),
                "bit_rate_bps": self.bit_rate.value(),
                "chip_rate_sps": self.chip_rate.value(),
                "spreading": {
                    "code_type": self.code_type.currentText(),
                    "code_length": self.code_length.value(),
                    "code_seed": self.code_seed.value(),
                    "degree": self.degree.value(),
                    "poly1": self.poly1.text() or "0x25",
                    "poly2": self.poly2.text() or "0x37",
                    "custom_code": self._parse_custom_code(),
                },
                "pulse_shaping": {
                    "filter_type": self.filter_type.currentText(),
                    "rolloff": self.rolloff.value(),
                    "span_symbols": self.span.value(),
                },
            }
        })
