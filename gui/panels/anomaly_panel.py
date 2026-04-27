"""Anomaly Injection Panel."""
from __future__ import annotations
from PyQt5.QtWidgets import (
    QWidget, QFormLayout, QDoubleSpinBox, QGroupBox,
    QVBoxLayout, QPushButton, QCheckBox, QComboBox,
    QLineEdit, QLabel, QHBoxLayout, QFileDialog
)
from PyQt5.QtCore import pyqtSignal


class AnomalyPanel(QWidget):
    config_changed = pyqtSignal(dict)
    anomaly_live_update = pyqtSignal(dict)   # for no-restart live updates

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_config(cfg.anomaly)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.master_enable = QCheckBox("Enable Anomaly Injection")
        layout.addWidget(self.master_enable)

        # BER
        ber_grp = QGroupBox("Bit Error Injection")
        ber_form = QFormLayout(ber_grp)
        self.ber = QDoubleSpinBox()
        self.ber.setRange(0.0, 0.5)
        self.ber.setSingleStep(0.001)
        self.ber.setDecimals(4)
        ber_form.addRow("BER:", self.ber)
        self.ber.valueChanged.connect(lambda v: self.anomaly_live_update.emit({"ber_injection": v}))
        layout.addWidget(ber_grp)

        # CFO
        cfo_grp = QGroupBox("Carrier Frequency Offset")
        cfo_form = QFormLayout(cfo_grp)
        self.cfo = QDoubleSpinBox()
        self.cfo.setRange(-500000, 500000)
        self.cfo.setSingleStep(100)
        self.cfo.setDecimals(1)
        self.cfo.setSuffix(" Hz")
        cfo_form.addRow("CFO:", self.cfo)
        self.cfo.valueChanged.connect(lambda v: self.anomaly_live_update.emit({"carrier_freq_offset_hz": v}))
        layout.addWidget(cfo_grp)

        # Burst dropout
        drop_grp = QGroupBox("Burst Dropout")
        drop_form = QFormLayout(drop_grp)
        self.dropout_en = QCheckBox("Enable")
        drop_form.addRow(self.dropout_en)
        self.dropout_prob = QDoubleSpinBox()
        self.dropout_prob.setRange(0.0, 1.0)
        self.dropout_prob.setSingleStep(0.01)
        self.dropout_prob.setDecimals(3)
        drop_form.addRow("Probability:", self.dropout_prob)
        layout.addWidget(drop_grp)

        # Power fade
        fade_grp = QGroupBox("Power Fade")
        fade_form = QFormLayout(fade_grp)
        self.fade_en = QCheckBox("Enable")
        fade_form.addRow(self.fade_en)
        self.fade_depth = QDoubleSpinBox()
        self.fade_depth.setRange(0, 60)
        self.fade_depth.setSuffix(" dB")
        fade_form.addRow("Fade Depth:", self.fade_depth)
        self.fade_rate = QDoubleSpinBox()
        self.fade_rate.setRange(0.1, 100)
        self.fade_rate.setDecimals(1)
        self.fade_rate.setSuffix(" Hz")
        fade_form.addRow("Fade Rate:", self.fade_rate)
        layout.addWidget(fade_grp)

        # IQ Imbalance
        iq_grp = QGroupBox("I/Q Imbalance")
        iq_form = QFormLayout(iq_grp)
        self.iq_en = QCheckBox("Enable")
        iq_form.addRow(self.iq_en)
        self.iq_amp = QDoubleSpinBox()
        self.iq_amp.setRange(-10, 10)
        self.iq_amp.setDecimals(2)
        self.iq_amp.setSuffix(" dB")
        iq_form.addRow("Amplitude Imbalance:", self.iq_amp)
        self.iq_phase = QDoubleSpinBox()
        self.iq_phase.setRange(-30, 30)
        self.iq_phase.setDecimals(2)
        self.iq_phase.setSuffix(" °")
        iq_form.addRow("Phase Imbalance:", self.iq_phase)
        layout.addWidget(iq_grp)

        # Interference
        int_grp = QGroupBox("Interference Injection")
        int_form = QFormLayout(int_grp)
        self.int_en = QCheckBox("Enable")
        int_form.addRow(self.int_en)
        self.int_type = QComboBox()
        self.int_type.addItems(["awgn", "tone", "file", "zmq_stream"])
        int_form.addRow("Source Type:", self.int_type)
        file_row = QHBoxLayout()
        self.int_file = QLineEdit()
        self.int_file.setPlaceholderText("Path to IQ file (.cf32, .bin)")
        file_btn = QPushButton("Browse")
        file_btn.clicked.connect(self._browse_file)
        file_row.addWidget(self.int_file)
        file_row.addWidget(file_btn)
        int_form.addRow("File Path:", file_row)
        self.int_zmq = QLineEdit()
        self.int_zmq.setPlaceholderText("tcp://127.0.0.1:5555")
        int_form.addRow("ZMQ Address:", self.int_zmq)
        self.int_tone_freq = QDoubleSpinBox()
        self.int_tone_freq.setRange(-5e6, 5e6)
        self.int_tone_freq.setDecimals(1)
        self.int_tone_freq.setSuffix(" Hz offset")
        int_form.addRow("Tone Frequency:", self.int_tone_freq)
        self.int_power = QDoubleSpinBox()
        self.int_power.setRange(-60, 0)
        self.int_power.setSingleStep(1)
        self.int_power.setDecimals(1)
        self.int_power.setSuffix(" dBr")
        self.int_power.valueChanged.connect(
            lambda v: self.anomaly_live_update.emit({"interference": {"relative_power_db": v}})
        )
        int_form.addRow("Relative Power:", self.int_power)
        layout.addWidget(int_grp)

        # Extra timing jitter
        jit_grp = QGroupBox("Extra Timing Jitter")
        jit_form = QFormLayout(jit_grp)
        self.extra_jitter = QDoubleSpinBox()
        self.extra_jitter.setRange(0, 500)
        self.extra_jitter.setDecimals(1)
        self.extra_jitter.setSuffix(" µs σ")
        jit_form.addRow("Extra Jitter:", self.extra_jitter)
        layout.addWidget(jit_grp)

        apply_btn = QPushButton("Apply Anomaly Config")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)
        layout.addStretch()

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select IQ File", "",
                                               "IQ Files (*.cf32 *.bin *.sigmf-data);;All Files (*)")
        if path:
            self.int_file.setText(path)

    def _load_config(self, a):
        self.master_enable.setChecked(a.enabled)
        self.ber.setValue(a.ber_injection)
        self.cfo.setValue(a.carrier_freq_offset_hz)
        self.dropout_en.setChecked(a.burst_dropout.enabled)
        self.dropout_prob.setValue(a.burst_dropout.dropout_probability)
        self.fade_en.setChecked(a.power_fade.enabled)
        self.fade_depth.setValue(a.power_fade.fade_depth_db)
        self.fade_rate.setValue(a.power_fade.fade_rate_hz)
        self.iq_en.setChecked(a.iq_imbalance.enabled)
        self.iq_amp.setValue(a.iq_imbalance.amplitude_db)
        self.iq_phase.setValue(a.iq_imbalance.phase_deg)
        self.int_en.setChecked(a.interference.enabled)
        self.int_type.setCurrentText(a.interference.source_type)
        self.int_file.setText(a.interference.file_path)
        self.int_zmq.setText(a.interference.zmq_address)
        self.int_tone_freq.setValue(a.interference.tone_freq_hz)
        self.int_power.setValue(a.interference.relative_power_db)
        self.extra_jitter.setValue(a.extra_timing_jitter_us)

    def _apply(self):
        self.config_changed.emit({
            "anomaly": {
                "enabled": self.master_enable.isChecked(),
                "ber_injection": self.ber.value(),
                "carrier_freq_offset_hz": self.cfo.value(),
                "extra_timing_jitter_us": self.extra_jitter.value(),
                "burst_dropout": {
                    "enabled": self.dropout_en.isChecked(),
                    "dropout_probability": self.dropout_prob.value(),
                },
                "power_fade": {
                    "enabled": self.fade_en.isChecked(),
                    "fade_depth_db": self.fade_depth.value(),
                    "fade_rate_hz": self.fade_rate.value(),
                },
                "iq_imbalance": {
                    "enabled": self.iq_en.isChecked(),
                    "amplitude_db": self.iq_amp.value(),
                    "phase_deg": self.iq_phase.value(),
                },
                "interference": {
                    "enabled": self.int_en.isChecked(),
                    "source_type": self.int_type.currentText(),
                    "file_path": self.int_file.text(),
                    "zmq_address": self.int_zmq.text() or "tcp://127.0.0.1:5555",
                    "tone_freq_hz": self.int_tone_freq.value(),
                    "relative_power_db": self.int_power.value(),
                },
            }
        })
