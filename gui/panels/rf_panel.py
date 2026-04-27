"""RF Configuration Panel."""
from __future__ import annotations
from PyQt5.QtWidgets import (
    QWidget, QFormLayout, QLineEdit, QDoubleSpinBox, QComboBox, QGroupBox,
    QVBoxLayout, QPushButton, QLabel, QHBoxLayout, QCheckBox, QSpinBox
)
from PyQt5.QtCore import pyqtSignal


class RFPanel(QWidget):
    config_changed = pyqtSignal(dict)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._build_ui()
        self._load_config(cfg.rf)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Setup guide
        setup_grp = QGroupBox("Loopback Setup Guide")
        setup_layout = QVBoxLayout(setup_grp)
        guide = QLabel(
            "<b>RF0/TRX → RF1/TRX cable:</b><br>"
            "• Use a <b>20–30 dB SMA attenuator</b> inline — without it, TX "
            "overdrives the RX input at gain &gt; 10 dB.<br>"
            "• Set <b>tx_gain ≤ 10 dB</b> (or attenuator + 20–30 dB).<br>"
            "• Mode: <b>single_b210</b> — TX on RF0/TRX (ch 0), RX on RF1/TRX (ch 1).<br>"
            "• Start with <b>Hopping disabled</b> on the Hopping tab until RX frames appear."
        )
        guide.setWordWrap(True)
        guide.setStyleSheet("color: #555; font-size: 10px; padding: 2px;")
        setup_layout.addWidget(guide)
        layout.addWidget(setup_grp)

        # Hardware group
        hw_grp = QGroupBox("Hardware")
        hw_form = QFormLayout(hw_grp)
        self.hw_mode = QComboBox()
        self.hw_mode.addItems(["single_b210", "dual_b210", "dual_b205"])
        self.hw_mode.setToolTip(
            "single_b210: one B210 used for both TX and RX.\n"
            "  TX → RF0/TRX (Port A, ch 0)\n"
            "  RX → RF1/TRX (Port B, ch 1)\n"
            "  Both share internal TCXO — no external 10 MHz ref needed.\n"
            "  Cable: RF0/TRX → 20–30 dB attenuator → RF1/TRX\n\n"
            "dual_b210: two separate B210s.\n"
            "  TX on gnd_device ch 0, RX on msl_device ch 0.\n"
            "  For tighter timing: REF OUT of master → REF IN of slave,\n"
            "  set b210_ref_source=external on slave.\n\n"
            "dual_b205: two B205 mini (1T1R each).\n"
            "  TX on gnd_device, RX on msl_device.\n"
            "  No shared reference — relies on preamble timing recovery."
        )
        hw_form.addRow("Mode:", self.hw_mode)

        self.simulation = QCheckBox("Simulation Mode (no hardware needed)")
        self.simulation.setToolTip(
            "When enabled, the system uses ZMQ loopback instead of USRP hardware.\n"
            "Useful for testing logic, FEC, and protocols without a B210 connected."
        )
        hw_form.addRow("Sim:", self.simulation)

        self.gnd_device = QLineEdit()
        self.gnd_device.setPlaceholderText("auto (or serial=XXXXXXX)")
        self.gnd_device.setToolTip(
            "UHD device address for the ground/TX terminal.\n"
            "Format: 'serial=3218DC2' or leave as 'auto' to use the first found device.\n"
            "Click Auto-Detect to fill in discovered serial numbers."
        )
        hw_form.addRow("GND Device:", self.gnd_device)
        self.msl_device = QLineEdit()
        self.msl_device.setPlaceholderText("auto")
        self.msl_device.setToolTip(
            "UHD device address for the missile/RX terminal (dual modes only).\n"
            "In single_b210 mode this is ignored — both paths use gnd_device."
        )
        hw_form.addRow("MSL Device:", self.msl_device)
        self.b210_ref = QComboBox()
        self.b210_ref.addItems(["internal", "external"])
        self.b210_ref.setToolTip(
            "Clock reference source for the GND B210.\n"
            "internal: use the B210's own TCXO (default for single_b210 and dual master).\n"
            "external: lock to 10 MHz signal on REF IN SMA (slave in dual_b210 setup)."
        )
        hw_form.addRow("B210 Ref Source:", self.b210_ref)
        self.detect_btn = QPushButton("Auto-Detect Devices")
        self.detect_btn.clicked.connect(self._detect_devices)
        hw_form.addRow("", self.detect_btn)
        self.detected_label = QLabel("")
        hw_form.addRow("Found:", self.detected_label)

        port_guide = QLabel("<i style='color: #666;'>B210: Ch 0 = Port A, Ch 1 = Port B</i>")
        hw_form.addRow("", port_guide)

        self.tx_channel = QSpinBox()
        self.tx_channel.setRange(0, 1)
        self.tx_channel.setToolTip("USRP channel index (B210: 0=Port A, 1=Port B)")
        hw_form.addRow("TX Channel:", self.tx_channel)

        self.rx_channel = QSpinBox()
        self.rx_channel.setRange(0, 1)
        self.rx_channel.setToolTip("USRP channel index (B210: 0=Port A, 1=Port B)")
        hw_form.addRow("RX Channel:", self.rx_channel)

        self.tx_antenna = QComboBox()
        self.tx_antenna.addItems(["TX/RX"])
        self.tx_antenna.setEditable(True)
        self.tx_antenna.setToolTip("USRP TX antenna port. B210: 'TX/RX'")
        hw_form.addRow("TX Antenna:", self.tx_antenna)

        self.rx_antenna = QComboBox()
        self.rx_antenna.addItems(["RX2", "TX/RX"])
        self.rx_antenna.setEditable(True)
        self.rx_antenna.setToolTip("USRP RX antenna port. B210: 'RX2' (Port B) or 'TX/RX' (Port A)")
        hw_form.addRow("RX Antenna:", self.rx_antenna)

        layout.addWidget(hw_grp)

        # RF Parameters group
        rf_grp = QGroupBox("RF Parameters")
        rf_form = QFormLayout(rf_grp)
        self.center_freq = QDoubleSpinBox()
        self.center_freq.setRange(70e6, 6e9)
        self.center_freq.setSingleStep(1e6)
        self.center_freq.setDecimals(0)
        self.center_freq.setSuffix(" Hz")
        self.center_freq.setToolTip(
            "Starting carrier frequency in Hz.\n"
            "With hopping disabled, TX and RX both stay on this frequency.\n"
            "With hopping enabled, this is the center of the hop band.\n"
            "B210 range: 70 MHz – 6 GHz. 915 MHz (ISM band) is a good starting point."
        )
        rf_form.addRow("Center Frequency:", self.center_freq)
        self.sample_rate = QDoubleSpinBox()
        self.sample_rate.setRange(1e6, 61.44e6)
        self.sample_rate.setSingleStep(1e6)
        self.sample_rate.setDecimals(0)
        self.sample_rate.setSuffix(" Hz")
        self.sample_rate.setToolTip(
            "ADC/DAC sample rate in samples/second.\n"
            "Must be an integer multiple of chip_rate_sps (sps = sample_rate / chip_rate).\n"
            "8 MHz with 1 MHz chip rate → sps=8 (recommended minimum for M&M timing recovery).\n"
            "Higher sample rates give better timing resolution but increase CPU load.\n"
            "B210 max: ~61 MHz. Recommended: 8–25 MHz."
        )
        rf_form.addRow("Sample Rate:", self.sample_rate)
        self.tx_gain = QDoubleSpinBox()
        self.tx_gain.setRange(0, 89.8)
        self.tx_gain.setSingleStep(1.0)
        self.tx_gain.setSuffix(" dB")
        self.tx_gain.setToolTip(
            "TX output power gain in dB (0–89.8 dB for B210).\n"
            "Cable loopback: keep ≤ 10 dB with a 20–30 dB attenuator inline.\n"
            "Without attenuator: keep ≤ 5 dB to avoid overdriving RX input.\n"
            "Over the air: increase as needed for range, accounting for path loss."
        )
        rf_form.addRow("TX Gain:", self.tx_gain)
        self.rx_gain = QDoubleSpinBox()
        self.rx_gain.setRange(0, 76.0)
        self.rx_gain.setSingleStep(1.0)
        self.rx_gain.setSuffix(" dB")
        self.rx_gain.setToolTip(
            "RX front-end gain in dB (0–76 dB for B210).\n"
            "Cable loopback with 30 dB attenuator + 10 dB TX: RX gain 20–40 dB is typical.\n"
            "If signal is clipping (AGC maxed), reduce RX gain.\n"
            "If SNR is low, increase RX gain up to the noise floor."
        )
        rf_form.addRow("RX Gain:", self.rx_gain)
        self.tx_bw = QDoubleSpinBox()
        self.tx_bw.setRange(200e3, 56e6)
        self.tx_bw.setSingleStep(1e6)
        self.tx_bw.setDecimals(0)
        self.tx_bw.setSuffix(" Hz")
        self.tx_bw.setToolTip(
            "TX analog filter bandwidth in Hz (auto-computed: chip_rate × (1+rolloff)).\n"
            "This value is set automatically — to change it, adjust chip_rate or rolloff.\n"
            "Setting it narrower than chip_rate will clip the signal spectrum."
        )
        rf_form.addRow("TX Bandwidth (auto):", self.tx_bw)
        self.rx_bw = QDoubleSpinBox()
        self.rx_bw.setRange(200e3, 56e6)
        self.rx_bw.setSingleStep(1e6)
        self.rx_bw.setDecimals(0)
        self.rx_bw.setSuffix(" Hz")
        self.rx_bw.setToolTip(
            "RX analog filter bandwidth in Hz (auto-computed: chip_rate × (1+rolloff)).\n"
            "Matches TX bandwidth automatically. Increase if you want to capture\n"
            "interference outside the signal band for analysis."
        )
        rf_form.addRow("RX Bandwidth (auto):", self.rx_bw)
        layout.addWidget(rf_grp)

        apply_btn = QPushButton("Apply RF Settings")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)
        layout.addStretch()

    def _load_config(self, rf):
        self.hw_mode.setCurrentText(rf.hw_mode)
        self.simulation.setChecked(rf.simulation)
        self.gnd_device.setText(rf.gnd_device)
        self.msl_device.setText(rf.msl_device)
        self.b210_ref.setCurrentText(rf.b210_ref_source)
        self.tx_channel.setValue(rf.tx_channel)
        self.rx_channel.setValue(rf.rx_channel)
        self.tx_antenna.setCurrentText(rf.tx_antenna)
        self.rx_antenna.setCurrentText(rf.rx_antenna)
        self.center_freq.setValue(rf.center_frequency)
        self.sample_rate.setValue(rf.sample_rate)
        self.tx_gain.setValue(rf.tx_gain)
        self.rx_gain.setValue(rf.rx_gain)
        self.tx_bw.setValue(rf.tx_bandwidth)
        self.rx_bw.setValue(rf.rx_bandwidth)

    def _apply(self):
        self.config_changed.emit({
            "rf": {
                "hw_mode": self.hw_mode.currentText(),
                "simulation": self.simulation.isChecked(),
                "gnd_device": self.gnd_device.text() or "auto",
                "msl_device": self.msl_device.text() or "auto",
                "b210_ref_source": self.b210_ref.currentText(),
                "tx_channel": self.tx_channel.value(),
                "rx_channel": self.rx_channel.value(),
                "tx_antenna": self.tx_antenna.currentText(),
                "rx_antenna": self.rx_antenna.currentText(),
                "center_frequency": self.center_freq.value(),
                "sample_rate": self.sample_rate.value(),
                "tx_gain": self.tx_gain.value(),
                "rx_gain": self.rx_gain.value(),
                "tx_bandwidth": self.tx_bw.value(),
                "rx_bandwidth": self.rx_bw.value(),
            }
        })

    def _detect_devices(self):
        from core.config_manager import _find_uhd_devices
        devs = _find_uhd_devices()
        if devs:
            self.detected_label.setText(f"{len(devs)} device(s): " + " | ".join(devs[:3]))
            if len(devs) >= 1:
                self.gnd_device.setText(devs[0])
            if len(devs) >= 2:
                self.msl_device.setText(devs[1])
        else:
            self.detected_label.setText("No devices found")
