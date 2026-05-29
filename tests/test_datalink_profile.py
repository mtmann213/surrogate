import unittest
import tempfile
from pathlib import Path

import yaml

from core.datalink_profile import FrameCodec


def profile_dict(**overrides):
    data = {
        "profile": {"name": "unit_test"},
        "frame": {
            "preamble": {"bits": "1010101010101010"},
            "syncword": {
                "bits": "0011110101001100",
                "max_bit_errors": 1,
                "allow_inverted": True,
            },
            "header": {
                "fields": [
                    {"name": "length", "type": "uint", "bits": 16, "value_from": "payload.length_bytes"},
                    {"name": "id", "type": "uint", "bits": 8, "default": 7},
                    {"name": "counter", "type": "uint", "bits": 16, "auto_increment": True},
                ]
            },
            "header_crc": {"enabled": True, "algorithm": "crc16_ccitt"},
            "payload": {"max_bytes": 64},
            "payload_crc": {"enabled": True, "algorithm": "crc32"},
            "padding": {"mode": "custom_byte", "byte": 0, "align_bits": 8},
        },
        "coding": {
            "fec": {"type": "none"},
            "interleaving": {"enabled": False},
            "whitening": {"enabled": False},
        },
    }
    for key, value in overrides.items():
        data["coding"][key] = value
    return data


class DatalinkProfileTests(unittest.TestCase):
    def test_build_parse_roundtrip_without_coding(self):
        codec = FrameCodec.from_dict(profile_dict())
        built = codec.build(b"hello", {"id": 9})
        parsed = codec.parse(built.bits)

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.payload, b"hello")
        self.assertEqual(parsed.header["length"], 5)
        self.assertEqual(parsed.header["id"], 9)
        self.assertEqual(parsed.header["counter"], 0)

    def test_counter_auto_increments(self):
        codec = FrameCodec.from_dict(profile_dict())
        first = codec.build(b"a")
        second = codec.build(b"b")

        self.assertEqual(first.header["counter"], 0)
        self.assertEqual(second.header["counter"], 1)

    def test_header_crc_detects_corruption(self):
        codec = FrameCodec.from_dict(profile_dict())
        bits = codec.build(b"payload").bits.copy()

        pre_sync = 16 + 16
        bits[pre_sync + 3] ^= 1
        parsed = codec.parse(bits)

        self.assertFalse(parsed.header_crc_ok)
        self.assertFalse(parsed.ok)

    def test_payload_crc_detects_corruption(self):
        codec = FrameCodec.from_dict(profile_dict())
        bits = codec.build(b"payload").bits.copy()

        bits[-1] ^= 1
        parsed = codec.parse(bits)

        self.assertTrue(parsed.header_crc_ok)
        self.assertFalse(parsed.payload_crc_ok)
        self.assertFalse(parsed.ok)

    def test_whitening_roundtrip(self):
        codec = FrameCodec.from_dict(profile_dict(
            whitening={"enabled": True, "type": "lfsr", "seed": 0x7F, "mask": 0x48},
        ))

        parsed = codec.parse(codec.build(b"whitened").bits)

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.payload, b"whitened")

    def test_convolutional_fec_roundtrip(self):
        codec = FrameCodec.from_dict(profile_dict(
            fec={
                "type": "convolutional_k7_r12",
                "constraint_length": 7,
                "polynomials": [121, 91],
            },
        ))

        parsed = codec.parse(codec.build(b"coded").bits)

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.payload, b"coded")

    def test_matrix_interleaving_roundtrip(self):
        codec = FrameCodec.from_dict(profile_dict(
            interleaving={"enabled": True, "type": "matrix", "rows": 8},
        ))

        parsed = codec.parse(codec.build(b"abcd").bits)

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.payload, b"abcd")

    def test_inverted_frame_is_accepted_when_sync_allows_it(self):
        codec = FrameCodec.from_dict(profile_dict())
        bits = codec.build(b"invert").bits ^ 1

        parsed = codec.parse(bits)

        self.assertTrue(parsed.ok)
        self.assertTrue(parsed.inverted)
        self.assertEqual(parsed.payload, b"invert")

    def test_payload_max_is_enforced(self):
        codec = FrameCodec.from_dict(profile_dict())

        with self.assertRaises(ValueError):
            codec.build(b"x" * 65)

    def test_profile_loads_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.yaml"
            path.write_text(yaml.safe_dump(profile_dict()))

            codec = FrameCodec.from_yaml(path)
            parsed = codec.parse(codec.build(b"yaml").bits)

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.payload, b"yaml")

    def test_length_helpers_match_built_frame(self):
        codec = FrameCodec.from_dict(profile_dict(
            fec={
                "type": "convolutional_k7_r12",
                "constraint_length": 7,
                "polynomials": [121, 91],
            },
            whitening={"enabled": True, "type": "lfsr", "seed": 0x7F, "mask": 0x48},
        ))
        built = codec.build(b"lengths")

        self.assertEqual(codec.payload_region_bytes_for_length(7), 11)
        self.assertEqual(codec.frame_bits_for_length(7), len(built.bits))

    def test_parse_ignores_trailing_stream_bits(self):
        codec = FrameCodec.from_dict(profile_dict())
        built = codec.build(b"stream")
        trailing = [1, 1, 0, 0, 1, 0, 1, 0] * 3

        parsed = codec.parse(list(built.bits) + trailing)

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.payload, b"stream")

    def test_checked_in_example_profile_roundtrip(self):
        codec = FrameCodec.from_yaml("config/profiles/bpsk_static_v1.yaml")
        built = codec.build(b"example")
        parsed = codec.parse(built.bits)

        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.payload, b"example")
        self.assertEqual(len(built.bits), codec.frame_bits_for_length(7))


if __name__ == "__main__":
    unittest.main()
