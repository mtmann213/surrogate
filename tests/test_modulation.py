import unittest

import numpy as np

from core.demodulation import (
    DEMOD_FUNCS,
    demodulate_8psk,
    demodulate_bpsk,
    demodulate_d8psk,
    demodulate_dbpsk,
    demodulate_dqpsk,
    demodulate_fsk,
    demodulate_qpsk,
    differential_decode,
)
from core.modulation import (
    BITS_PER_SYMBOL,
    MOD_FUNCS,
    differential_encode,
    modulate_8psk,
    modulate_bpsk,
    modulate_d8psk,
    modulate_dbpsk,
    modulate_dqpsk,
    modulate_fsk,
    modulate_qpsk,
)


class ModulationRoundtripTests(unittest.TestCase):
    def test_bpsk_roundtrip_all_symbols(self):
        bits = np.array([0, 1, 1, 0, 1, 0], dtype=np.uint8)

        rx = demodulate_bpsk(modulate_bpsk(bits))

        self.assertTrue(np.array_equal(bits, rx))

    def test_qpsk_roundtrip_all_symbol_pairs(self):
        bits = np.array([
            0, 0,
            0, 1,
            1, 0,
            1, 1,
        ], dtype=np.uint8)

        rx = demodulate_qpsk(modulate_qpsk(bits))

        self.assertTrue(np.array_equal(bits, rx))

    def test_8psk_roundtrip_all_symbol_triples(self):
        triples = []
        for value in range(8):
            triples.extend([(value >> 2) & 1, (value >> 1) & 1, value & 1])
        bits = np.array(triples, dtype=np.uint8)

        rx = demodulate_8psk(modulate_8psk(bits))

        self.assertTrue(np.array_equal(bits, rx))

    def test_differential_encode_decode_roundtrip(self):
        bits = np.array([1, 0, 1, 1, 0, 0, 1], dtype=np.uint8)

        rx = differential_decode(differential_encode(bits))

        self.assertTrue(np.array_equal(bits, rx))

    def test_differential_psk_roundtrips(self):
        cases = [
            (modulate_dbpsk, demodulate_dbpsk, 17),
            (modulate_dqpsk, demodulate_dqpsk, 18),
            (modulate_d8psk, demodulate_d8psk, 24),
        ]
        for mod, demod, n_bits in cases:
            with self.subTest(mod=mod.__name__):
                bits = np.array([(idx * 5 + 1) & 1 for idx in range(n_bits)], dtype=np.uint8)

                rx = demod(mod(bits))

                self.assertTrue(np.array_equal(bits, rx))

    def test_qpsk_rejects_odd_bit_count(self):
        with self.assertRaises(ValueError):
            modulate_qpsk(np.array([1, 0, 1], dtype=np.uint8))

    def test_8psk_rejects_non_triple_bit_count(self):
        with self.assertRaises(ValueError):
            modulate_8psk(np.array([1, 0, 1, 1], dtype=np.uint8))

    def test_fsk_roundtrip_and_length(self):
        bits = np.array([0, 1, 1, 0, 1, 0, 0, 1], dtype=np.uint8)
        samples_per_symbol = 16

        samples = modulate_fsk(bits, samples_per_symbol=samples_per_symbol)
        rx = demodulate_fsk(samples, samples_per_symbol=samples_per_symbol)

        self.assertEqual(len(samples), len(bits) * samples_per_symbol)
        self.assertTrue(np.array_equal(bits, rx))

    def test_empty_inputs(self):
        self.assertEqual(len(modulate_bpsk([])), 0)
        self.assertEqual(len(modulate_fsk([])), 0)
        self.assertEqual(len(demodulate_fsk([])), 0)

    def test_function_registries_are_consistent(self):
        for name in BITS_PER_SYMBOL:
            self.assertIn(name, MOD_FUNCS)
            self.assertIn(name, DEMOD_FUNCS)


if __name__ == "__main__":
    unittest.main()
