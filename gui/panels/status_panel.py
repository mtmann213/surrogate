"""
Status and Monitoring Panel.

Polls FlowgraphManager.get_stats() at 2 Hz and updates all widgets.
"""
from __future__ import annotations

import time
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLabel, QProgressBar, QPushButton, QTextEdit, QSizePolicy,
    QGridLayout, QFrame
)
from PyQt5.QtCore import QTimer, pyqtSignal, Qt
from PyQt5.QtGui import QFont, QColor, QPalette


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------

class LEDIndicator(QLabel):
    _COLORS = {
        "green":  "#00e000",
        "yellow": "#e0e000",
        "red":    "#e00000",
        "gray":   "#606060",
    }

    def __init__(self, size: int = 14, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.set_color("gray")

    def set_color(self, name: str) -> None:
        color = self._COLORS.get(name, self._COLORS["gray"])
        r = size = self.width() // 2
        self.setStyleSheet(
            f"background-color:{color}; border-radius:{r}px; border:1px solid #222;"
        )


class ValueLabel(QLabel):
    """Bold value label with optional colour feedback."""
    def __init__(self, text: str = "—", parent=None):
        super().__init__(text, parent)
        f = self.font()
        f.setBold(True)
        self.setFont(f)

    def set_value(self, text: str, color: str = "") -> None:
        self.setText(text)
        if color:
            self.setStyleSheet(f"color:{color};")
        else:
            self.setStyleSheet("")


class MiniBar(QProgressBar):
    def __init__(self, lo: int, hi: int, parent=None):
        super().__init__(parent)
        self.setRange(lo, hi)
        self.setTextVisible(False)
        self.setFixedHeight(10)
        self.setMaximumWidth(160)


# ---------------------------------------------------------------------------
# Main panel
# ---------------------------------------------------------------------------

class StatusPanel(QWidget):
    restart_requested = pyqtSignal()

    def __init__(self, fg_manager, parent=None):
        super().__init__(parent)
        self._fg = fg_manager
        self._start_time = time.time()
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(500)   # 2 Hz

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        root.addWidget(self._make_link_state_group())
        root.addWidget(self._make_traffic_group())
        root.addWidget(self._make_rf_group())
        root.addWidget(self._make_hop_group())
        root.addWidget(self._make_events_group())

        btn_row = QHBoxLayout()
        self._restart_btn = QPushButton("Restart Flowgraphs")
        self._restart_btn.clicked.connect(self.restart_requested)
        btn_row.addWidget(self._restart_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

    def _make_link_state_group(self) -> QGroupBox:
        grp = QGroupBox("Link State")
        row = QHBoxLayout(grp)

        self._link_led = LEDIndicator(18)
        row.addWidget(self._link_led)

        self._link_label = ValueLabel("Stopped")
        self._link_label.setFixedWidth(80)
        row.addWidget(self._link_label)

        row.addSpacing(16)
        row.addWidget(QLabel("Elapsed:"))
        self._elapsed_label = ValueLabel("0:00:00")
        row.addWidget(self._elapsed_label)
        row.addStretch()
        return grp

    def _make_traffic_group(self) -> QGroupBox:
        grp = QGroupBox("Traffic")
        form = QFormLayout(grp)

        # TX row
        tx_row = QHBoxLayout()
        self._tx_count = ValueLabel("0")
        self._tx_rate = QLabel("0.0 pkt/s")
        tx_row.addWidget(self._tx_count)
        tx_row.addWidget(self._tx_rate)
        tx_row.addStretch()
        form.addRow("TX Frames:", tx_row)

        # RX row
        rx_row = QHBoxLayout()
        self._rx_count = ValueLabel("0")
        self._rx_rate = QLabel("0.0 pkt/s")
        rx_row.addWidget(self._rx_count)
        rx_row.addWidget(self._rx_rate)
        rx_row.addStretch()
        form.addRow("RX Frames:", rx_row)

        # Packet delivery
        pdr_row = QHBoxLayout()
        self._pdr_label = ValueLabel("—")
        self._pdr_bar = MiniBar(0, 100)
        pdr_row.addWidget(self._pdr_label)
        pdr_row.addWidget(self._pdr_bar)
        pdr_row.addStretch()
        form.addRow("Delivery:", pdr_row)

        # Detection rate (% of TX frames detected by RX)
        detect_row = QHBoxLayout()
        self._detect_label = ValueLabel("—")
        self._detect_bar = MiniBar(0, 100)
        detect_row.addWidget(self._detect_label)
        detect_row.addWidget(self._detect_bar)
        detect_row.addStretch()
        form.addRow("Detect:", detect_row)

        # Decode rate (% of detected frames with FEC OK)
        decode_row = QHBoxLayout()
        self._decode_label = ValueLabel("—")
        self._decode_bar = MiniBar(0, 100)
        decode_row.addWidget(self._decode_label)
        decode_row.addWidget(self._decode_bar)
        decode_row.addStretch()
        form.addRow("Decode:", decode_row)

        # FEC
        fec_row = QHBoxLayout()
        self._fec_ok_label = QLabel("0 OK")
        self._fec_err_label = ValueLabel("0 ERR")
        fec_row.addWidget(self._fec_ok_label)
        fec_row.addWidget(QLabel(" / "))
        fec_row.addWidget(self._fec_err_label)
        fec_row.addStretch()
        form.addRow("FEC:", fec_row)

        return grp

    def _make_rf_group(self) -> QGroupBox:
        grp = QGroupBox("RF Quality")
        form = QFormLayout(grp)

        snr_row = QHBoxLayout()
        self._snr_label = ValueLabel("— dB")
        self._snr_bar = MiniBar(0, 40)
        snr_row.addWidget(self._snr_label)
        snr_row.addWidget(self._snr_bar)
        snr_row.addStretch()
        form.addRow("SNR (est.):", snr_row)

        self._snr_avg_label = QLabel("avg — dB  min — dB")
        form.addRow("", self._snr_avg_label)

        return grp

    def _make_hop_group(self) -> QGroupBox:
        grp = QGroupBox("Frequency Hopping")
        form = QFormLayout(grp)

        self._hop_freq_label = ValueLabel("— MHz")
        form.addRow("Current Freq:", self._hop_freq_label)

        self._hop_idx_label = QLabel("0")
        form.addRow("Hop Index:", self._hop_idx_label)

        self._hop_rate_label = QLabel("— hops/s")
        form.addRow("Hop Rate:", self._hop_rate_label)

        return grp

    def _make_events_group(self) -> QGroupBox:
        grp = QGroupBox("Recent Events")
        layout = QVBoxLayout(grp)
        self._events_box = QTextEdit()
        self._events_box.setReadOnly(True)
        self._events_box.setFixedHeight(160)
        self._events_box.setFont(QFont("Monospace", 8))
        layout.addWidget(self._events_box)
        return grp

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self):
        if self._fg is None:
            return
        s = self._fg.get_stats()

        # Elapsed
        elapsed = int(time.time() - self._start_time)
        h, rem = divmod(elapsed, 3600)
        m, sec = divmod(rem, 60)
        self._elapsed_label.setText(f"{h}:{m:02d}:{sec:02d}")

        # Link LED + label
        if s.running and s.rx_frames > 0 and s.tx_frames > 0:
            self._link_led.set_color("green")
            self._link_label.set_value("Active", "#00c000")
        elif s.running and s.tx_frames > 0:
            self._link_led.set_color("yellow")
            self._link_label.set_value("TX Only", "#c0c000")
        elif s.running:
            self._link_led.set_color("yellow")
            self._link_label.set_value("Starting", "#c0c000")
        else:
            self._link_led.set_color("gray")
            self._link_label.set_value("Stopped", "")

        # Traffic
        self._tx_count.setText(f"{s.tx_frames:,}")
        self._tx_rate.setText(f"{s.tx_rate:.1f} pkt/s")
        self._rx_count.setText(f"{s.rx_frames:,}")
        self._rx_rate.setText(f"{s.rx_rate:.1f} pkt/s")

        pdr = max(0.0, 100.0 - s.packet_loss_pct)
        self._pdr_label.set_value(f"{pdr:.1f}%")
        self._pdr_bar.setValue(int(pdr))
        if pdr >= 95:
            self._pdr_label.set_value(f"{pdr:.1f}%", "#00c000")
        elif pdr >= 80:
            self._pdr_label.set_value(f"{pdr:.1f}%", "#c0c000")
        else:
            self._pdr_label.set_value(f"{pdr:.1f}%", "#c00000")

        # Detection rate (% of TX frames detected by RX)
        det = min(100.0, s.detection_rate) if s.tx_frames > 0 else 0.0
        self._detect_label.set_value(f"{det:.1f}%")
        self._detect_bar.setValue(int(det))
        det_color = "#00c000" if det >= 90 else "#c0c000" if det >= 50 else "#c00000"
        self._detect_label.set_value(f"{det:.1f}%", det_color)

        # Decode rate (% of detected frames with FEC OK)
        dec = min(100.0, s.fec_ok_rate) if s.rx_frames > 0 else 0.0
        self._decode_label.set_value(f"{dec:.1f}%")
        self._decode_bar.setValue(int(dec))
        dec_color = "#00c000" if dec >= 90 else "#c0c000" if dec >= 50 else "#c00000"
        self._decode_label.set_value(f"{dec:.1f}%", dec_color)

        self._fec_ok_label.setText(f"{s.rx_fec_ok:,} OK")
        errs = s.rx_fec_err
        self._fec_err_label.set_value(
            f"{errs:,} ERR",
            "#c00000" if errs > 0 else ""
        )

        # SNR
        snr = s.last_snr
        self._snr_label.setText(f"{snr:.1f} dB")
        self._snr_bar.setValue(int(min(40, max(0, snr))))
        if s.snr_history:
            avg = sum(s.snr_history) / len(s.snr_history)
            mn = min(s.snr_history)
            self._snr_avg_label.setText(f"avg {avg:.1f} dB  min {mn:.1f} dB")
            color = "#00c000" if avg > 15 else "#c0c000" if avg > 8 else "#c00000"
            self._snr_label.setStyleSheet(f"color:{color}; font-weight:bold;")

        # Hopping
        freq = s.current_hop_freq
        if freq > 0:
            self._hop_freq_label.setText(f"{freq/1e6:.3f} MHz")
        self._hop_idx_label.setText(f"{s.hop_index:,}")
        # Hop rate ≈ rx_rate (one hop per burst)
        if s.tx_rate > 0:
            self._hop_rate_label.setText(f"{s.tx_rate:.1f} hops/s")

        # Events log — only redraw if changed
        events_text = "\n".join(s.recent_events)
        if self._events_box.toPlainText() != events_text:
            self._events_box.setPlainText(events_text)
            self._events_box.verticalScrollBar().setValue(
                self._events_box.verticalScrollBar().maximum()
            )
