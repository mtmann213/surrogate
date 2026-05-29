import unittest

import numpy as np

from core.fec_codec import ConvolutionalCodec


class ConvolutionalCodecTests(unittest.TestCase):
    def test_roundtrip_varied_byte_lengths(self):
        codec = ConvolutionalCodec()
        for size in range(1, 32):
            with self.subTest(size=size):
                data = bytes(range(size))
                bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
                decoded = codec.decode(codec.encode(bits))
                self.assertTrue(np.array_equal(bits, decoded))


if __name__ == "__main__":
    unittest.main()
