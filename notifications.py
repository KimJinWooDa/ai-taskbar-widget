# -*- coding: utf-8 -*-
"""Unread routine notifications for the taskbar bar.

Scheduled tasks ("routines") run unattended, and their result is easy to miss:
the built-in push notification is suppressed while the user is sitting at the
app, and a toast is gone once it fades. Tasks therefore append one line per
result to a plain text log, and this module turns that log into an unread
count the bar can show until it is actually read.

Nothing here is tied to one machine or one set of tasks. Any task that appends
a line participates -- see notify.ps1 for the writer side and README for the
one-line contract. The log is read-only here; the widget never writes it.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path


HOME = Path.home()
# 루틴이 결과를 적는 곳 — Claude Code가 예약 작업을 두는 디렉터리와 같다.
NOTIFY_DIR = Path(os.environ.get(
    "CLAUDE_NOTIFY_DIR", HOME / ".claude" / "scheduled-tasks"))
LOG_PATH = Path(os.environ.get("CLAUDE_NOTIFY_LOG",
                               NOTIFY_DIR / "notifications.log"))
# 읽음 표시는 위젯이 소유하는 상태라 위젯 데이터 폴더에 둔다.
# (~/.claude 는 Claude 것이므로 위젯 파일로 더럽히지 않는다.)
APPDATA_DIR = Path(os.environ.get("APPDATA", HOME)) / "ClaudeUsageWidget"
STATE_PATH = Path(os.environ.get("CLAUDE_NOTIFY_STATE",
                                 APPDATA_DIR / "notifications-read.json"))

# "[2026-07-31 17:40:51] 제목 | 본문" — 제목에는 파이프를 쓰지 않는다.
_LINE_RE = re.compile(
    r"^\[(?P<when>[^\]]+)\]\s*(?P<title>[^|]*?)\s*\|\s*(?P<body>.*)$")
# 선택 꼬리표 "|run:<경로>" — 루틴이 "이걸 실행하면 된다"를 명시할 때만 붙인다.
# 맨 끝에서만 찾고 파이프를 안 넘는다: Windows 경로에는 파이프를 못 쓰므로
# 본문에 파이프가 섞여 있어도 경계가 흔들리지 않는다.
_RUN_RE = re.compile(r"\s*\|\s*run:\s*(?P<run>[^|]+?)\s*$")
_WHEN_FMT = "%Y-%m-%d %H:%M:%S"


def _parse(line: str, index: int) -> dict:
    """한 줄을 항목으로. 형식이 안 맞아도 버리지 않고 본문으로 살린다."""
    m = _LINE_RE.match(line)
    if m:
        when, title, body = m["when"], m["title"], m["body"]
    else:
        when, title, body = "", "", line
    run = ""
    rm = _RUN_RE.search(body)
    if rm:
        run = rm["run"]
        body = body[:rm.start()].rstrip()
    try:
        ts = time.mktime(time.strptime(when, _WHEN_FMT))
    except (ValueError, OverflowError):
        ts = 0.0
    return {"index": index, "when": when, "ts": ts,
            "title": title, "body": body, "run": run}


def run_target(row: dict) -> Path | None:
    """알림이 가리키는 '열 것'. 없거나 못 믿을 값이면 None.

    로그는 평문이라 이 프로세스로 쓸 수 있는 것이면 무엇이든 한 줄 붙일 수
    있다. 그래서 여기서는 **명령줄을 절대 받지 않는다** — 디스크에 실제로
    있는 절대경로 하나만 인정하고, 실행은 탐색기 더블클릭과 같은 방식으로
    한다. 인자·파이프·리다이렉션이 낄 자리가 없다.
    """
    raw = (row.get("run") or "").strip().strip('"')
    if not raw:
        return None
    try:
        path = Path(os.path.expandvars(raw))
        if not path.is_absolute() or not path.exists():
            return None            # 상대경로는 기준이 모호, 없는 것은 못 연다
    except (OSError, ValueError):
        return None
    return path


def entries() -> list[dict]:
    """오래된 것부터. 로그가 없거나 못 읽으면 빈 목록.

    utf-8-sig로 읽는 이유: PowerShell의 `Add-Content -Encoding UTF8`은 파일을
    처음 만들 때 BOM을 넣는다. 그대로 두면 첫 줄이 "\\ufeff["로 시작해 형식
    판정이 어긋나고, 첫 알림만 원문이 그대로 보인다(실측).
    """
    try:
        text = LOG_PATH.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    lines = [ln.strip().lstrip("﻿") for ln in text.splitlines()]
    return [_parse(ln, i) for i, ln in enumerate(x for x in lines if x)]


def ago(ts: float, now: float | None = None) -> str:
    """'3분 전' 같은 짧은 경과 표기. 시각을 못 읽었으면 빈 문자열."""
    if not ts:
        return ""
    secs = max(0, int((time.time() if now is None else now) - ts))
    if secs < 60:
        return "방금"
    if secs < 3600:
        return f"{secs // 60}분 전"
    if secs < 86400:
        return f"{secs // 3600}시간 전"
    return f"{secs // 86400}일 전"


def _load_counts() -> tuple[int, int]:
    """(읽은 개수, 지운 개수). 지운 것은 읽은 것보다 클 수 없다."""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        read = max(0, int(data.get("read_count", 0)))
        cleared = max(0, int(data.get("cleared_count", 0)))
        return max(read, cleared), cleared
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0


def _save_counts(read: int, cleared: int) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"read_count": int(read),
                                   "cleared_count": int(cleared)}),
                       encoding="utf-8")
        os.replace(tmp, STATE_PATH)     # 원자적 교체 — 중간에 죽어도 안 깨진다
    except OSError:
        pass                            # 읽음 표시를 못 남겨도 위젯은 계속 돈다


class NotificationService:
    """Thread-safe cached view of the routine notification log.

    The bar polls twice a second, so the log is only re-read when its
    modification stamp changes. Everything is best-effort: a missing or
    unreadable log simply means "no notifications", never an error the
    user has to see.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._stamp: tuple[float, int] | None = None
        self._read_count, self._cleared = _load_counts()

    def refresh(self, force: bool = False) -> None:
        try:
            st = LOG_PATH.stat()
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = None
        with self._lock:
            if not force and stamp == self._stamp:
                return
        rows = entries() if stamp is not None else []
        with self._lock:
            self._stamp = stamp
            self._entries = rows
            if self._read_count > len(rows) or self._cleared > len(rows):
                # 로그를 지웠거나 갈아치웠다 — 남은 것은 다 읽은 것으로 본다.
                self._read_count = min(self._read_count, len(rows))
                self._cleared = min(self._cleared, len(rows))
                keep = (self._read_count, self._cleared)
            else:
                keep = None
        if keep is not None:
            _save_counts(*keep)

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        """(전체, 안 읽은 것). 둘 다 오래된 것부터."""
        self.refresh()
        with self._lock:
            return list(self._entries), list(self._entries[self._read_count:])

    def mark_all_read(self, upto: int | None = None) -> None:
        """읽음 처리. `upto`를 주면 그 개수까지만 읽은 것으로 본다.

        창에 실제로 보여준 개수를 넘겨야 한다 — 목록을 뜬 뒤 창을 여는 짧은
        사이에 새 줄이 붙었을 때, 사용자가 못 본 것까지 삼키지 않기 위해서다.
        생략하면 지금 로그 전체를 읽은 것으로 한다.
        """
        if upto is None:
            self.refresh()
        with self._lock:
            total = len(self._entries)
            n = total if upto is None else max(0, min(int(upto), total))
            if n <= self._read_count:
                return                  # 읽음 표시는 뒤로 물러나지 않는다
            self._read_count = n
            counts = (self._read_count, self._cleared)
        _save_counts(*counts)

    def cleared_count(self) -> int:
        """'모두 지우기'로 목록에서 숨긴 항목 수 — 이 인덱스부터 보여준다."""
        with self._lock:
            return self._cleared

    def clear_all(self) -> None:
        """창의 '모두 지우기'. 로그 파일은 루틴의 기록이라 건드리지 않고
        (이 모듈은 읽기 전용), 지금까지의 항목을 목록에서 숨기기만 한다.
        이후에 오는 알림은 평소처럼 쌓인다.
        """
        self.refresh()
        with self._lock:
            n = len(self._entries)
            if n <= self._cleared:
                return
            self._cleared = n
            self._read_count = max(self._read_count, n)
            counts = (self._read_count, self._cleared)
        _save_counts(*counts)
