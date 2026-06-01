import unittest

from radio.runtime_modulation import (
    SUPPORTED_RUNTIME_MODULATIONS,
    runtime_bits_per_symbol,
    validate_runtime_frame_alignment,
    validate_runtime_modulation,
)
from radio.tx_flowgraph import _IDLE_CHIP_PATTERN


class RuntimeModulationTests(unittest.TestCase):
    def test_bpsk_and_qpsk_are_current_runtime_modes(self):
        self.assertEqual(SUPPORTED_RUNTIME_MODULATIONS, {"bpsk", "qpsk"})
        validate_runtime_modulation("bpsk")
        validate_runtime_modulation("qpsk")
        self.assertEqual(runtime_bits_per_symbol("bpsk"), 1)
        self.assertEqual(runtime_bits_per_symbol("qpsk"), 2)

    def test_unwired_runtime_modulations_fail_loudly(self):
        for modulation_type in ("8psk", "8dpsk", "d8psk"):
            with self.subTest(modulation_type=modulation_type):
                with self.assertRaisesRegex(ValueError, "Runtime flowgraph"):
                    validate_runtime_modulation(modulation_type)

    def test_qpsk_requires_even_total_frame_chips(self):
        validate_runtime_frame_alignment("qpsk", 17268)
        with self.assertRaisesRegex(ValueError, "divisible by 2"):
            validate_runtime_frame_alignment("qpsk", 17269)

    def test_idle_chip_pattern_is_not_preamble_like(self):
        preamble = [0, 1] * 16
        tiled_idle = list(_IDLE_CHIP_PATTERN) * 3
        bipolar_preamble = [1 - 2 * bit for bit in preamble]

        max_corr = 0
        for offset in range(len(_IDLE_CHIP_PATTERN)):
            window = tiled_idle[offset : offset + len(preamble)]
            bipolar_window = [1 - 2 * int(bit) for bit in window]
            corr = abs(
                sum(a * b for a, b in zip(bipolar_preamble, bipolar_window))
            )
            max_corr = max(max_corr, corr)

        self.assertLess(max_corr, len(preamble) * 0.55)


if __name__ == "__main__":
    unittest.main()
