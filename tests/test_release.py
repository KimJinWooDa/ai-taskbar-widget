import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "widget_main", _ROOT / "ClaudeUsageWidget.pyw")
widget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(widget)


def _release_json(tag, assets=None, body="notes"):
    return json.dumps({
        "tag_name": tag,
        "body": body,
        "assets": [{"name": n, "browser_download_url": u}
                   for n, u in (assets or {}).items()],
    })


class ParseReleaseTests(unittest.TestCase):
    def test_normal_release(self):
        text = _release_json("v3.16.0", {
            "AI-Skill-Widget.exe": "https://example.com/w.exe",
            "SkillEventHook.exe": "https://example.com/h.exe",
        })
        ver_t, ver_s, notes, assets = widget.parse_release(text)
        self.assertEqual(ver_t, (3, 16, 0))
        self.assertEqual(ver_s, "3.16.0")
        self.assertEqual(notes, "notes")
        self.assertEqual(assets["AI-Skill-Widget.exe"],
                         "https://example.com/w.exe")

    def test_tag_without_v_prefix(self):
        ver_t, ver_s, _, _ = widget.parse_release(_release_json("3.2"))
        self.assertEqual(ver_t, (3, 2, 0))
        self.assertEqual(ver_s, "3.2")

    def test_experimental_tag_rejected(self):
        self.assertIsNone(widget.parse_release(_release_json("nightly")))
        self.assertIsNone(widget.parse_release(_release_json("v3.16.0-rc1")))

    def test_bad_json_rejected(self):
        self.assertIsNone(widget.parse_release("<html>rate limited</html>"))

    def test_assets_missing_fields_skipped(self):
        text = json.dumps({"tag_name": "v9.0.0",
                           "assets": [{"name": "x.exe"}, {}]})
        _, _, _, assets = widget.parse_release(text)
        self.assertEqual(assets, {})


class CheckExeTests(unittest.TestCase):
    def test_small_or_non_pe_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            small = os.path.join(d, "small.exe")
            with open(small, "wb") as f:
                f.write(b"MZ" + b"\0" * 100)
            with self.assertRaises(RuntimeError):
                widget.check_exe(small)
            big_not_pe = os.path.join(d, "big.bin")
            with open(big_not_pe, "wb") as f:
                f.write(b"PK" + b"\0" * widget.EXE_MIN_BYTES)
            with self.assertRaises(RuntimeError):
                widget.check_exe(big_not_pe)

    def test_valid_exe_passes(self):
        with tempfile.TemporaryDirectory() as d:
            ok = os.path.join(d, "ok.exe")
            with open(ok, "wb") as f:
                f.write(b"MZ" + b"\0" * widget.EXE_MIN_BYTES)
            widget.check_exe(ok)       # 예외 없으면 통과


if __name__ == "__main__":
    unittest.main()
