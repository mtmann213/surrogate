"""
Main Application Window.

Hosts all configuration panels in a QTabWidget.
Connects panel config_changed signals to the ConfigManager and FlowgraphManager.
"""
from __future__ import annotations

import logging
from pathlib import Path
from PyQt5.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QHBoxLayout, QPushButton,
    QStatusBar, QFileDialog, QMessageBox, QToolBar, QAction
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon

from core.config_manager import ConfigManager
from radio.flowgraph_manager import FlowgraphManager
from gui.panels.rf_panel import RFPanel
from gui.panels.hop_panel import HopPanel
from gui.panels.modulation_panel import ModulationPanel
from gui.panels.frame_panel import FramePanel
from gui.panels.timing_panel import TimingPanel
from gui.panels.anomaly_panel import AnomalyPanel
from gui.panels.logging_panel import LoggingPanel
from gui.panels.status_panel import StatusPanel

log = logging.getLogger(__name__)

_RESTART_REQUIRED_TOP_KEYS = {"rf", "modulation", "frame", "hopping"}


class MainWindow(QMainWindow):

    def __init__(self, config_manager: ConfigManager,
                 fg_manager: FlowgraphManager,
                 logger,
                 parent=None):
        super().__init__(parent)
        self._cm = config_manager
        self._fg = fg_manager
        self._logger = logger
        self._recorder = None

        self.setWindowTitle("Surrogate Datalink System")
        self.resize(900, 700)

        self._build_toolbar()
        self._build_tabs()
        self._build_statusbar()
        self._connect_signals()

    def _build_toolbar(self):
        tb = QToolBar("Main")
        self.addToolBar(tb)

        self._start_action = QAction("▶ Start", self)
        self._start_action.triggered.connect(self._start)
        tb.addAction(self._start_action)

        self._stop_action = QAction("■ Stop", self)
        self._stop_action.triggered.connect(self._stop)
        self._stop_action.setEnabled(False)
        tb.addAction(self._stop_action)

        tb.addSeparator()

        save_action = QAction("💾 Save Config", self)
        save_action.triggered.connect(self._save_config)
        tb.addAction(save_action)

        load_action = QAction("📂 Load Config", self)
        load_action.triggered.connect(self._load_config)
        tb.addAction(load_action)

        tb.addSeparator()

        self._record_action = QAction("⏺ Record IQ", self)
        self._record_action.setCheckable(True)
        self._record_action.triggered.connect(self._toggle_recording)
        tb.addAction(self._record_action)

    def _build_tabs(self):
        cfg = self._cm.config
        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)

        self._rf_panel = RFPanel(cfg)
        self._tabs.addTab(self._rf_panel, "RF")

        self._hop_panel = HopPanel(cfg)
        self._tabs.addTab(self._hop_panel, "Hopping")

        self._mod_panel = ModulationPanel(cfg)
        self._tabs.addTab(self._mod_panel, "Modulation")

        self._frame_panel = FramePanel(cfg)
        self._tabs.addTab(self._frame_panel, "Frame")

        self._timing_panel = TimingPanel(cfg)
        self._tabs.addTab(self._timing_panel, "Timing")

        self._anomaly_panel = AnomalyPanel(cfg)
        self._tabs.addTab(self._anomaly_panel, "Anomaly")

        self._log_panel = LoggingPanel(cfg)
        self._tabs.addTab(self._log_panel, "Logging")

        self._status_panel = StatusPanel(self._fg)
        self._tabs.addTab(self._status_panel, "Status")

    def _build_statusbar(self):
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("Ready")

    def _connect_signals(self):
        # All panels emit config_changed(dict) — route to config manager
        for panel in [self._rf_panel, self._hop_panel, self._mod_panel,
                      self._frame_panel, self._timing_panel, self._anomaly_panel,
                      self._log_panel]:
            panel.config_changed.connect(self._on_config_changed)

        # Anomaly live updates (no restart)
        self._anomaly_panel.anomaly_live_update.connect(
            lambda d: self._fg.set_anomaly_state(d)
        )

        # IQ recording buttons
        self._log_panel.recording_start.connect(self._start_recording)
        self._log_panel.recording_stop.connect(self._stop_recording)

        # Status panel restart button
        self._status_panel.restart_requested.connect(self._restart_flowgraphs)

        # RX frame callback for status (Qt signal for thread safety)
        self._fg.signals.frame_received.connect(self._on_rx_frame)
        self._fg.signals.tx_transmitted.connect(self._on_tx_transmitted)

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------

    def _start(self):
        try:
            self._fg.start()
            self._start_action.setEnabled(False)
            self._stop_action.setEnabled(True)
            self._statusbar.showMessage("Running")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to start: {exc}")
            log.exception("Start failed")

    def _stop(self):
        self._fg.stop()
        self._start_action.setEnabled(True)
        self._stop_action.setEnabled(False)
        self._statusbar.showMessage("Stopped")

    def _save_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Configuration", "config/", "YAML Files (*.yaml *.yml)"
        )
        if path:
            self._cm.save(path)
            self._statusbar.showMessage(f"Config saved: {path}")

    def _load_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Configuration", "config/", "YAML Files (*.yaml *.yml)"
        )
        if path:
            self._cm._path = Path(path)
            self._cm.reload()
            self._statusbar.showMessage(f"Config loaded: {path}")
            QMessageBox.information(self, "Config Loaded",
                                    "Restart flowgraphs to apply all changes.")

    # ------------------------------------------------------------------
    # Config change routing
    # ------------------------------------------------------------------

    def _on_config_changed(self, updates: dict):
        self._cm.update(updates)
        cfg = self._cm.config
        # Refresh derived stats displays whenever anything changes
        self._timing_panel._refresh_derived(cfg)
        self._mod_panel._refresh_derived(cfg)
        # Check if restart is needed
        top_keys = set(updates.keys())
        needs_restart = bool(top_keys & _RESTART_REQUIRED_TOP_KEYS)
        if needs_restart:
            if self._fg._running:
                self._statusbar.showMessage("Config changed — auto-restarting flowgraphs...", 3000)
                self._restart_flowgraphs()
            else:
                self._statusbar.showMessage(
                    "Config changed — restart required before next start", 5000
                )
        else:
            self._statusbar.showMessage("Config updated (live)", 3000)

    # ------------------------------------------------------------------
    # IQ Recording
    # ------------------------------------------------------------------

    def _start_recording(self):
        from radio.iq_recorder import IQRecorder
        cfg = self._cm.config
        rec_cfg = cfg.logging.iq_recording
        self._recorder = IQRecorder(rec_cfg, cfg.rf.sample_rate, cfg.rf.center_frequency)
        tx_sink = self._recorder.build_tx_sink()
        rx_sink = self._recorder.build_rx_sink()
        if tx_sink and self._fg._tx_fg:
            self._fg._tx_fg.enable_iq_recording(tx_sink)
        if rx_sink and self._fg._rx_fg:
            self._fg._rx_fg.enable_iq_recording(rx_sink)
        self._recorder.start()
        self._statusbar.showMessage("IQ Recording started")

    def _stop_recording(self):
        if self._recorder:
            if self._fg._tx_fg:
                self._fg._tx_fg.disable_iq_recording()
            if self._fg._rx_fg:
                self._fg._rx_fg.disable_iq_recording()
            self._recorder.stop()
            self._recorder = None
        self._statusbar.showMessage("IQ Recording stopped")

    # ------------------------------------------------------------------
    # Frame callback
    # ------------------------------------------------------------------

    def _on_rx_frame(self, payload, timestamp, snr, fec_ok):
        payload_str = payload.hex()[:16] + "..." if payload else "None"
        status = f"RX Frame | SNR={snr:.1f}dB | FEC={'OK' if fec_ok else 'ERR'} | {payload_str}"
        self._statusbar.showMessage(status, 3000)

    def _on_tx_transmitted(self, frame_id, payload, timestamp):
        # Placeholder for TX-related GUI updates
        pass

    def _toggle_recording(self, checked):
        if checked:
            self._start_recording()
            self._record_action.setText("⏹ Stop Rec")
        else:
            self._stop_recording()
            self._record_action.setText("⏺ Record IQ")

    def _restart_flowgraphs(self):
        self._fg.restart()
        self._statusbar.showMessage("Flowgraphs restarted")

    def closeEvent(self, event):
        self._fg.stop()
        event.accept()
