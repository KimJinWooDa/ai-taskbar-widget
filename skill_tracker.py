# -*- coding: utf-8 -*-
"""Local-only skill inventory and invocation counters for Claude Code and Codex.

The tracker never calls a model or stores prompt text. Claude events are recorded
by a command hook; Codex events are inferred from local session JSONL because
Codex does not currently expose a dedicated skill-use hook.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path


HOME = Path.home()
APPDATA_DIR = Path(os.environ.get("APPDATA", HOME)) / "ClaudeUsageWidget"
DB_PATH = Path(os.environ.get("SKILL_TRACKER_DB", APPDATA_DIR / "skill-usage.db"))
CODEX_SESSIONS = HOME / ".codex" / "sessions"

_SKILL_PATH_RE = re.compile(
    r"(?:^|[\\/])skills[\\/](?P<name>[^\\/\s\"']+)[\\/]SKILL\.md",
    re.IGNORECASE,
)
_EXPLICIT_CODEX_RE = re.compile(r"(?<![\w$])\$([A-Za-z0-9_.:-]+)")
_FRONTMATTER_NAME_RE = re.compile(
    r"(?m)^name\s*:\s*[\"']?([^\"'\r\n#]+?)\s*[\"']?\s*$"
)


class _PE32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def running_clients() -> tuple[str, ...]:
    """Return active supported clients using cheap process-name inspection."""
    override = os.environ.get("SKILL_WIDGET_CLIENTS")
    if override:
        requested = [x.strip().lower() for x in override.split(",")]
        return tuple(x for x in ("claude", "codex") if x in requested)
    names: set[str] = set()
    try:
        kernel = ctypes.windll.kernel32
        snap = kernel.CreateToolhelp32Snapshot(2, 0)
        if snap in (0, -1):
            return ()
        try:
            item = _PE32W()
            item.dwSize = ctypes.sizeof(_PE32W)
            ok = kernel.Process32FirstW(snap, ctypes.byref(item))
            while ok:
                names.add(item.szExeFile.lower())
                ok = kernel.Process32NextW(snap, ctypes.byref(item))
        finally:
            kernel.CloseHandle(snap)
    except Exception:
        return ()
    clients = []
    if "claude.exe" in names:
        clients.append("claude")
    # Exact match avoids Codex command runners and code-mode helper processes.
    if "codex.exe" in names:
        clients.append("codex")
    return tuple(clients)


def _connect() -> sqlite3.Connection:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=2000")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS skills (
            client TEXT NOT NULL,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            source TEXT NOT NULL,
            discovered_at REAL NOT NULL,
            PRIMARY KEY (client, path)
        );
        CREATE INDEX IF NOT EXISTS skills_name_idx ON skills(client, name);

        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            happened_at REAL NOT NULL,
            client TEXT NOT NULL,
            skill TEXT NOT NULL,
            mode TEXT NOT NULL,
            source TEXT NOT NULL,
            session_id TEXT
        );
        CREATE INDEX IF NOT EXISTS events_rollup_idx
            ON events(client, skill, happened_at);

        CREATE TABLE IF NOT EXISTS file_state (
            path TEXT PRIMARY KEY,
            offset INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    try:
        con.execute(
            "ALTER TABLE file_state ADD COLUMN turn_offset INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    return con


def _event_id(*parts: object) -> str:
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def record_event(
    client: str,
    skill: str,
    mode: str,
    source: str,
    session_id: str = "",
    identity: str = "",
    happened_at: float | None = None,
) -> bool:
    """Insert one deduplicated event. Returns True only for a new row."""
    client = client.strip().lower()
    skill = skill.strip().strip("/$").lower()
    if client not in {"claude", "codex"} or not skill:
        return False
    mode = mode if mode in {"auto", "manual", "estimated"} else "estimated"
    ts = happened_at or time.time()
    eid = _event_id(client, skill, mode, source, session_id, identity or ts)
    try:
        with closing(_connect()) as con:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO events
                    (event_id, happened_at, client, skill, mode, source, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (eid, ts, client, skill, mode, source, session_id or None),
            )
            con.commit()
            return bool(cur.rowcount)
    except sqlite3.Error:
        return False


def _skill_name(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:8192]
        match = _FRONTMATTER_NAME_RE.search(head)
        if match:
            return match.group(1).strip()
    except OSError:
        pass
    return path.parent.name


def _roots() -> dict[str, tuple[tuple[Path, str], ...]]:
    return {
        "claude": (
            (HOME / ".claude" / "skills", "사용자"),
            (HOME / ".claude" / "plugins" / "cache", "플러그인"),
        ),
        "codex": (
            (HOME / ".agents" / "skills", "사용자"),
            (HOME / ".codex" / "skills", "Codex"),
            (HOME / ".codex" / "plugins" / "cache", "플러그인"),
        ),
    }


def discover_skills() -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for client, roots in _roots().items():
        for root, source in roots:
            if not root.is_dir():
                continue
            try:
                paths = root.rglob("SKILL.md")
                for path in paths:
                    key = (client, os.path.normcase(str(path)))
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(
                        {
                            "client": client,
                            "name": _skill_name(path),
                            "path": str(path),
                            "source": source,
                        }
                    )
            except OSError:
                continue
    return found


def refresh_inventory() -> int:
    rows = discover_skills()
    now = time.time()
    try:
        with closing(_connect()) as con:
            con.execute("DELETE FROM skills")
            con.executemany(
                """
                INSERT INTO skills(client, name, path, source, discovered_at)
                VALUES (:client, :name, :path, :source, :discovered_at)
                """,
                ({**row, "discovered_at": now} for row in rows),
            )
            con.commit()
    except sqlite3.Error:
        return 0
    return len(rows)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _message_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    bits = []
    for item in content or []:
        if isinstance(item, dict):
            value = item.get("text") or item.get("input_text")
            if isinstance(value, str):
                bits.append(value)
    return "\n".join(bits)


def _parse_codex_line(
    line: str,
    session_id: str,
    offset: int,
    context: dict,
    known_skills: set[str],
) -> int:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return 0
    payload = obj.get("payload") or {}
    if not isinstance(payload, dict):
        return 0
    added = 0
    happened_at = None
    stamp = obj.get("timestamp")
    if isinstance(stamp, str):
        try:
            happened_at = dt.datetime.fromisoformat(
                stamp.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            pass
    ptype = payload.get("type")
    if obj.get("type") == "response_item" and ptype == "message" \
            and payload.get("role") == "user":
        text = _message_text(payload)
        context["turn_offset"] = offset
        context["explicit"].clear()
        for name in _EXPLICIT_CODEX_RE.findall(text):
            skill = name.lower()
            if skill not in known_skills:
                continue
            context["explicit"].add(skill)
            added += int(
                record_event(
                    "codex", skill, "manual", "session-explicit", session_id,
                    identity=f"turn:{offset}:{skill}",
                    happened_at=happened_at,
                )
            )
        return added

    # Newer app-server rollouts may persist a dedicated skill input item.
    if ptype == "skill":
        name = payload.get("name") or payload.get("skill")
        if isinstance(name, str):
            skill = name.lower()
            context["explicit"].add(skill)
            return int(
                record_event(
                    "codex", skill, "manual", "session-skill-item", session_id,
                    identity=f"turn:{context['turn_offset']}:{skill}",
                    happened_at=happened_at,
                )
            )

    if obj.get("type") != "response_item" or ptype not in {
        "function_call", "custom_tool_call"
    }:
        return 0
    body = payload.get("arguments") if ptype == "function_call" \
        else payload.get("input")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
    names = {
        match.group("name").lower()
        for text in _strings(body)
        for match in _SKILL_PATH_RE.finditer(text)
    }
    for skill in names:
        if skill in context["explicit"]:
            continue
        added += int(
            record_event(
                "codex", skill, "estimated", "session-skill-read", session_id,
                identity=f"turn:{context['turn_offset']}:{skill}",
                happened_at=happened_at,
            )
        )
    return added


def scan_codex_sessions(limit: int = 24, initial_bytes: int = 8 * 1024 * 1024) -> int:
    """Incrementally scan recent rollouts; reads at most ``initial_bytes`` per new file."""
    if not CODEX_SESSIONS.is_dir():
        return 0
    try:
        paths = sorted(
            CODEX_SESSIONS.rglob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
    except OSError:
        return 0
    added = 0
    try:
        con = _connect()
    except sqlite3.Error:
        return 0
    try:
        known_skills = {
            row["name"].lower()
            for row in con.execute(
                "SELECT DISTINCT name FROM skills WHERE client='codex'"
            )
        }
        for path in paths:
            try:
                size = path.stat().st_size
                state = con.execute(
                    "SELECT offset, turn_offset FROM file_state WHERE path=?",
                    (str(path),)
                ).fetchone()
                start = int(state["offset"]) if state else max(0, size - initial_bytes)
                if start > size:
                    start = 0
                context = {
                    "explicit": set(),
                    "turn_offset": int(state["turn_offset"]) if state else start,
                }
                with path.open("rb") as src:
                    src.seek(start)
                    if start:
                        src.readline()  # discard a partial JSONL record
                    while True:
                        pos = src.tell()
                        raw = src.readline()
                        if not raw:
                            break
                        added += _parse_codex_line(
                            raw.decode("utf-8", "replace"),
                            path.stem,
                            pos,
                            context,
                            known_skills,
                        )
                    end = src.tell()
                con.execute(
                    """
                    INSERT INTO file_state(path, offset, updated_at, turn_offset)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        offset=excluded.offset, updated_at=excluded.updated_at,
                        turn_offset=excluded.turn_offset
                    """,
                    (str(path), end, time.time(), context["turn_offset"]),
                )
                con.commit()
            except (OSError, sqlite3.Error):
                continue
    finally:
        con.close()
    return added


def _start_of_today() -> float:
    now = dt.datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def usage_rows(client: str | None = None) -> list[dict]:
    """Return every installed/observed skill with all-time and today counters."""
    params: list[object] = [_start_of_today()]
    client_sql = ""
    if client:
        client_sql = "WHERE client = ?"
        params.append(client)
    sql = f"""
        WITH names AS (
            SELECT client, name FROM skills
            UNION
            SELECT client, skill AS name FROM events
        ),
        inventory AS (
            SELECT client, name, COUNT(*) AS copies,
                   GROUP_CONCAT(DISTINCT source) AS sources
            FROM skills GROUP BY client, name
        ),
        totals AS (
            SELECT client, skill AS name,
                   SUM(mode = 'auto') AS auto_count,
                   SUM(mode = 'manual') AS manual_count,
                   SUM(mode = 'estimated') AS estimated_count,
                   SUM(happened_at >= ?) AS today_count,
                   COUNT(*) AS total_count,
                   MAX(happened_at) AS last_used
            FROM events GROUP BY client, skill
        )
        SELECT n.client, n.name,
               COALESCE(i.copies, 0) AS copies,
               COALESCE(i.sources, '') AS sources,
               COALESCE(t.auto_count, 0) AS auto_count,
               COALESCE(t.manual_count, 0) AS manual_count,
               COALESCE(t.estimated_count, 0) AS estimated_count,
               COALESCE(t.today_count, 0) AS today_count,
               COALESCE(t.total_count, 0) AS total_count,
               t.last_used
        FROM names n
        LEFT JOIN inventory i USING(client, name)
        LEFT JOIN totals t USING(client, name)
        {client_sql}
        ORDER BY total_count DESC, n.name COLLATE NOCASE
    """
    try:
        with closing(_connect()) as con:
            return [dict(row) for row in con.execute(sql, params)]
    except sqlite3.Error:
        return []


def _frontmatter_description(path: str) -> str:
    """SKILL.md 머리말(frontmatter)의 description 값 — 없으면 ''.

    yaml 라이브러리 없이 처리한다: 한 줄 값, 따옴표 값, `>`/`|` 블록과
    이어지는 들여쓴 줄까지만 지원하면 실전 스킬 파일은 전부 커버된다.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(65536)
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    parts: list[str] = []
    in_desc = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if in_desc:
            if line[:1] in (" ", "\t"):
                parts.append(line.strip())
                continue
            break
        if line.startswith("description:"):
            first = line[len("description:"):].strip()
            in_desc = True
            if first not in (">", ">-", "|", "|-", ""):
                parts.append(first)
    desc = " ".join(parts).strip()
    if len(desc) >= 2 and desc[0] in "'\"" and desc[-1] == desc[0]:
        desc = desc[1:-1]
    return desc


