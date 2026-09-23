"""바 배경 동기화 — 작업표시줄 색이 바뀌면 1초 안에 따라가는지 (화면 없이)."""
import importlib.util
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
# 위젯 모듈은 불러오는 순간 %APPDATA%에 로그를 연다 — 임시 폴더로 돌린다
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="widget-test-")
_spec = importlib.util.spec_from_file_location(
    "widget_main", _ROOT / "ClaudeUsageWidget.pyw")
widget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(widget)

GRAY, BEIGE = (218, 218, 218), (231, 220, 207)


def _bgra(width, height, color_at):
    """color_at(x) → (r, g, b) 인 가로 줄무늬 BGRA 바이트."""
    row = bytearray()
    for x in range(width):
        r, g, b = color_at(x)
        row += bytes((b, g, r, 255))
    return bytes(row) * height


def _bar():
    bar = widget.FloatingBar(types.SimpleNamespace(stop_evt=threading.Event()))
    bar.SIDE, bar._gen = 12, 1
    bar._camo_geom, bar._camo_pending = None, None
    bar._camo_ref, bar._camo_at = (None, None), widget.time.time()
    bar._snip_active = False
    return bar


class SampleComposeTests(unittest.TestCase):
    def test_sides_means_and_busy_detection(self):
        bar = _bar()
        # 가상 화면 0..1000, 바 x=100 폭 50 → 뜬 구간 88..162
        span = _bgra(74, 4, lambda x: GRAY if x < 12
                     else ((0, 0, 0) if x % 2 else (255, 255, 255))
                     if x >= 62 else BEIGE)
        with mock.patch.object(widget, "blit_bgra", return_value=span), \
                mock.patch.object(widget.ctypes.windll.user32,
                                  "GetSystemMetrics",
                                  side_effect=lambda i: 0 if i == 76 else 1000):
            left, right = bar._sample_sides((100, 0, 50, 4))
        self.assertEqual(left[1], GRAY)
        self.assertTrue(left[2])            # 매끈 — 배경으로 쓴다
        self.assertFalse(right[2])          # 흑백 줄무늬 = 아이콘·글자 — 안 쓴다

    def test_side_off_screen_is_none(self):
        bar = _bar()
        with mock.patch.object(widget, "blit_bgra",
                               return_value=_bgra(62, 4, lambda x: GRAY)), \
                mock.patch.object(widget.ctypes.windll.user32,
                                  "GetSystemMetrics",
                                  side_effect=lambda i: 0 if i == 76 else 1000):
            left, right = bar._sample_sides((0, 0, 50, 4))  # 왼쪽 끝에 붙음
        self.assertIsNone(left)
        self.assertEqual(right[1], GRAY)

    def test_compose_blends_left_to_right(self):
        from PIL import Image
        bar = _bar()
        lft = (Image.new("RGB", (12, 4), GRAY), GRAY, True)
        rgt = (Image.new("RGB", (12, 4), BEIGE), BEIGE, True)
        img = bar._compose(lft, rgt, 100, 4)
        self.assertEqual(img.getpixel((0, 1)), GRAY)
        self.assertEqual(img.getpixel((99, 1)), BEIGE)
        busy = (Image.new("RGB", (12, 4), BEIGE), BEIGE, False)
        self.assertEqual(bar._compose(lft, busy, 100, 4).getpixel((99, 1)),
                         GRAY)                  # 거친 쪽은 버리고 한쪽만
        self.assertIsNone(bar._compose(None, busy, 100, 4))


class SyncLoopTests(unittest.TestCase):
    """0.5초 간격 표본을 차례로 먹여 언제 새 배경을 만드는지 본다."""

    def _run(self, samples, ref=(GRAY, GRAY)):
        """표본 목록을 먹이고, 몇 번째 표본 뒤에 새 배경이 만들어졌는지 돌려준다.

        루프는 떠 둔 배경이 입혀질 때까지 다음 표본을 뜨지 않는다 — 실제로는
        틱(0.5초)이 입힌다. 여기서는 대기(wait) 한 번을 틱 한 번으로 친다.
        """
        from PIL import Image
        bar = _bar()
        bar._camo_geom = (100, 0, 50, 4)
        bar._camo_ref = ref
        made, taken = [], []
        feed = iter(samples)

        class Tick:
            def wait(self, timeout):
                if bar._camo_pending is not None:
                    made.append(len(taken))
                    bar._camo_pending = None
                return False

        bar.app = types.SimpleNamespace(stop_evt=Tick())

        def sample(geom):
            try:
                lc, rc = next(feed)
            except StopIteration:
                bar._gen += 1               # 표본이 떨어지면 루프를 끝낸다
                return None
            taken.append(1)
            side = (lambda c: (Image.new("RGB", (12, 4), c), c, True))
            return side(lc), side(rc)

        with mock.patch.object(bar, "_sample_sides", side_effect=sample):
            bar._camo_loop(1)
        if bar._camo_pending is not None:
            made.append(len(taken))
        return made

    def test_left_change_adopted_after_two_samples(self):
        self.assertEqual(self._run([(BEIGE, GRAY), (BEIGE, GRAY)]), [2])

    def test_single_blip_is_ignored(self):
        self.assertEqual(self._run([(BEIGE, GRAY), (GRAY, GRAY),
                                    (BEIGE, GRAY), (GRAY, GRAY)]), [])

    def test_continuously_changing_color_still_followed(self):
        # 게임·영상이 뒤에서 돌면 매번 다른 색 — 예전엔 영영 못 따라갔다
        shades = [(200 + i * 5, 210, 190) for i in range(4)]
        self.assertEqual(self._run([(c, GRAY) for c in shades]), [2, 4])

    def test_right_side_needs_longer_confirmation(self):
        self.assertEqual(self._run([(GRAY, BEIGE)] * 3), [])
        self.assertEqual(self._run([(GRAY, BEIGE)] * 4), [4])

    def test_hidden_bar_is_not_sampled(self):
        bar = _bar()
        bar._camo_geom = None
        calls = []

        def sample(geom):
            calls.append(geom)

        with mock.patch.object(bar, "_sample_sides", side_effect=sample), \
                mock.patch.object(widget.FloatingBar, "CAMO_SYNC_SEC", 0.001):
            t = threading.Thread(target=bar._camo_loop, args=(1,))
            t.start()
            t.join(0.05)
            bar._gen += 1
            t.join(1)
        self.assertEqual(calls, [])

    def test_pending_for_an_old_position_is_dropped(self):
        bar = _bar()
        bar._camo_pending = ((100, 0, 50, 4), object(), (None, None))
        with mock.patch.object(bar, "_apply_camo") as apply:
            bar._apply_pending_camo((140, 0, 50, 4))    # 그사이 옮겼다
        apply.assert_not_called()
        self.assertIsNone(bar._camo_pending)
        bar._camo_pending = ((100, 0, 50, 4), "img", ("l", "r"))
        with mock.patch.object(bar, "_apply_camo") as apply:
            bar._apply_pending_camo((100, 0, 50, 4))
        apply.assert_called_once_with("img", "l", "r", "sync")


if __name__ == "__main__":
    unittest.main()
