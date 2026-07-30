import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skill_tracker


class SkillTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_patch = mock.patch.object(skill_tracker, "DB_PATH", root / "usage.db")
        self.app_patch = mock.patch.object(skill_tracker, "APPDATA_DIR", root)
        self.db_patch.start()
        self.app_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.app_patch.stop()
        self.temp.cleanup()

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
