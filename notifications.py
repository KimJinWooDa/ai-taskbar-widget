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
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


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


def _read_state_file(path: Path) -> tuple[int, int]:
    """상태 파일 하나를 (읽은 개수, 지운 개수)로. 못 읽으면 (0, 0).

    utf-8-sig: 외부 도구가 BOM을 붙여 저장해도 읽음 상태가 통째로
    초기화되지 않게 — config.json이 실제로 당한 사고와 같은 유형이다.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        read = max(0, int(data.get("read_count", 0)))
        cleared = max(0, int(data.get("cleared_count", 0)))
        return read, cleared
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0


def _load_counts() -> tuple[int, int]:
    """(읽은 개수, 지운 개수). 지운 것은 읽은 것보다 클 수 없다."""
    read, cleared = _read_state_file(STATE_PATH)
    return max(read, cleared), cleared


def _save_counts(read: int, cleared: int) -> bool:
    """디스크 반영 여부를 돌려준다 — 실패는 호출자가 틱마다 재시도한다."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"read_count": int(read),
                                   "cleared_count": int(cleared)}),
                       encoding="utf-8")
        os.replace(tmp, STATE_PATH)     # 원자적 교체 — 중간에 죽어도 안 깨진다
        return True
    except OSError:
        return False                    # 읽음 표시를 못 남겨도 위젯은 계속 돈다


# MSIX 컨테이너 앱(예: Claude 데스크톱)이 자식으로 띄운 위젯은 %APPDATA% 쓰기가
# 패키지 그림자(Packages\<앱>\LocalCache\Roaming)로 가상화된다. 그 세션이 남긴
# 읽음 표시는 실파일에 반영되지 않아, 다음 정상 실행이 실파일을 읽으면 이미
# 읽은 알림이 되살아난다(2026-08-13 실측: 그림자 33/실파일 31 → 재시작마다
# 2건 부활). 시작할 때 그림자 사본의 더 큰 값을 흡수하고 사본을 지워 상태를
# 실파일 하나로 수렴시킨다.
_PACKAGES_DIR = (
    Path(os.environ["CLAUDE_NOTIFY_PACKAGES"])
    if os.environ.get("CLAUDE_NOTIFY_PACKAGES")
    else Path(os.environ["LOCALAPPDATA"]) / "Packages"
    if os.environ.get("LOCALAPPDATA")
    else None)


def _shadow_copies() -> list[Path]:
    if _PACKAGES_DIR is None:
        return []
    try:
        return sorted(_PACKAGES_DIR.glob(
            f"*/LocalCache/Roaming/{APPDATA_DIR.name}/{STATE_PATH.name}"))
    except OSError:
        return []


def _merge_shadow_state(read: int, cleared: int) -> tuple[int, int, bool]:
    """그림자 사본을 흡수해 (읽음, 지움, 값이 커졌는가)로 돌려준다.

    위젯 자신이 그림자 위에서 돌고 있으면(컨테이너 상속 실행 — STATE_PATH가
    그 사본으로 재지정된 상태) 백킹 파일이라 지우지 않는다. 낡은 사본은 값을
    보태지 않아도 지운다 — 남겨 두면 실파일을 계속 가려 가짜 미읽음/기읽음을
    만들기 때문이다.
    """
    grew = False
    for shadow in _shadow_copies():
        try:
            if os.path.samefile(shadow, STATE_PATH):
                continue
        except OSError:
            pass                # 실파일이 아직 없으면 같은 파일일 수도 없다
        s_read, s_cleared = _read_state_file(shadow)
        if s_read > read or s_cleared > cleared:
            read = max(read, s_read)
            cleared = max(cleared, s_cleared)
            grew = True
            log.info("adopted alert read-state from package shadow: %s",
                     shadow)
        try:
            shadow.unlink()
            log.info("removed package shadow of alert read-state: %s", shadow)
        except OSError:
            pass                # 못 지우면 다음 시작 때 다시 시도된다
    return max(read, cleared), cleared, grew


class NotificationService:
    """Thread-safe cached view of the routine notification log.

    The bar polls twice a second, so the log is only re-read when its
    modification stamp changes. Everything is best-effort: a missing or
    unreadable log simply means "no notifications", never an error the
    user has to see.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._entries: list[dict] = []
        self._stamp: tuple[float, int] | None = None
        self._pending_save = False
        read, cleared = _load_counts()
        read, cleared, grew = _merge_shadow_state(read, cleared)
        self._read_count, self._cleared = read, cleared
        if grew:
            self._persist()

    def _persist(self) -> None:
        """지금 카운트를 디스크에. 실패하면 refresh 틱마다 다시 쓴다.

        실패를 조용히 잊으면 메모리(읽음)와 디스크(안 읽음)가 갈라진 채
        돌다가 재시작 때 읽은 알림이 되살아난다. 저장 직전에 최신 카운트를
        다시 읽는 것은 동시 저장이 옛 값으로 새 값을 덮지 않게 하기 위해서다.
        """
        with self._io_lock:
            with self._lock:
                counts = (self._read_count, self._cleared)
            ok = _save_counts(*counts)
            with self._lock:
                was_pending = self._pending_save
                if not ok:
                    self._pending_save = True
                elif (self._read_count, self._cleared) == counts:
                    self._pending_save = False
        if not ok and not was_pending:
            log.warning("alert read-state save failed - retrying each tick")
        elif ok and was_pending:
            log.info("alert read-state save recovered")

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            pending = self._pending_save
        if pending:
            self._persist()             # 지난 틱에 못 남긴 읽음 표시 재시도
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
                clamped = True
            else:
                clamped = False
        if clamped:
            self._persist()

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
        self._persist()

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
        self._persist()
