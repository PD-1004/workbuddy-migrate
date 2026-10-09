"""Update checks must not require a user's GitHub credentials."""
import io
import json
import unittest
import sys
import types
import marshal
import struct
import zlib
from pathlib import Path
from unittest.mock import patch

if "--packed" in sys.argv:
    sys.argv.remove("--packed")
    from build_repaired import archive, unpack
    report = json.loads((Path(__file__).absolute().parent / "build" / "build_report.json").read_text(encoding="utf-8"))
    data = Path(report["output"]).read_bytes()
    pyz = next(unpack(e) for e in archive(data)[3] if e["name"] == "PYZ.pyz")
    _, position, length = dict(marshal.loads(pyz[struct.unpack("!I", pyz[8:12])[0]:]))["wb_updates"]
    wb_updates = types.ModuleType("wb_updates")
    exec(marshal.loads(zlib.decompress(pyz[position:position + length])), wb_updates.__dict__)
else:
    import wb_updates


class UpdateTests(unittest.TestCase):
    def check(self, payload, current="1.0.0"):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as request:
            result = wb_updates.check_update(current, "https://raw.githubusercontent.com/test/releases/main/version.json", "https://github.com/test/releases")
        self.assertEqual(request.call_args.kwargs["timeout"], 5)
        self.assertNotIn("Authorization", request.call_args.args[0].headers)
        return result

    def test_new_version_offers_download(self):
        result = self.check({"version": "1.1.0", "url": "https://github.com/test/releases/releases/tag/v1.1.0", "notes": "修复说明"})
        self.assertTrue(result["available"])
        self.assertEqual(result["notes"], "修复说明")

    def test_numeric_version_order_and_no_downgrade(self):
        for remote, current, expected in [("1.10.0", "1.9.0", True), ("1.0.0", "1.0.0", False), ("1.0.0", "1.1.0", False)]:
            result = self.check({"version": remote, "url": "https://github.com/test/releases/releases/latest"}, current)
            self.assertEqual(result["available"], expected)

    def test_untrusted_download_url_is_rejected(self):
        for url in ["javascript:alert(1)", "https://github.com/test/releases.evil/releases/latest", "https://evil.example/download", "https://github.com/test/releases/releases/../../../attacker/repo/releases/latest", "https://github.com/test/releases/releases/%2e%2e/%2e%2e/attacker/releases/latest"]:
            self.assertFalse(self.check({"version": "2.0.0", "url": url})["ok"])

    def test_invalid_version_is_rejected(self):
        for version in ["1.1.0-beta", "latest", None]:
            self.assertFalse(self.check({"version": version, "url": "https://github.com/test/releases/releases/latest"})["ok"])

    def test_offline_does_not_crash_application(self):
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            result = wb_updates.check_update("1.0.0", "https://example.test/feed", "https://github.com/test/releases")
        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])


if __name__ == "__main__":
    unittest.main()
