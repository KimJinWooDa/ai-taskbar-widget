import hashlib
import importlib.util
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
# 위젯 모듈은 불러오는 순간 %APPDATA%\ClaudeUsageWidget에 로그를 연다 —
# 테스트가 사용자의 실제 widget.log·설정에 흔적을 남기지 않게 임시 폴더로 돌린다
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="widget-test-")
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
        ver_t, ver_s, notes, assets, digests = widget.parse_release(text)
        self.assertEqual(ver_t, (3, 16, 0))
        self.assertEqual(ver_s, "3.16.0")
        self.assertEqual(notes, "notes")
        self.assertEqual(assets["AI-Skill-Widget.exe"],
                         "https://example.com/w.exe")

    def test_tag_without_v_prefix(self):
        ver_t, ver_s, _, _, _ = widget.parse_release(_release_json("3.2"))
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
        _, _, _, assets, _ = widget.parse_release(text)
        self.assertEqual(assets, {})


class DigestAndUrlTests(unittest.TestCase):
    def test_digests_parsed_and_bad_ones_dropped(self):
        good = "a" * 64
        text = json.dumps({"tag_name": "v3.18.0", "assets": [
            {"name": "AI-Skill-Widget.exe", "browser_download_url": "u1",
             "digest": f"sha256:{good.upper()}"},
            {"name": "SkillEventHook.exe", "browser_download_url": "u2",
             "digest": "md5:abc"},
            {"name": "x.exe", "browser_download_url": "u3"},
        ]})
        _, _, _, assets, digests = widget.parse_release(text)
        self.assertEqual(len(assets), 3)
        self.assertEqual(digests, {"AI-Skill-Widget.exe": good})

    def test_only_this_repo_release_urls_trusted(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_WIDGET_RELEASE_API", None)
            self.assertTrue(widget.trusted_asset_url(
                widget.ASSET_URL_PREFIX + "v3.18.0/AI-Skill-Widget.exe"))
            for bad in ("https://example.com/AI-Skill-Widget.exe",
                        "http://github.com/KimJinWooDa/ai-taskbar-widget/"
                        "releases/download/v1/x.exe",
                        "https://github.com/someone-else/ai-taskbar-widget/"
                        "releases/download/v1/x.exe"):
                self.assertFalse(widget.trusted_asset_url(bad), bad)

    def test_download_rejects_hash_mismatch_and_removes_file(self):
        payload = b"MZ" + b"\0" * 100

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        url = widget.ASSET_URL_PREFIX + "v9.0.0/AI-Skill-Widget.exe"
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(widget.urllib.request, "urlopen",
                                  side_effect=lambda *a, **k: _Resp(payload)):
            dst = os.path.join(d, "new.exe")
            with self.assertRaises(RuntimeError):
                widget.download_file(url, dst, "0" * 64)
            self.assertFalse(os.path.exists(dst))
            widget.download_file(url, dst,
                                 hashlib.sha256(payload).hexdigest())
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), payload)

    def test_download_refuses_untrusted_url(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(widget.urllib.request, "urlopen") as op:
            with self.assertRaises(RuntimeError):
                widget.download_file("https://example.com/x.exe",
                                     os.path.join(d, "x.exe"))
            op.assert_not_called()


CHANGELOG = """# 패치 이력

설명 문단.

## v3.18.0 — 2026-09-23

- **업데이트 소식을 바에서 바로 봅니다.** 설치 뒤 바에 패널이
  뜹니다
- `install.cmd` 더블클릭 설치

## v3.17.2 — 2026-09-08

- **빈 상자로 얼어붙은 바가 되살아납니다.** 설명

## v3.17.1 — 2026-09-05

- 부팅 직후 번쩍임 제거
"""


class ChangelogTests(unittest.TestCase):
    def test_entries_join_wrapped_lines(self):
        entries = widget.changelog_entries(CHANGELOG)
        self.assertEqual([e["v"] for e in entries],
                         ["3.18.0", "3.17.2", "3.17.1"])
        self.assertEqual(entries[0]["date"], "2026-09-23")
        self.assertEqual(entries[0]["items"][0],
                         "**업데이트 소식을 바에서 바로 봅니다.** 설치 뒤 바에 "
                         "패널이 뜹니다")
        self.assertEqual(len(entries[0]["items"]), 2)

    def test_headline_prefers_bold_text(self):
        entries = widget.changelog_entries(CHANGELOG)
        self.assertEqual(widget.headline(entries[0]["items"]),
                         "업데이트 소식을 바에서 바로 봅니다")
        self.assertEqual(widget.headline(entries[2]["items"]),
                         "부팅 직후 번쩍임 제거")
        self.assertEqual(widget.headline([]), "")

    def test_release_notes_between_filters_and_orders(self):
        rels = [
            {"tag_name": "v3.17.2", "body": "## v3.17.2 — a\n- old"},
            {"tag_name": "v3.19.0", "body": "## v3.19.0 — c\n- newest"},
            {"tag_name": "v3.18.0", "body": "## v3.18.0 — b\n- mid"},
            {"tag_name": "v3.18.5", "body": "draft", "draft": True},
            {"tag_name": "v3.18.6", "body": "pre", "prerelease": True},
            {"tag_name": "nightly", "body": "junk"},
        ]
        text = widget.release_notes_between(json.dumps(rels), (3, 17, 2),
                                            (3, 19, 0))
        self.assertEqual([e["v"] for e in widget.changelog_entries(text)],
                         ["3.19.0", "3.18.0"])
        self.assertEqual(widget.release_notes_between("<html>", (1,), (2,)),
                         "")


class WhatsNewTests(unittest.TestCase):
    """업데이트 직후 첫 실행 → 바에 패널, 창에는 이전~지금 사이 패치노트."""

    def _app(self, cfg):
        app = types.SimpleNamespace(cfg=cfg, update_info=None,
                                    _headlines={})
        for name in ("_note_version", "whats_new_view", "mark_whats_new_seen",
                     "range_headline", "whats_new_range"):
            setattr(app, name,
                    types.MethodType(getattr(widget.TrayApp, name), app))
        return app

    def setUp(self):
        self.saves = []
        self.patches = [
            mock.patch.object(widget, "save_config",
                              side_effect=lambda c: self.saves.append(dict(c))),
            mock.patch.object(widget, "local_changelog",
                              return_value=CHANGELOG),
            mock.patch.object(widget, "__version__", "3.18.0"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_upgrade_records_range_and_view_lists_between(self):
        app = self._app({"bar_right": 10, "last_run_version": "3.17.1"})
        self.assertFalse(app._note_version())
        self.assertEqual(app.cfg["whats_new"]["from"], "3.17.1")
        self.assertEqual(app.cfg["last_run_version"], "3.18.0")
        mode, sub, entries = app.whats_new_view()
        self.assertEqual(mode, "installed")
        self.assertIn("3.17.1", sub)
        self.assertEqual([e["v"] for e in entries], ["3.18.0", "3.17.2"])
        app.mark_whats_new_seen(mode)
        self.assertNotIn("whats_new", app.cfg)
        self.assertEqual(app.whats_new_view()[0], "history")

    def test_upgrade_from_pre_tracking_version_shows_current_only(self):
        app = self._app({"bar_right": 10})
        app._note_version()
        _, _, entries = app.whats_new_view()
        self.assertEqual([e["v"] for e in entries], ["3.18.0"])

    def test_pre_tracking_upgrade_lists_everything_since_tracking_began(self):
        # 3.17.2 이하에는 last_run_version이 없다 — 3.18.1로 바로 올라와도
        # 3.18.0 패치노트까지 보여야 한다
        text = "## v3.18.1 — b\n- **재시도**\n\n" + CHANGELOG.split("\n", 3)[3]
        with mock.patch.object(widget, "local_changelog", return_value=text), \
                mock.patch.object(widget, "__version__", "3.18.1"):
            app = self._app({"bar_right": 10})
            app._note_version()
            mode, sub, entries = app.whats_new_view()
        self.assertEqual(mode, "installed")
        self.assertEqual([e["v"] for e in entries], ["3.18.1", "3.18.0"])
        self.assertEqual(sub, "v3.18.1 업데이트 완료")

    def test_unseen_notes_are_chained_not_overwritten(self):
        # 3.17.1 → 3.18.0 소식을 안 연 채 3.18.1로 올라와도 3.18.0이 남아야 한다
        app = self._app({"bar_right": 10, "last_run_version": "3.18.0",
                         "whats_new": {"from": "3.17.1", "to": "3.18.0",
                                       "at": 1}})
        with mock.patch.object(widget, "__version__", "3.18.1"):
            app._note_version()
        self.assertEqual(app.cfg["whats_new"]["from"], "3.17.1")
        self.assertEqual(app.cfg["whats_new"]["to"], "3.18.1")

    def test_seen_notes_start_a_new_range(self):
        app = self._app({"bar_right": 10, "last_run_version": "3.18.0"})
        with mock.patch.object(widget, "__version__", "3.18.1"):
            app._note_version()
        self.assertEqual(app.cfg["whats_new"]["from"], "3.18.0")

    def test_range_headline_prefers_the_biggest_release(self):
        text = ("## v3.18.1 — b\n- 작은 수정\n\n"
                "## v3.18.0 — a\n- **큰 기능** 설명\n- 둘\n- 셋\n\n"
                "## v3.17.2 — z\n- 옛것 하나\n- 옛것 둘\n- 셋\n- 넷\n")
        app = self._app({})
        self.assertEqual(app.range_headline((3, 17, 2), (3, 18, 1), text),
                         "큰 기능")
        self.assertEqual(app.range_headline((3, 18, 0), (3, 18, 1), text),
                         "작은 수정")
        self.assertEqual(app.range_headline((3, 18, 1), (3, 18, 1), text), "")

    def test_fresh_install_has_no_whats_new(self):
        app = self._app({})
        self.assertTrue(app._note_version())
        self.assertNotIn("whats_new", app.cfg)

    def test_same_version_restart_changes_nothing(self):
        app = self._app({"bar_right": 10, "last_run_version": "3.18.0"})
        self.assertFalse(app._note_version())
        self.assertNotIn("whats_new", app.cfg)
        self.assertEqual(self.saves, [])

    def test_available_update_view_and_seen(self):
        app = self._app({"bar_right": 10, "last_run_version": "3.18.0"})
        notes = "## v3.19.0 — x\n- **새 기능** 설명\n\n## v3.18.0 — y\n- 옛것"
        app.update_info = ("3.19.0", notes, {}, {})
        mode, _, entries = app.whats_new_view()
        self.assertEqual(mode, "available")
        self.assertEqual([e["v"] for e in entries], ["3.19.0"])
        self.assertEqual(app.range_headline((3, 18, 0), (3, 19, 0), notes),
                         "새 기능")
        app.mark_whats_new_seen(mode)
        self.assertEqual(app.cfg["update_seen"], "3.19.0")


class DpapiConfigTests(unittest.TestCase):
    def test_token_is_encrypted_at_rest_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(widget, "CONFIG_PATH",
                                  os.path.join(d, "config.json")):
            cfg = {"bar_right": 5, "setup_token": "sk-ant-oat01-TEST"}
            widget.save_config(cfg)
            with open(widget.CONFIG_PATH, encoding="utf-8") as f:
                raw = f.read()
            self.assertNotIn("sk-ant", raw)
            self.assertIn(widget.TOKEN_ENC_KEY, raw)
            self.assertEqual(widget.load_config(), cfg)

    def test_legacy_plain_token_still_loads(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(widget, "CONFIG_PATH",
                                  os.path.join(d, "config.json")):
            with open(widget.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"setup_token": "sk-ant-legacy"}, f)
            self.assertEqual(widget.load_config()["setup_token"],
                             "sk-ant-legacy")

    def test_undecryptable_token_is_ignored(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(widget, "CONFIG_PATH",
                                  os.path.join(d, "config.json")):
            with open(widget.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({widget.TOKEN_ENC_KEY: "bm90LWRwYXBp", "x": 1}, f)
            self.assertEqual(widget.load_config(), {"x": 1})


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task><Triggers><LogonTrigger>{enabled}<UserId>PC\\me</UserId></LogonTrigger>
</Triggers><Settings><Enabled>true</Enabled></Settings></Task>"""


class AutostartTests(unittest.TestCase):
    """메뉴 체크 = 로그온 예약 작업의 트리거 (예전엔 없는 vbs만 봤다)."""

    def test_logon_trigger_state_from_xml(self):
        self.assertTrue(widget.logon_trigger_state(TASK_XML.format(enabled="")))
        self.assertFalse(widget.logon_trigger_state(
            TASK_XML.format(enabled="<Enabled>false</Enabled>")))
        self.assertIsNone(widget.logon_trigger_state("<Task><Triggers/></Task>"))

    def _run(self, rc, xml=""):
        # 위젯은 subprocess를 함수 안에서 import한다 — 모듈 함수를 바꿔 끼운다
        return mock.patch("subprocess.run", return_value=types.SimpleNamespace(
            returncode=rc, stdout=xml.encode("utf-8"), stderr=b"denied"))

    def test_task_autostart_reads_the_task(self):
        with self._run(0, TASK_XML.format(enabled="")):
            self.assertEqual(widget.task_autostart(), (True, True))
        with self._run(0, TASK_XML.format(enabled="<Enabled>false</Enabled>")):
            self.assertEqual(widget.task_autostart(), (True, False))
        with self._run(1):
            self.assertEqual(widget.task_autostart(), (False, False))

    def test_set_task_autostart_toggles_triggers_without_a_window(self):
        with self._run(0) as run:
            widget.set_task_autostart(False)
        args, kwargs = run.call_args
        self.assertIn("$tr.Enabled = $false", args[0][-1])
        self.assertEqual(kwargs["creationflags"], widget.NO_WINDOW)
        with self._run(1):
            with self.assertRaises(OSError):
                widget.set_task_autostart(True)

    def _app(self, autostart):
        app = types.SimpleNamespace(autostart=autostart, q=mock.Mock(),
                                    icon=None)
        for name in ("_toggle_autostart", "_refresh_autostart"):
            setattr(app, name,
                    types.MethodType(getattr(widget.TrayApp, name), app))
        return app

    def test_toggle_uses_the_task_when_present(self):
        app = self._app((True, True))
        with mock.patch.object(widget, "set_task_autostart") as st, \
                mock.patch.object(widget, "task_autostart",
                                  return_value=(True, False)), \
                mock.patch.object(widget, "startup_installed",
                                  return_value=False), \
                mock.patch.object(widget, "install_startup") as inst:
            app._toggle_autostart()
        st.assert_called_once_with(False)
        inst.assert_not_called()                # no duplicate vbs any more
        self.assertEqual(app.autostart, (True, False))

    def test_toggle_falls_back_to_vbs_without_a_task(self):
        app = self._app((False, False))
        with mock.patch.object(widget, "task_autostart",
                               return_value=(False, False)), \
                mock.patch.object(widget, "startup_installed",
                                  return_value=True), \
                mock.patch.object(widget, "install_startup") as inst:
            app._toggle_autostart()
        inst.assert_called_once()
        self.assertEqual(app.autostart, (False, True))

    def test_legacy_vbs_removed_when_task_handles_autostart(self):
        app = self._app((False, False))
        with mock.patch.object(widget, "task_autostart",
                               return_value=(True, True)), \
                mock.patch.object(widget, "startup_installed",
                                  return_value=True), \
                mock.patch.object(widget, "uninstall_startup") as rm:
            app._refresh_autostart()
        rm.assert_called_once()
        self.assertEqual(app.autostart, (True, True))
        app.q.put.assert_called_with(("menu",))


class VersionLineTests(unittest.TestCase):
    def _line(self, update_info, checked):
        app = types.SimpleNamespace(update_info=update_info,
                                    update_checked=checked)
        with mock.patch.object(widget, "__version__", "3.18.3"):
            return widget.TrayApp.version_line(app)

    def test_states(self):
        self.assertEqual(self._line(None, False), "현재 버전 v3.18.3")
        self.assertEqual(self._line(None, True), "현재 버전 v3.18.3 · 최신")
        self.assertEqual(self._line(("3.19.0", "", {}, {}), True),
                         "현재 버전 v3.18.3 · 새 버전 v3.19.0 있음")


class RenameRetryTests(unittest.TestCase):
    """PyInstaller EXE가 지연 import로 자기 파일을 잠깐 여는 순간을 넘긴다."""

    def test_transient_sharing_violation_is_retried(self):
        calls = []

        def flaky(src, dst):
            calls.append((src, dst))
            if len(calls) < 3:
                raise PermissionError(32, "in use")

        with mock.patch.object(widget.os, "rename", side_effect=flaky), \
                mock.patch.object(widget.time, "sleep"):
            widget.rename_retry("a.exe", "a.exe.old")
        self.assertEqual(len(calls), 3)

    def test_persistent_lock_still_raises(self):
        with mock.patch.object(widget.os, "rename",
                               side_effect=PermissionError(32, "in use")), \
                mock.patch.object(widget.time, "sleep") as slept:
            with self.assertRaises(PermissionError):
                widget.rename_retry("a.exe", "a.exe.old", tries=4)
        self.assertEqual(slept.call_count, 3)

    def test_other_errors_are_not_retried(self):
        with mock.patch.object(widget.os, "rename",
                               side_effect=FileNotFoundError()) as ren:
            with self.assertRaises(FileNotFoundError):
                widget.rename_retry("a.exe", "a.exe.old")
        self.assertEqual(ren.call_count, 1)


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
