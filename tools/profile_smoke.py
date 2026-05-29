#!/usr/bin/env python3
"""Build and parse one payload through a datalink profile.

This is intentionally lightweight: it exercises the pure profile/frame engine
without starting GNU Radio or touching SDR hardware.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.datalink_profile import FrameCodec


def _payload_from_args(args: argparse.Namespace) -> bytes:
    if args.payload_hex is not None:
        return bytes.fromhex(args.payload_hex)
    return args.payload_text.encode("utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build/parse a payload through a datalink profile."
    )
    parser.add_argument(
        "profile",
        nargs="?",
        default="config/profiles/bpsk_static_v1.yaml",
        help="Path to datalink profile YAML.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--payload-text",
        default="hello",
        help="UTF-8 payload text to send through the profile.",
    )
    group.add_argument(
        "--payload-hex",
        help="Hex payload bytes to send through the profile.",
    )
    parser.add_argument("--id", type=int, dest="link_id", help="Override header id field.")
    parser.add_argument("--flags", type=int, help="Override header flags field.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = _payload_from_args(args)
    codec = FrameCodec.from_yaml(args.profile)

    overrides = {}
    if args.link_id is not None:
        overrides["id"] = args.link_id
    if args.flags is not None:
        overrides["flags"] = args.flags

    built = codec.build(payload, overrides)
    parsed = codec.parse(built.bits)

    result = {
        "profile": codec.profile.name,
        "payload_hex": payload.hex(),
        "frame_bits": len(built.bits),
        "payload_region_bytes": codec.payload_region_bytes_for_length(len(payload)),
        "encoded_payload_bits": codec.encoded_payload_bits_for_length(len(payload)),
        "header": parsed.header,
        "header_crc_ok": parsed.header_crc_ok,
        "payload_crc_ok": parsed.payload_crc_ok,
        "fec_ok": parsed.fec_ok,
        "inverted": parsed.inverted,
        "ok": parsed.ok,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if parsed.ok and parsed.payload == payload else 1


if __name__ == "__main__":
    raise SystemExit(main())
