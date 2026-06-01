import unittest

from core.config_manager import SurrogateConfig, compute_frame_stats


def _base_config(**overrides):
    data = {
        "rf": {
            "gnd_device": "serial=test",
            "msl_device": "serial=test",
            "sample_rate": 1_000_000.0,
        },
        "modulation": {
            "type": "bpsk",
            "chip_rate_sps": 250_000.0,
        },
    }
    for key, value in overrides.items():
        data.setdefault(key, {}).update(value)
    return data


class ConfigManagerDerivationTests(unittest.TestCase):
    def test_symbol_rate_uses_bits_per_symbol_registry(self):
        cases = [
            ("bpsk", 1, 250_000.0),
            ("qpsk", 2, 125_000.0),
            ("8psk", 3, 250_000.0 / 3.0),
            ("8dpsk", 3, 250_000.0 / 3.0),
            ("d8psk", 3, 250_000.0 / 3.0),
        ]

        for mod_type, bits_per_symbol, symbol_rate in cases:
            with self.subTest(mod_type=mod_type):
                cfg = SurrogateConfig.model_validate(
                    _base_config(modulation={"type": mod_type})
                )

                self.assertEqual(cfg.modulation.bits_per_symbol, bits_per_symbol)
                self.assertAlmostEqual(cfg.modulation.symbol_rate, symbol_rate)

    def test_stats_report_symbol_rate_sps_and_derived_bandwidths(self):
        cfg = SurrogateConfig.model_validate(
            _base_config(modulation={"type": "8psk", "chip_rate_sps": 250_000.0})
        )

        stats = compute_frame_stats(cfg)

        self.assertAlmostEqual(stats["sps"], 12.0)
        self.assertAlmostEqual(stats["signal_bw_hz"], (250_000.0 / 3.0) * 1.35)
        self.assertAlmostEqual(stats["hw_bw_hz"], cfg.rf.tx_bandwidth)
        self.assertAlmostEqual(stats["bandwidth_hz"], stats["signal_bw_hz"])

    def test_chip_rate_snaps_to_integer_samples_per_symbol(self):
        cfg = SurrogateConfig.model_validate(
            _base_config(modulation={"type": "qpsk", "chip_rate_sps": 310_000.0})
        )

        self.assertAlmostEqual(cfg.modulation.chip_rate_sps, 1_000_000.0 * 2 / 6)
        self.assertAlmostEqual(
            cfg.rf.sample_rate / cfg.modulation.symbol_rate,
            6.0,
        )


if __name__ == "__main__":
    unittest.main()
