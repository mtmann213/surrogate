"""Frame Structure and FEC Configuration Panel."""
from __future__ import annotations
from PyQt5.QtWidgets import (
    QWidget, QFormLayout, QDoubleSpinBox, QComboBox, QGroupBox,
    QVBoxLayout, QPushButton, QLabel, QLineEdit, QSpinBox,
    QCheckBox, QHBoxLayout
)
from PyQt5.QtCore import pyqtSignal


class FramePanel(QWidget):
    config_changed = pyqtSignal(dict)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_config(cfg.frame)
        self._update_payload_label()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Frame structure
        fs_grp = QGroupBox("Frame Structure")
        fs_form = QFormLayout(fs_grp)

        self.total_bits = QSpinBox()
        self.total_bits.setRange(64, 4096)
        self.total_bits.valueChanged.connect(self._update_payload_label)
        self.total_bits.setToolTip(
            "Total information bits per frame before FEC encoding and spreading.\n"
            "= preamble_bits + invariant_bits + payload_bits\n"
            "Default: 300 bits = 32 preamble + 16 invariant + 252 payload (31.5 bytes)\n"
            "After FEC (rate 1/2): 548 coded bits → 16,988 chips at code_length=31."
        )
        fs_form.addRow("Total Bits:", self.total_bits)

        self.pre_bits = QSpinBox()
        self.pre_bits.setRange(8, 256)
        self.pre_bits.valueChanged.connect(self._update_payload_label)
        self.pre_bits.setToolTip(
            "Number of preamble bits transmitted WITHOUT FEC or spreading.\n"
            "The preamble is raw BPSK — used by the RX for burst detection and\n"
            "timing recovery via sliding correlation.\n"
            "Longer preamble → easier detection but less payload capacity.\n"
            "32 bits is the recommended minimum for reliable correlation."
        )
        fs_form.addRow("Preamble Bits:", self.pre_bits)

        self.pre_pattern = QLineEdit()
        self.pre_pattern.setPlaceholderText("alternating  or  0xF0F0F0F0")
        self.pre_pattern.setToolTip(
            "Bit pattern of the preamble.\n"
            "'alternating' → 10101010...10 (maximum transitions, best for timing recovery)\n"
            "hex string → e.g. '0xF0F0F0F0' for a specific bit pattern\n"
            "TX and RX must use the same pattern for preamble correlation to work."
        )
        fs_form.addRow("Preamble Pattern:", self.pre_pattern)

        self.inv_enable = QCheckBox("Enabled")
        self.inv_enable.stateChanged.connect(self._update_payload_label)
        self.inv_enable.setToolTip(
            "Adds a fixed-pattern field between preamble and payload (FEC-encoded+spread).\n"
            "Used as a secondary sync marker in FrameSink — helps align the bit stream\n"
            "after despreading. Disable to increase payload capacity by inv_bits."
        )
        fs_form.addRow("Invariant Section:", self.inv_enable)

        self.inv_bits = QSpinBox()
        self.inv_bits.setRange(0, 256)
        self.inv_bits.valueChanged.connect(self._update_payload_label)
        self.inv_bits.setToolTip(
            "Size of the invariant (fixed-pattern) section in bits.\n"
            "These bits are FEC-encoded and spread along with the payload.\n"
            "Must be a multiple of 8 for clean byte alignment."
        )
        fs_form.addRow("Invariant Bits:", self.inv_bits)

        self.inv_pattern = QLineEdit()
        self.inv_pattern.setPlaceholderText("0x1234")
        self.inv_pattern.setToolTip(
            "Fixed bit pattern for the invariant section (hex string).\n"
            "The RX uses this known pattern as an additional sync/sanity check.\n"
            "TX and RX must use the same pattern."
        )
        fs_form.addRow("Invariant Pattern:", self.inv_pattern)

        self.payload_label = QLabel("Payload: -- bits")
        self.payload_label.setToolTip(
            "Derived payload size = total_bits - preamble_bits - invariant_bits.\n"
            "This is the actual data capacity per burst before FEC overhead."
        )
        fs_form.addRow("Payload (derived):", self.payload_label)

        self.payload_hex = QLineEdit()
        self.payload_hex.setPlaceholderText("e.g. 00010203... (leave empty for default)")
        self.payload_hex.setToolTip(
            "Optional custom payload in hex. If empty, the system transmits a repeating 0x00-0xFF pattern.\n"
            "Data will be truncated or zero-padded to fit the derived payload size."
        )
        fs_form.addRow("Custom Payload (hex):", self.payload_hex)

        layout.addWidget(fs_grp)

        # FEC
        fec_grp = QGroupBox("FEC (Forward Error Correction)")
        fec_form = QFormLayout(fec_grp)

        self.fec_enable = QCheckBox("Enable FEC")
        self.fec_enable.setToolTip(
            "Enable forward error correction coding.\n"
            "Adds redundancy bits so the RX can correct transmission errors.\n"
            "Rate 1/2 CC roughly doubles the chip count but provides ~5–8 dB coding gain."
        )
        fec_form.addRow(self.fec_enable)

        self.fec_primary = QComboBox()
        self.fec_primary.addItems(["cc", "none"])
        self.fec_primary.setToolTip(
            "cc:   Convolutional Code — standard K=7 rate-1/2 Viterbi decoder.\n"
            "      NASA/CCSDS standard polynomials [121, 91] by default.\n"
            "none: No FEC — raw bits transmitted. Useful for debugging."
        )
        fec_form.addRow("Primary FEC:", self.fec_primary)

        self.fec_outer = QComboBox()
        self.fec_outer.addItems(["none", "rs"])
        self.fec_outer.setToolTip(
            "none: Primary FEC only.\n"
            "rs:   Reed-Solomon outer code applied BEFORE the CC inner code.\n"
            "      RS handles burst errors; CC handles random errors.\n"
            "      Adds RS ECC Symbols × 8 bits overhead per frame."
        )
        fec_form.addRow("Outer Code (RS):", self.fec_outer)

        self.cc_rate = QSpinBox()
        self.cc_rate.setRange(2, 4)
        self.cc_rate.setPrefix("1/")
        self.cc_rate.setToolTip(
            "Convolutional code rate as 1/N (rate_inv = N).\n"
            "1/2: each input bit produces 2 coded bits (default, ~5 dB gain).\n"
            "1/3: each input bit produces 3 coded bits (more redundancy, 3× chips)."
        )
        fec_form.addRow("CC Rate:", self.cc_rate)

        self.cc_k = QSpinBox()
        self.cc_k.setRange(3, 9)
        self.cc_k.setToolTip(
            "Convolutional code constraint length K.\n"
            "K=7 is the standard NASA/CCSDS choice — good performance, practical complexity.\n"
            "Viterbi decoder complexity: 2^K states. K=9 gives ~1 dB more gain but is 4× slower."
        )
        fec_form.addRow("CC Constraint K:", self.cc_k)

        self.cc_polys = QLineEdit()
        self.cc_polys.setPlaceholderText("121, 91")
        self.cc_polys.setToolTip(
            "Generator polynomials for the convolutional encoder (comma-separated decimal).\n"
            "NASA/CCSDS K=7 standard: 121 (octal 171 = x⁷+x⁶+x⁴+x³+1)\n"
            "                          91 (octal 133 = x⁷+x⁵+x⁴+x³+1)\n"
            "One polynomial per output stream; count must equal rate_inv."
        )
        fec_form.addRow("CC Polynomials:", self.cc_polys)

        self.rs_nsym = QSpinBox()
        self.rs_nsym.setRange(2, 32)
        self.rs_nsym.setToolTip(
            "Number of Reed-Solomon ECC symbols (bytes) per RS block.\n"
            "RS can correct up to nsym/2 erased bytes or nsym/2 symbol errors.\n"
            "nsym=16: corrects up to 8 bytes of errors per block (+128 bits overhead)."
        )
        fec_form.addRow("RS ECC Symbols:", self.rs_nsym)
        layout.addWidget(fec_grp)

        apply_btn = QPushButton("Apply Frame Config")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)
        layout.addStretch()

    def _load_config(self, frame):
        self.total_bits.setValue(frame.total_bits)
        self.payload_hex.setText(frame.payload_hex)
        self.pre_bits.setValue(frame.preamble.length_bits)
        self.pre_pattern.setText(frame.preamble.pattern)
        self.inv_enable.setChecked(frame.invariant.enabled)
        self.inv_bits.setValue(frame.invariant.length_bits)
        self.inv_pattern.setText(frame.invariant.pattern)
        fec = frame.fec
        self.fec_enable.setChecked(fec.enabled)
        self.fec_primary.setCurrentText(fec.primary_type)
        self.fec_outer.setCurrentText(fec.outer_type)
        self.cc_rate.setValue(fec.cc.rate_inv)
        self.cc_k.setValue(fec.cc.constraint_length)
        self.cc_polys.setText(", ".join(str(p) for p in fec.cc.polynomials))
        self.rs_nsym.setValue(fec.rs.nsym)

    def _update_payload_label(self):
        total = self.total_bits.value()
        pre = self.pre_bits.value()
        inv = self.inv_bits.value() if self.inv_enable.isChecked() else 0
        payload = max(0, total - pre - inv)
        self.payload_label.setText(f"Payload: {payload} bits ({payload // 8} bytes)")

    def _apply(self):
        polys_text = self.cc_polys.text()
        try:
            polys = [int(p.strip()) for p in polys_text.split(",")]
        except ValueError:
            polys = [121, 91]

        self.config_changed.emit({
            "frame": {
                "total_bits": self.total_bits.value(),
                "payload_hex": self.payload_hex.text().strip(),
                "preamble": {
                    "length_bits": self.pre_bits.value(),
                    "pattern": self.pre_pattern.text() or "alternating",
                },
                "invariant": {
                    "enabled": self.inv_enable.isChecked(),
                    "length_bits": self.inv_bits.value(),
                    "pattern": self.inv_pattern.text() or "0x1234",
                },
                "fec": {
                    "enabled": self.fec_enable.isChecked(),
                    "primary_type": self.fec_primary.currentText(),
                    "outer_type": self.fec_outer.currentText(),
                    "cc": {
                        "rate_inv": self.cc_rate.value(),
                        "constraint_length": self.cc_k.value(),
                        "polynomials": polys,
                    },
                    "rs": {
                        "nsym": self.rs_nsym.value(),
                    },
                },
            }
        })
