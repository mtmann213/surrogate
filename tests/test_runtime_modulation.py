import unittest

from radio.runtime_modulation import (
    SUPPORTED_RUNTIME_MODULATIONS,
    validate_runtime_modulation,
)


class RuntimeModulationTests(unittest.TestCase):
    def test_bpsk_is_current_runtime_mode(self):
        self.assertEqual(SUPPORTED_RUNTIME_MODULATIONS, {"bpsk"})
        validate_runtime_modulation("bpsk")

    def test_unwired_runtime_modulations_fail_loudly(self):
        for modulation_type in ("qpsk", "8psk", "8dpsk", "d8psk"):
            with self.subTest(modulation_type=modulation_type):
                with self.assertRaisesRegex(ValueError, "Runtime flowgraph"):
                    validate_runtime_modulation(modulation_type)


if __name__ == "__main__":
    unittest.main()
