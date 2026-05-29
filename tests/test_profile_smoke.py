import json
import subprocess
import sys
import unittest


class ProfileSmokeCommandTests(unittest.TestCase):
    def test_profile_smoke_command(self):
        result = subprocess.run(
            [
                sys.executable,
                "tools/profile_smoke.py",
                "config/profiles/bpsk_static_v1.yaml",
                "--payload-text",
                "smoke",
                "--id",
                "12",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["payload_hex"], b"smoke".hex())
        self.assertEqual(data["header"]["id"], 12)
        self.assertGreater(data["frame_bits"], data["encoded_payload_bits"])


if __name__ == "__main__":
    unittest.main()
