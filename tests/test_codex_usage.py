import importlib.util
import json
import tempfile
import time
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "widget_main", _ROOT / "ClaudeUsageWidget.pyw")
widget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(widget)


def _reset_state():
    widget._codex_last = None
    widget._codex_plan = None
    widget._codex_plan_ts = 0
    widget._codex_cache = (None, None)


def _win(minutes, pct, resets_in=3600):
    return {"pct": float(pct), "minutes": minutes,
            "resets_at": time.time() + resets_in}


def _reading(windows, plan, ts=None):
    return {"windows": windows, "ts": ts or time.time(), "plan": plan}


def _by_minutes(merged):
    return {w["minutes"]: round(w["pct"]) for w in (merged or {})["windows"]}


class CodexMergeTests(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def tearDown(self):
        _reset_state()

    def test_missing_window_is_filled_within_same_plan(self):
        """5시간 창이 한 응답에서 빠져도 유지된다 (v3.14.0 동작)."""
        widget.codex_merge(_reading([_win(300, 42), _win(10080, 8)], "plus"))
        merged = widget.codex_merge(_reading([_win(10080, 9)], "plus"))
        self.assertEqual(_by_minutes(merged), {300: 42, 10080: 9})

    def test_plan_change_drops_old_windows(self):
        widget.codex_merge(_reading([_win(300, 100), _win(10080, 63)], "plus"))
        merged = widget.codex_merge(_reading([_win(10080, 7)], "prolite"))
        self.assertEqual(_by_minutes(merged), {10080: 7})

    def test_reading_from_previous_plan_is_ignored(self):
        now = time.time()
        widget.codex_merge(_reading([_win(10080, 7)], "prolite", ts=now))
        stale = _reading([_win(300, 99), _win(10080, 63)], "plus",
                         ts=now - 3600)
        merged = widget.codex_merge(stale)
        self.assertEqual(_by_minutes(merged), {10080: 7})

    def test_window_absent_too_long_is_dropped(self):
        widget.codex_merge(_reading([_win(300, 100), _win(10080, 63)], "plus"))
        widget._codex_last["windows"][0]["seen"] -= \
            widget.CODEX_WINDOW_KEEP_SEC + 1
        merged = widget.codex_merge(_reading([_win(10080, 64)], "plus"))
        self.assertEqual(_by_minutes(merged), {10080: 64})

    def test_unknown_plan_keeps_old_behaviour(self):
        """옛 Codex 기록에는 plan_type이 없다 — 그때는 예전처럼 이어 붙인다."""
        widget.codex_merge(_reading([_win(300, 42), _win(10080, 8)], None))
        merged = widget.codex_merge(_reading([_win(10080, 9)], None))
        self.assertEqual(_by_minutes(merged), {300: 42, 10080: 9})

    def test_reset_window_still_dropped(self):
        widget.codex_merge(
            _reading([_win(300, 100, resets_in=-10), _win(10080, 8)], "plus"))
        merged = widget.codex_merge(_reading([_win(10080, 9)], "plus"))
        self.assertEqual(_by_minutes(merged), {10080: 9})


class CodexTailSnapshotTests(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()
        _reset_state()

    def _write(self, events):
        path = Path(self.temp.name) / "rollout.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for stamp, plan, primary, secondary in events:
                f.write(json.dumps({
                    "timestamp": stamp,
                    "payload": {"rate_limits": {
                        "plan_type": plan,
                        "primary": primary,
                        "secondary": secondary,
                    }},
                }) + "\n")
        return str(path)

    @staticmethod
    def _rec(minutes, pct, resets_in=3600):
        return {"used_percent": float(pct), "window_minutes": minutes,
                "resets_at": time.time() + resets_in}

    def test_older_plan_events_do_not_revive_windows(self):
        path = self._write([
            ("2026-08-31T00:00:00Z", "plus",
             self._rec(300, 99), self._rec(10080, 63)),
            ("2026-08-31T04:00:00Z", "prolite", self._rec(10080, 7), None),
        ])
        snap = widget._codex_tail_snapshot(path)
        self.assertEqual(snap["plan"], "prolite")
        self.assertEqual([w["minutes"] for w in snap["windows"]], [10080])

    def test_same_plan_events_still_fill_missing_window(self):
        path = self._write([
            ("2026-08-31T00:00:00Z", "plus",
             self._rec(300, 42), self._rec(10080, 8)),
            ("2026-08-31T04:00:00Z", "plus", self._rec(10080, 9), None),
        ])
        snap = widget._codex_tail_snapshot(path)
        self.assertEqual(
            {w["minutes"]: round(w["pct"]) for w in snap["windows"]},
            {300: 42, 10080: 9})


class CodexPanelTests(unittest.TestCase):
    """Tk 없이 줄 구성만 본다 — 폰트가 필요한 폭 계산은 대역으로 막는다."""

    def _bar(self, windows, plan):
        bar = object.__new__(widget.FloatingBar)
        bar._panel_width = lambda lines: 0
        bar._disp_pct = lambda pct: pct
        bar._value_color = lambda pct: "#000000"
        bar.app = types.SimpleNamespace(
            cfg={},
            codex_usage={"windows": windows, "ts": time.time(), "plan": plan},
            skill_tracker=types.SimpleNamespace(
                snapshot=lambda: ({"codex": 1},)))
        return bar

    def test_weekly_only_plan_uses_app_row_with_tag(self):
        lines = self._bar([_win(10080, 7)], "prolite")._codex_panel()["lines"]
        self.assertEqual(lines[0][0], "Codex")
        self.assertEqual(lines[0][1], "7%")
        self.assertEqual(lines[0][5], "주간")       # 세션 값으로 오해 방지
        self.assertEqual(lines[1][0], "")

    def test_session_plus_weekly_layout_unchanged(self):
        lines = self._bar([_win(300, 47), _win(10080, 50)],
                          "plus")._codex_panel()["lines"]
        self.assertEqual((lines[0][0], lines[0][1], lines[0][5]),
                         ("Codex", "47%", ""))
        self.assertEqual((lines[1][0], lines[1][1]), ("주간", "50%"))


if __name__ == "__main__":
    unittest.main()
