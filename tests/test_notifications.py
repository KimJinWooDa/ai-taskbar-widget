import tempfile
import unittest
from pathlib import Path
from unittest import mock

import notifications


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.log = root / "notifications.log"
        self.state = root / "notifications-read.json"
        self.log_patch = mock.patch.object(notifications, "LOG_PATH", self.log)
        self.state_patch = mock.patch.object(notifications, "STATE_PATH",
                                             self.state)
        self.log_patch.start()
        self.state_patch.start()

    def tearDown(self):
        self.log_patch.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def write(self, *lines):
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_missing_log_means_no_notifications(self):
        """로그가 없으면 조용해야 한다 — 이게 '아이콘 없음'의 근거다."""
        svc = notifications.NotificationService()
        entries, unread = svc.snapshot()
        self.assertEqual(entries, [])
        self.assertEqual(unread, [])

    def test_parses_title_body_and_time(self):
        self.write("[2026-07-31 17:40:51] 깁미더코인 주간점검 | 디스크 여유 18GB")
        svc = notifications.NotificationService()
        _, unread = svc.snapshot()
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]["title"], "깁미더코인 주간점검")
        self.assertEqual(unread[0]["body"], "디스크 여유 18GB")
        self.assertGreater(unread[0]["ts"], 0)

    def test_body_may_contain_pipe(self):
        """본문에 파이프가 섞여도 제목만 잘라내고 나머지는 보존한다."""
        self.write("[2026-07-31 17:40:51] 리그 승격심사 | 고래 승격 | 낙타 기각")
        svc = notifications.NotificationService()
        _, unread = svc.snapshot()
        self.assertEqual(unread[0]["title"], "리그 승격심사")
        self.assertEqual(unread[0]["body"], "고래 승격 | 낙타 기각")

    def test_malformed_line_is_kept_not_dropped(self):
        self.write("형식이 안 맞는 줄")
        svc = notifications.NotificationService()
        _, unread = svc.snapshot()
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]["body"], "형식이 안 맞는 줄")
        self.assertEqual(unread[0]["ts"], 0.0)

    def test_mark_all_read_clears_unread_and_persists(self):
        self.write("[2026-07-31 10:00:00] A | 1",
                   "[2026-07-31 11:00:00] B | 2")
        svc = notifications.NotificationService()
        self.assertEqual(len(svc.snapshot()[1]), 2)
        svc.mark_all_read()
        self.assertEqual(svc.snapshot()[1], [])
        # 위젯을 껐다 켜도 읽음 상태가 유지된다
        again = notifications.NotificationService()
        entries, unread = again.snapshot()
        self.assertEqual(len(entries), 2)
        self.assertEqual(unread, [])

    def test_new_line_after_read_is_unread_again(self):
        self.write("[2026-07-31 10:00:00] A | 1")
        svc = notifications.NotificationService()
        svc.mark_all_read()
        self.write("[2026-07-31 10:00:00] A | 1",
                   "[2026-07-31 12:00:00] B | 2")
        _, unread = svc.snapshot()
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]["title"], "B")

    def test_truncated_log_does_not_resurrect_old_alerts(self):
        """로그를 지우면 남은 것을 다 읽은 것으로 본다 — 알림 폭탄 방지."""
        self.write("[2026-07-31 10:00:00] A | 1",
                   "[2026-07-31 11:00:00] B | 2")
        svc = notifications.NotificationService()
        svc.mark_all_read()
        self.log.write_text("", encoding="utf-8")
        entries, unread = svc.snapshot()
        self.assertEqual(entries, [])
        self.assertEqual(unread, [])

    def test_bom_written_by_powershell_is_stripped(self):
        """PowerShell Add-Content -Encoding UTF8은 첫 줄 앞에 BOM을 넣는다."""
        self.log.write_bytes(
            "﻿[2026-07-31 17:40:51] 주간점검 | 디스크 18GB\n"
            .encode("utf-8")
        )
        svc = notifications.NotificationService()
        _, unread = svc.snapshot()
        self.assertEqual(unread[0]["title"], "주간점검")
        self.assertEqual(unread[0]["body"], "디스크 18GB")

    def test_blank_lines_are_ignored(self):
        self.write("[2026-07-31 10:00:00] A | 1", "", "   ")
        svc = notifications.NotificationService()
        self.assertEqual(len(svc.snapshot()[0]), 1)

    def test_ago_wording(self):
        now = 1_000_000.0
        self.assertEqual(notifications.ago(0), "")
        self.assertEqual(notifications.ago(now - 5, now), "방금")
        self.assertEqual(notifications.ago(now - 120, now), "2분 전")
        self.assertEqual(notifications.ago(now - 7200, now), "2시간 전")
        self.assertEqual(notifications.ago(now - 172800, now), "2일 전")


if __name__ == "__main__":
    unittest.main()
