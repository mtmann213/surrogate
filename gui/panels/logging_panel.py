"""Logging and IQ Recording Configuration Panel."""
from __future__ import annotations
from PyQt5.QtWidgets import (
    QWidget, QFormLayout, QGroupBox, QVBoxLayout, QPushButton,
    QCheckBox, QComboBox, QLineEdit, QFileDialog, QLabel, QHBoxLayout
)
from PyQt5.QtCore import pyqtSignal


class LoggingPanel(QWidget):
    config_changed = pyqtSignal(dict)
    recording_start = pyqtSignal()
    recording_stop = pyqtSignal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_config(cfg.logging)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Text logging
        log_grp = QGroupBox("Text Logging")
        log_form = QFormLayout(log_grp)
        self.log_enable = QCheckBox("Enabled")
        log_form.addRow(self.log_enable)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        log_form.addRow("Log Level:", self.log_level)
        log_file_row = QHBoxLayout()
        self.log_file = QLineEdit()
        browse_log = QPushButton("Browse")
        browse_log.clicked.connect(lambda: self._browse_save(self.log_file, "Log Files (*.log *.txt)"))
        log_file_row.addWidget(self.log_file)
        log_file_row.addWidget(browse_log)
        log_form.addRow("Log File:", log_file_row)
        self.log_tx = QCheckBox("Log TX Frames")
        log_form.addRow(self.log_tx)
        self.log_timing = QCheckBox("Log Timing / Hops")
        log_form.addRow(self.log_timing)
        layout.addWidget(log_grp)

        # CSV
        csv_grp = QGroupBox("CSV Frame Log")
        csv_form = QFormLayout(csv_grp)
        self.csv_enable = QCheckBox("Enabled")
        csv_form.addRow(self.csv_enable)
        csv_row = QHBoxLayout()
        self.csv_file = QLineEdit()
        browse_csv = QPushButton("Browse")
        browse_csv.clicked.connect(lambda: self._browse_save(self.csv_file, "CSV Files (*.csv)"))
        csv_row.addWidget(self.csv_file)
        csv_row.addWidget(browse_csv)
        csv_form.addRow("CSV File:", csv_row)
        layout.addWidget(csv_grp)

        # IQ Recording
        iq_grp = QGroupBox("IQ Recording")
        iq_form = QFormLayout(iq_grp)
        self.iq_enable = QCheckBox("Enabled")
        iq_form.addRow(self.iq_enable)
        self.iq_format = QComboBox()
        self.iq_format.addItems(["sigmf", "cf32", "i8"])
        iq_form.addRow("Format:", self.iq_format)
        iq_dir_row = QHBoxLayout()
        self.iq_output_path = QLineEdit()
        browse_iq = QPushButton("Browse")
        browse_iq.clicked.connect(self._browse_dir)
        iq_dir_row.addWidget(self.iq_output_path)
        iq_dir_row.addWidget(browse_iq)
        iq_form.addRow("Output Directory:", iq_dir_row)
        self.record_tx = QCheckBox("Record TX")
        self.record_rx = QCheckBox("Record RX")
        iq_form.addRow(self.record_tx)
        iq_form.addRow(self.record_rx)
        rec_btns = QHBoxLayout()
        self.start_rec_btn = QPushButton("Start Recording")
        self.stop_rec_btn = QPushButton("Stop Recording")
        self.stop_rec_btn.setEnabled(False)
        self.start_rec_btn.clicked.connect(self._start_recording)
        self.stop_rec_btn.clicked.connect(self._stop_recording)
        rec_btns.addWidget(self.start_rec_btn)
        rec_btns.addWidget(self.stop_rec_btn)
        iq_form.addRow(rec_btns)
        self.rec_status = QLabel("Not recording")
        iq_form.addRow("Status:", self.rec_status)
        layout.addWidget(iq_grp)

        apply_btn = QPushButton("Apply Logging Config")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)
        layout.addStretch()

    def _load_config(self, log):
        self.log_enable.setChecked(log.enabled)
        self.log_level.setCurrentText(log.log_level)
        self.log_file.setText(log.log_file)
        self.log_tx.setChecked(log.log_transmission)
        self.log_timing.setChecked(log.log_timing)
        self.csv_enable.setChecked(log.csv.enabled)
        self.csv_file.setText(log.csv.file)
        self.iq_enable.setChecked(log.iq_recording.enabled)
        self.iq_format.setCurrentText(log.iq_recording.format)
        self.iq_output_path.setText(log.iq_recording.output_path)
        self.record_tx.setChecked(log.iq_recording.record_tx)
        self.record_rx.setChecked(log.iq_recording.record_rx)

    def _browse_save(self, widget: QLineEdit, filter_str: str):
        path, _ = QFileDialog.getSaveFileName(self, "Select File", "", filter_str)
        if path:
            widget.setText(path)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if d:
            self.iq_output_path.setText(d + "/")

    def _start_recording(self):
        self.start_rec_btn.setEnabled(False)
        self.stop_rec_btn.setEnabled(True)
        self.rec_status.setText("Recording...")
        self.recording_start.emit()

    def _stop_recording(self):
        self.start_rec_btn.setEnabled(True)
        self.stop_rec_btn.setEnabled(False)
        self.rec_status.setText("Stopped")
        self.recording_stop.emit()

    def _apply(self):
        self.config_changed.emit({
            "logging": {
                "enabled": self.log_enable.isChecked(),
                "log_level": self.log_level.currentText(),
                "log_file": self.log_file.text(),
                "log_transmission": self.log_tx.isChecked(),
                "log_timing": self.log_timing.isChecked(),
                "csv": {
                    "enabled": self.csv_enable.isChecked(),
                    "file": self.csv_file.text(),
                },
                "iq_recording": {
                    "enabled": self.iq_enable.isChecked(),
                    "format": self.iq_format.currentText(),
                    "output_path": self.iq_output_path.text(),
                    "record_tx": self.record_tx.isChecked(),
                    "record_rx": self.record_rx.isChecked(),
                },
            }
        })