def skill_description(client: str, name: str) -> str:
    """설치된 스킬의 SKILL.md description — 사본이 여럿이면 첫 파일 기준."""
    try:
        with closing(_connect()) as con:
            row = con.execute(
                "SELECT path FROM skills WHERE client = ? AND name = ? "
                "ORDER BY path LIMIT 1",
                (client, name),
            ).fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    return _frontmatter_description(row[0])


def compact_summary(client: str) -> dict:
    rows = usage_rows(client)
    installed = sum(1 for row in rows if row["copies"])
    today = sum(row["today_count"] for row in rows)
    total = sum(row["total_count"] for row in rows)
    top = [row for row in rows if row["total_count"]][:2]
    if len(top) < 2:
        known = {row["name"] for row in top}
        extras = [row for row in rows if row["name"] not in known]
        top.extend(extras[: 2 - len(top)])
    return {
        "client": client,
        "installed": installed,
        "today": today,
        "total": total,
        "top": top,
    }


class TrackerService:
    """Thread-safe cached facade used by the Tk UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: tuple[str, ...] = ()
        self._summary: dict[str, dict] = {}
        self._rows: list[dict] = []
        self._inventory_at = 0.0
        self._scan_at = 0.0
        self._desc_cache: dict[tuple[str, str], str] = {}

    def describe(self, client: str, name: str) -> str:
        key = (client, name)
        if key not in self._desc_cache:
            self._desc_cache[key] = skill_description(client, name)
        return self._desc_cache[key]

    def refresh(self, force=False):
        now = time.time()
        if force or now - self._inventory_at >= 60:
            refresh_inventory()
            self._inventory_at = now
        clients = running_clients()
        if "codex" in clients and (force or now - self._scan_at >= 3):
            scan_codex_sessions()
            self._scan_at = now
        summary = {client: compact_summary(client) for client in clients}
        rows = usage_rows()
        with self._lock:
            self._clients = clients
            self._summary = summary
            self._rows = rows

    def snapshot(self) -> tuple[tuple[str, ...], dict[str, dict], list[dict]]:
        with self._lock:
            return self._clients, dict(self._summary), list(self._rows)


def record_hook_payload(client: str, payload: dict) -> int:
    """Extract a Claude/Codex hook payload without retaining prompt contents."""
    event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
    session = str(payload.get("session_id") or payload.get("sessionId") or "")
    if client == "claude" and event == "PreToolUse" \
            and payload.get("tool_name") == "Skill":
        tool_input = payload.get("tool_input") or {}
        name = tool_input.get("skill") or tool_input.get("name") \
            or tool_input.get("command")
        if isinstance(name, str):
            return int(
                record_event(
                    "claude", name, "auto", "hook-pre-tool", session,
                    identity=str(payload.get("tool_use_id") or time.time_ns()),
                )
            )
    if client == "claude" and event == "UserPromptExpansion" \
            and payload.get("expansion_type") == "slash_command":
        name = payload.get("command_name")
        if isinstance(name, str):
            return int(
                record_event(
                    "claude", name, "manual", "hook-slash", session,
                    identity=f"{payload.get('prompt', '')}:{payload.get('command_args', '')}",
                )
            )
    if client == "codex" and event == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        return sum(
            int(
                record_event(
                    "codex", name, "manual", "hook-explicit", session,
                    identity=f"{payload.get('turn_id', '')}:{name}",
                )
            )
            for name in _EXPLICIT_CODEX_RE.findall(prompt)
        )
    return 0
