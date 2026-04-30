#!/usr/bin/env python3
"""
Diagnostics: run BPSK datalink and measure chip-level quality, FEC performance.
Also provides offline analysis of captured IQ / chip files.

Usage:
  python3 diagnose_link.py              # Run live diagnostic (15 s)
  python3 diagnose_link.py --analyze recordings/near_miss_0.npy   # Analyze saved chip window
"""
import sys, time, logging, os
import numpy as np
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s')
log = logging.getLogger('diag')


def analyze_chip_window(npy_path: str) -> None:
    """Analyze a saved near-miss chip window to understand preamble detection failure."""
    data = np.load(npy_path)
    log.info("=== Analyzing %s ===", npy_path)
    log.info("  Shape: %s  dtype: %s  len: %d", data.shape, data.dtype, len(data))
    log.info("  Mean: %.4f  Std: %.4f  Abs mean: %.4f", float(np.mean(data)),
             float(np.std(data)), float(np.mean(np.abs(data))))
    log.info("  Min: %.4f  Max: %.4f", float(np.min(data)), float(np.max(data)))

    # Print first 40 values
    preview = ' '.join(f'{x:+.2f}' for x in data[:min(40, len(data))])
    log.info("  First 40 values: [%s]", preview)

    # Generate expected preamble (0x55555555 = alternating 1,0)
    pre_len = 32
    expected = (1 - 2 * np.array([1,0,1,0,1,0,1,0,1,0,1,0,1,0,1,0,
                                  1,0,1,0,1,0,1,0,1,0,1,0,1,0,1,0], dtype=np.float32))

    # Sliding correlation
    if len(data) >= pre_len:
        corr = np.correlate(data, expected, mode='valid')
        peak_idx = int(np.argmax(np.abs(corr)))
        peak_val = float(np.abs(corr[peak_idx]))
        log.info("  Preamble correlation peak: %.2f (expected ~32 for clean BPSK)", peak_val)
        log.info("  Peak at offset: %d", peak_idx)
        log.info("  Peak / preamble_len: %.3f (should be ~1.0 for full-amplitude symbols)", peak_val / pre_len)

        # Extract the region around the peak
        region = data[max(0, peak_idx-2):peak_idx+pre_len+2]
        log.info("  Region around peak: %s", ' '.join(f'{x:+.2f}' for x in region[:min(36, len(region))]))

        # Check if this looks like BPSK (±1) or noise
        region_abs = np.abs(region)
        log.info("  Region abs mean: %.3f  (should be ~1.0 for BPSK)", float(np.mean(region_abs)))
    else:
        log.info("  Data too short (%d) for preamble correlation (%d)", len(data), pre_len)

    # Histogram
    hist, edges = np.histogram(data, bins=21, range=(-2, 2))
    log.info("  Histogram (bins from -2 to +2):")
    for i in range(len(hist)):
        if hist[i] > 0:
            log.info("    [%+.2f, %+.2f): %d", edges[i], edges[i+1], hist[i])

    # Check if this looks random (Gaussian noise) or has structure
    from scipy import stats as sp_stats
    try:
        _, p_value = sp_stats.normaltest(data)
        log.info("  Normality test p-value: %.4f (low = not Gaussian = has structure)", p_value)
    except Exception:
        pass


def analyze_near_miss_dir(diag_dir: str) -> None:
    """Analyze all near-miss chip windows in a directory."""
    for f in sorted(Path(diag_dir).glob("near_miss_*.npy")):
        if "_corr" not in f.name:
            analyze_chip_window(str(f))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--analyze":
        path = sys.argv[2] if len(sys.argv) > 2 else "recordings"
        if os.path.isdir(path):
            analyze_near_miss_dir(path)
        else:
            analyze_chip_window(path)
        return

    # Live diagnostic run
    from core.config_manager import ConfigManager
    from core.fec_codec import FECCodec
    from core.frame_generator import FrameGenerator
    from core.hop_scheduler import HopScheduler
    from core.anomaly_injector import AnomalyInjector

    cm = ConfigManager('config/default_config.yaml')
    cfg = cm.config

    # Force specific settings for diagnostic
    updates = {
        "hopping": {"enabled": False},
        "rf": {"sample_rate": 1_000_000.0},
    }
    cm.update(updates)
    cfg = cm.config

    log.info("=== Link Diagnostic ===")
    log.info("sample_rate=%d  chip_rate=%d  sps=%.1f",
             cfg.rf.sample_rate, cfg.modulation.chip_rate_sps,
             cfg.rf.sample_rate / cfg.modulation.chip_rate_sps)
    log.info("burst_duration=%.3f ms  guard=%.1f ms",
             cfg.timing.burst_duration_ms, cfg.timing.transition_time_ms)
    log.info("hopping=%s  modulation=%s",
             cfg.hopping.enabled, cfg.modulation.type)

    fec = FECCodec(cfg.frame.fec)
    fg = FrameGenerator(cfg.frame, fec)
    hs = HopScheduler(cfg.hopping, cfg.rf)
    ai = AnomalyInjector(cfg.anomaly)

    from radio.flowgraph_manager import FlowgraphManager
    from radio.blocks.baseband_hopper import BasebandHopper
    from radio.tx_flowgraph import TXFlowgraph
    from radio.rx_flowgraph import RXFlowgraph

    # Manual construction for diagnostics
    burst_s = cfg.timing.burst_duration_ms / 1000.0
    guard_s = cfg.timing.transition_time_ms / 1000.0

    tx_fg = TXFlowgraph(cfg, hs, fg, fec, ai, lambda fid, p, ts: None)
    rx_fg = RXFlowgraph(cfg, hs, fg, fec, lambda p, ts, snr, ok: (
        log.info("RX frame: SNR=%.1f peak=%.2f FEC=%s snr=%.1f",
                 snr, rx_fg._frame_sink.last_peak if hasattr(rx_fg._frame_sink, 'last_peak') else 0,
                 "OK" if ok else "ERR", snr)
    ))

    rx_fg.start()
    tx_fg.start()
    log.info("Flowgraphs started. Running for 15 seconds...")

    try:
        for i in range(15):
            time.sleep(1)
            # Log per-second status
            agc_gain = rx_fg.get_agc_gain() if hasattr(rx_fg, 'get_agc_gain') else 0.0
            frames = rx_fg.get_frame_count()
            snr = rx_fg.get_snr()
            peak = rx_fg._frame_sink.last_peak if hasattr(rx_fg._frame_sink, 'last_peak') else 0.0
            log.info("[%ds] frames=%d  AGC=%.1fdB  peak=%.2f  SNR=%.1f",
                     i+1, frames,
                     20*np.log10(agc_gain) if agc_gain > 0 else 0,
                     peak, snr)
    except KeyboardInterrupt:
        pass

    tx_fg.stop()
    tx_fg.wait()
    rx_fg.stop()
    rx_fg.wait()

    log.info("=== Diagnostic Complete ===")
    log.info("Total RX frames: %d", rx_fg.get_frame_count())


if __name__ == '__main__':
    main()