#!/usr/bin/env python3
"""
Diagnostics: run BPSK datalink with hopping disabled and measure
actual chip-level quality and FEC performance.

Logs:
  - Per-frame correlation peak, SNR, polarity
  - Per-frame despread soft-bit statistics (mean, std, distribution)
  - Per-frame bit error count (by comparing invariant + known payload)
  - FEC decode result
"""
import sys, time, logging, numpy as np
from pathlib import Path
from core.config_manager import ConfigManager, compute_frame_stats
from core.fec_codec import FECCodec
from core.frame_generator import FrameGenerator
from core.hop_scheduler import HopScheduler
from core.anomaly_injector import AnomalyInjector
from radio.tx_flowgraph import TXFlowgraph
from radio.rx_flowgraph import RXFlowgraph

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s')
log = logging.getLogger('diag')

class DiagStats:
    def __init__(self):
        self.tx_frames = 0
        self.rx_frames = 0
        self.fec_ok = 0
        self.fec_err = 0
        self.preamble_peaks = []
        self.snr_values = []
        self.bit_error_counts = []
        self.soft_bit_means = []

stats = DiagStats()

def on_tx(frame_id, payload, ts):
    stats.tx_frames += 1
    if frame_id < 5 or frame_id % 10 == 0:
        log.info(f'TX #{frame_id}')

def on_rx(payload_bytes, ts, snr, fec_ok):
    stats.rx_frames += 1
    if fec_ok:
        stats.fec_ok += 1
    else:
        stats.fec_err += 1

    # Summarize
    peak = getattr(rx_fg._frame_sink, 'last_peak', 0)
    stats.preamble_peaks.append(peak)
    stats.snr_values.append(snr)

    log.info(f'RX #{stats.rx_frames} SNR={snr:.1f} peak={peak:.1f} FEC={"OK" if fec_ok else "ERR"} '
             f'| total: {stats.fec_ok}OK/{stats.fec_err}ERR')

def main():
    cm = ConfigManager('config/default_config.yaml')
    cfg = cm.config
    cfg.hopping.enabled = False  # Force no hopping

    stats_dict = compute_frame_stats(cfg)
    log.info(f'Config: sps={stats_dict["sps"]} burst={stats_dict["burst_ms"]:.1f}ms '
             f'coded_bits={stats_dict["coded_bits"]} total_chips={stats_dict["total_chips"]} '
             f'chip_rate={cfg.modulation.chip_rate_sps/1e3:.0f}kHz')

    fec = FECCodec(cfg.frame.fec)
    fg = FrameGenerator(cfg.frame, fec)
    hs = HopScheduler(cfg.hopping, cfg.rf)
    ai = AnomalyInjector(cfg.anomaly)

    tx_fg = TXFlowgraph(cfg, hs, fg, fec, ai, on_tx)
    rx_fg = RXFlowgraph(cfg, hs, fg, fec, on_rx)

    rx_fg.start()
    tx_fg.start()
    log.info('Flowgraphs started. Running for 15 seconds...')

    try:
        time.sleep(15)
    except KeyboardInterrupt:
        pass

    tx_fg.stop()
    rx_fg.stop()

    log.info('='*60)
    log.info(f'DIAGNOSTIC RESULTS ({stats.tx_frames} TX frames, {stats.rx_frames} RX frames):')
    log.info(f'  FEC OK: {stats.fec_ok}, ERR: {stats.fec_err}')
    log.info(f'  Preamble peaks: {np.mean(stats.preamble_peaks):.1f} +/- {np.std(stats.preamble_peaks):.1f} '
             f'(min={min(stats.preamble_peaks):.1f}, max={max(stats.preamble_peaks):.1f})')
    log.info(f'  SNR: {np.mean(stats.snr_values):.1f} +/- {np.std(stats.snr_values):.1f} dB')
    log.info('='*60)

if __name__ == '__main__':
    main()
