#!/usr/bin/env python3
"""
Surrogate Datalink System — Entry Point.

Usage:
  python main.py                          # GUI mode, default config
  python main.py --config my_config.yaml  # GUI mode, custom config
  python main.py --no-gui                 # Headless/CLI mode
  python main.py --log-level DEBUG        # Override log level
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Configurable Bidirectional Datalink Surrogate System"
    )
    parser.add_argument(
        "--config",
        default="config/default_config.yaml",
        help="Path to YAML configuration file",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Run in headless mode (no PyQt GUI)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override log level from config",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Ensure config file exists; fall back gracefully
    config_path = args.config
    if not Path(config_path).exists() and config_path != "config/default_config.yaml":
        print(f"WARNING: Config file not found: {config_path} — using defaults")

    from core.config_manager import ConfigManager
    config_manager = ConfigManager(config_path)
    cfg = config_manager.config

    if args.log_level:
        cfg.logging.log_level = args.log_level

    from logging_module.surrogate_logger import SurrogateLogger
    logger = SurrogateLogger(cfg.logging)

    from radio.flowgraph_manager import FlowgraphManager
    fg_manager = FlowgraphManager(config_manager, logger)

    if args.no_gui:
        _run_headless(fg_manager)
    else:
        _run_gui(config_manager, fg_manager, logger)


def _run_headless(fg_manager):
    import signal
    import time

    print("Starting in headless mode. Press Ctrl+C to stop.")
    fg_manager.start()

    def _sigint(sig, frame):
        print("\nStopping...")
        fg_manager.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        fg_manager.stop()


def _run_gui(config_manager, fg_manager, logger):
    from PyQt5.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Datalink Surrogate")
    app.setStyle("Fusion")

    window = MainWindow(config_manager, fg_manager, logger)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
