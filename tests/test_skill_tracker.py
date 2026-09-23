import json
import sqlite3
import struct
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import skill_tracker


class SkillTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.temp.name)
        self.db_patch = mock.patch.object(skill_tracker, "DB_PATH", root / "usage.db")
        self.app_patch = mock.patch.object(skill_tracker, "APPDATA_DIR", root)
        self.pkg_patch = mock.patch.object(skill_tracker, "PACKAGES_DIR",
                                           root / "Packages")
        self.db_patch.start()
        self.app_patch.start()
        self.pkg_patch.start()
        skill_tracker._DB_FAULT = None
        skill_tracker._inbox_cache = (0.0, [])

    def tearDown(self):
        self.db_patch.stop()
        self.app_patch.stop()
        self.pkg_patch.stop()
        skill_tracker._DB_FAULT = None
        skill_tracker._inbox_cache = (0.0, [])
        self.temp.cleanup()

    SLASH = {
        "hook_event_name": "UserPromptExpansion",
        "session_id": "s9",
        "expansion_type": "slash_command",
        "command_name": "image-prompt-craft",
        "command_args": "secret words from the prompt",
        "prompt": "/image-prompt-craft secret words from the prompt",
    }

    def test_hook_queues_to_inbox_without_prompt_text(self):
        self.assertEqual(skill_tracker.queue_hook_payload("claude", self.SLASH), 1)
        inbox = self.root / skill_tracker.INBOX_NAME
        raw = inbox.read_text(encoding="utf-8")
        self.assertNotIn("secret", raw)             # the prompt never lands on disk
        self.assertFalse((self.root / "usage.db").exists())   # hook skips the DB
        self.assertEqual(skill_tracker.ingest_inbox(), 1)
        self.assertEqual(skill_tracker.ingest_inbox(), 0)     # offset remembered
        skill_tracker.queue_hook_payload("claude", self.SLASH)  # same event again
        self.assertEqual(skill_tracker.ingest_inbox(), 0)     # deduplicated by id
        row = skill_tracker.usage_rows("claude")[0]
        self.assertEqual((row["name"], row["manual_count"]),
                         ("image-prompt-craft", 1))

    def test_half_written_line_waits_for_its_newline(self):
        inbox = self.root / skill_tracker.INBOX_NAME
        event = skill_tracker.hook_events("claude", self.SLASH)[0]
        line = json.dumps(event).encode("utf-8")
        inbox.write_bytes(line[:20])
        self.assertEqual(skill_tracker.ingest_inbox(), 0)
        inbox.write_bytes(line + b"\n")
        self.assertEqual(skill_tracker.ingest_inbox(), 1)

    def test_forged_or_garbled_inbox_lines_are_skipped(self):
        inbox = self.root / skill_tracker.INBOX_NAME
        good = skill_tracker.hook_events("claude", self.SLASH)[0]
        bad_id = dict(good, id="not-a-hash")
        bad_client = dict(good, c="evil", id="b" * 64)
        inbox.write_text("\n".join([
            "{not json", json.dumps(bad_id), json.dumps(bad_client),
            json.dumps(good)]) + "\n", encoding="utf-8")
        self.assertEqual(skill_tracker.ingest_inbox(), 1)

    def test_shadow_inbox_from_msix_container_is_ingested(self):
        shadow_dir = (self.root / "Packages" / "Claude_x" / "LocalCache" /
                      "Roaming" / self.root.name)
        shadow_dir.mkdir(parents=True)
        event = skill_tracker.hook_events("claude", self.SLASH)[0]
        (shadow_dir / skill_tracker.INBOX_NAME).write_text(
            json.dumps(event) + "\n", encoding="utf-8")
        self.assertEqual(skill_tracker.ingest_inbox(), 1)
        self.assertEqual(skill_tracker.usage_rows("claude")[0]["total_count"], 1)

    def test_full_inbox_is_recycled_without_losing_lines(self):
        inbox = self.root / skill_tracker.INBOX_NAME
        with mock.patch.object(skill_tracker, "INBOX_ROTATE_BYTES", 10):
            skill_tracker.queue_hook_payload("claude", self.SLASH)
            self.assertEqual(skill_tracker.ingest_inbox(), 1)
            self.assertFalse(inbox.exists())            # recycled once ingested
            self.assertEqual(list(self.root.glob("*.ingest-*")), [])
            other = dict(self.SLASH, command_name="dataviz", prompt="/dataviz")
            skill_tracker.queue_hook_payload("claude", other)
            self.assertEqual(skill_tracker.ingest_inbox(), 1)   # offset reset
        names = {r["name"] for r in skill_tracker.usage_rows("claude")}
        self.assertEqual(names, {"image-prompt-craft", "dataviz"})

    def _corrupt_db_like_production(self):
        """A WAL-mode file one page shorter than its header says (40 of 41)."""
        db = self.root / "usage.db"
        for i in range(400):
            skill_tracker.record_event("claude", f"skill-{i % 7}", "auto", "t",
                                       "s", identity=f"id-{i}")
        skill_tracker._SCHEMA_READY.discard(str(db))
        data = db.read_bytes()
        page = struct.unpack(">H", data[16:18])[0]
        pages = struct.unpack(">I", data[28:32])[0]
        self.assertGreater(pages, 4)
        db.write_bytes(data[:(pages - 1) * page])      # drop the last page
        return db

    def test_corrupt_database_is_backed_up_and_rebuilt(self):
        self._corrupt_db_like_production()
        self.assertFalse(skill_tracker.check_database())
        svc = skill_tracker.TrackerService()
        with mock.patch.object(skill_tracker, "running_clients", return_value=()), \
                mock.patch.object(skill_tracker, "discover_skills", return_value=[]):
            svc.refresh(force=True)
        self.assertIsNotNone(svc.repair_report)
        self.assertGreater(svc.repair_report["events"], 300)   # most rows saved
        self.assertTrue(skill_tracker.check_database())
        backups = list(self.root.glob("usage.db.corrupt-*"))
        self.assertEqual(len(backups), 1)                     # original kept
        self.assertEqual(backups[0].stat().st_size % 4096, 0)
        self.assertIsNone(skill_tracker._DB_FAULT)
        # the repaired file keeps working
        self.assertTrue(skill_tracker.record_event("claude", "new", "auto", "t",
                                                   identity="after-repair"))

    def test_inventory_rewrites_only_on_change(self):
        rows = [{"client": "claude", "name": "a", "path": "C:/a/SKILL.md",
                 "source": "사용자"}]
        with mock.patch.object(skill_tracker, "discover_skills",
                               return_value=rows):
            skill_tracker.refresh_inventory()
            with closing(sqlite3.connect(self.root / "usage.db")) as con:
                first = con.execute("SELECT discovered_at FROM skills").fetchone()
            time.sleep(0.02)
            skill_tracker.refresh_inventory()
            with closing(sqlite3.connect(self.root / "usage.db")) as con:
                again = con.execute("SELECT discovered_at FROM skills").fetchone()
        self.assertEqual(first, again)

    def test_unchanged_codex_session_is_not_reread(self):
        sessions = self.root / "sessions" / "2026"
        sessions.mkdir(parents=True)
        f = sessions / "rollout-1.jsonl"
        f.write_text('{"type":"event"}\n', encoding="utf-8")
        with mock.patch.object(skill_tracker, "CODEX_SESSIONS",
                               self.root / "sessions"):
            skill_tracker.scan_codex_sessions()
            with closing(sqlite3.connect(self.root / "usage.db")) as con:
                first = con.execute("SELECT updated_at FROM file_state").fetchone()
            time.sleep(0.02)
            with mock.patch.object(Path, "open",
                                   side_effect=AssertionError("reopened")):
                skill_tracker.scan_codex_sessions()
            with closing(sqlite3.connect(self.root / "usage.db")) as con:
                again = con.execute("SELECT updated_at FROM file_state").fetchone()
        self.assertEqual(first, again)

    def test_claude_auto_and_manual_events(self):
        auto = {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Skill",
            "tool_input": {"skill": "image-prompt-craft"},
            "tool_use_id": "tool-1",
        }
        manual = {
            "hook_event_name": "UserPromptExpansion",
            "session_id": "s1",
            "expansion_type": "slash_command",
            "command_name": "image-prompt-craft",
            "command_args": "",
            "prompt": "/image-prompt-craft",
        }
        self.assertEqual(skill_tracker.record_hook_payload("claude", auto), 1)
        self.assertEqual(skill_tracker.record_hook_payload("claude", auto), 0)
        self.assertEqual(skill_tracker.record_hook_payload("claude", manual), 1)
        row = skill_tracker.usage_rows("claude")[0]
        self.assertEqual(row["auto_count"], 1)
        self.assertEqual(row["manual_count"], 1)
        self.assertEqual(row["total_count"], 2)

    def test_codex_ignores_unknown_shell_variables(self):
        context = {"explicit": set(), "turn_offset": 0}
        payload = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "$false $known-skill"}],
            },
        }
        added = skill_tracker._parse_codex_line(
            json.dumps(payload), "s2", 10, context, {"known-skill"}
        )
        self.assertEqual(added, 1)
        rows = skill_tracker.usage_rows("codex")
        self.assertEqual([row["name"] for row in rows], ["known-skill"])

    def test_codex_skill_read_is_once_per_turn(self):
        context = {"explicit": set(), "turn_offset": 20}
        payload = {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "arguments": {
                    "command": r"Get-Content C:\Users\me\.agents\skills\visualize\SKILL.md"
                },
            },
        }
        line = json.dumps(payload)
        self.assertEqual(
            skill_tracker._parse_codex_line(line, "s3", 30, context, set()), 1
        )
        self.assertEqual(
            skill_tracker._parse_codex_line(line, "s3", 40, context, set()), 0
        )
        row = skill_tracker.usage_rows("codex")[0]
        self.assertEqual(row["estimated_count"], 1)


if __name__ == "__main__":
    unittest.main()
