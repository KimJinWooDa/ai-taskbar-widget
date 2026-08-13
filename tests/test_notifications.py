import json
import os
import subprocess
import sys
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
        # 그림자 검색까지 임시 폴더로 격리 — 실머신의 Packages를 긁거나
        # 실사용 그림자 사본을 지우면 안 된다.
        self.pkgs = root / "Packages"
        self.patches = [
            mock.patch.object(notifications, "LOG_PATH", self.log),
            mock.patch.object(notifications, "STATE_PATH", self.state),
            mock.patch.object(notifications, "_PACKAGES_DIR", self.pkgs),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def write(self, *lines):
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def shadow(self, read, cleared=0, pkg="SomeContainerApp_abc123"):
        """MSIX 그림자 사본과 같은 구조의 상태 파일을 만든다."""
        d = self.pkgs / pkg / "LocalCache" / "Roaming" / "ClaudeUsageWidget"
        d.mkdir(parents=True, exist_ok=True)
        p = d / self.state.name
        p.write_text(json.dumps({"read_count": read,
                                 "cleared_count": cleared}),
                     encoding="utf-8")
        return p

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

    def test_shadow_with_more_reads_is_adopted_and_removed(self):
        """MSIX 그림자에만 남은 읽음 표시를 흡수한다 — 부활 버그의 재현.

        컨테이너 신원으로 실행된 세션이 읽음 33을 그림자에 쓰고 실파일은
        31에 머문 실사고(2026-08-13). 다음 정상 실행은 그림자 값을 채택해
        읽은 알림을 되살리지 않고, 사본은 지워 실파일 하나로 수렴해야 한다.
        """
        self.write("[2026-08-13 10:00:00] A | 1",
                   "[2026-08-13 11:00:00] B | 2",
                   "[2026-08-13 12:00:00] C | 3")
        self.state.write_text(json.dumps({"read_count": 1}), encoding="utf-8")
        shadow = self.shadow(read=3)
        svc = notifications.NotificationService()
        _, unread = svc.snapshot()
        self.assertEqual(unread, [])
        self.assertFalse(shadow.exists())
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["read_count"], 3)

    def test_stale_shadow_is_removed_without_losing_reads(self):
        """낡은 그림자(더 작은 값)는 값을 되돌리지 않고 삭제만 한다.

        낡은 사본을 남겨 두면 컨테이너 실행 때 실파일을 가려
        '가짜 새 알림 수십 건'을 만든다(같은 날 실측된 다른 증상).
        """
        self.write("[2026-08-13 10:00:00] A | 1",
                   "[2026-08-13 11:00:00] B | 2")
        self.state.write_text(json.dumps({"read_count": 2}), encoding="utf-8")
        shadow = self.shadow(read=1)
        svc = notifications.NotificationService()
        _, unread = svc.snapshot()
        self.assertEqual(unread, [])
        self.assertFalse(shadow.exists())

    def test_failed_save_is_retried_on_next_tick(self):
        """저장 실패가 조용히 잊히지 않는다 — 다음 refresh 틱에 재저장."""
        self.write("[2026-08-13 10:00:00] A | 1")
        svc = notifications.NotificationService()
        with mock.patch.object(notifications.os, "replace",
                               side_effect=OSError("locked")):
            svc.mark_all_read()
        self.assertFalse(self.state.exists())   # 저장은 실제로 실패했다
        svc.refresh()                           # 바 폴링이 매 틱 부르는 경로
        self.assertTrue(self.state.exists())
        again = notifications.NotificationService()
        self.assertEqual(again.snapshot()[1], [])

    def test_force_killed_process_keeps_read_state(self):
        """읽음 → 강제 종료 → 재시작 후에도 배지가 0건으로 유지된다."""
        self.write("[2026-08-13 10:00:00] A | 1",
                   "[2026-08-13 11:00:00] B | 2")
        script = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import notifications\n"
            "svc = notifications.NotificationService()\n"
            "assert len(svc.snapshot()[1]) == 2\n"
            "svc.mark_all_read()\n"
            "os._exit(1)\n"             # 정리·플러시 없이 즉사 = 강제 종료
        )
        env = dict(os.environ,
                   CLAUDE_NOTIFY_LOG=str(self.log),
                   CLAUDE_NOTIFY_STATE=str(self.state),
                   CLAUDE_NOTIFY_PACKAGES=str(self.pkgs))
        repo = str(Path(notifications.__file__).parent)
        proc = subprocess.run([sys.executable, "-c", script, repo],
                              env=env, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        again = notifications.NotificationService()
        entries, unread = again.snapshot()
        self.assertEqual(len(entries), 2)
        self.assertEqual(unread, [])


if __name__ == "__main__":
    unittest.main()
