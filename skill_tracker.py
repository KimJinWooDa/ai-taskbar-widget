# -*- coding: utf-8 -*-
"""Local-only skill inventory and invocation counters for Claude Code and Codex.

The tracker never calls a model or stores prompt text. Claude events are recorded
by a command hook; Codex events are inferred from local session JSONL because
Codex does not currently expose a dedicated skill-use hook.

Only the widget process writes the SQLite database. Hooks append one JSON line
per event to an inbox file and the widget ingests it (see ``queue_hook_payload``
and ``ingest_inbox``): a hook launched from inside an MSIX container (Claude
desktop) gets its new files -- SQLite's -wal/-shm included -- redirected to the
package shadow folder, so a hook and the widget writing the same database saw
two different write-ahead logs and silently lost each other's pages. That split
corrupted real databases (reproduced 2026-09-23).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

log = logging.getLogger(__name__)


HOME = Path.home()
APPDATA_DIR = Path(os.environ.get("APPDATA", HOME)) / "ClaudeUsageWidget"
DB_PATH = Path(os.environ.get("SKILL_TRACKER_DB", APPDATA_DIR / "skill-usage.db"))
CODEX_SESSIONS = HOME / ".codex" / "sessions"
INBOX_NAME = "skill-events.jsonl"
INBOX_ROTATE_BYTES = 256 * 1024     # fully ingested inboxes past this are recycled
# Where MSIX containers redirect new %APPDATA% files (see module docstring).
PACKAGES_DIR = (
    Path(os.environ["SKILL_TRACKER_PACKAGES"])
    if os.environ.get("SKILL_TRACKER_PACKAGES")
    else Path(os.environ["LOCALAPPDATA"]) / "Packages"
    if os.environ.get("LOCALAPPDATA")
    else None)

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


_SCHEMA = """
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

        CREATE TABLE IF NOT EXISTS translations (
            client TEXT NOT NULL,
            name TEXT NOT NULL,
            src_hash TEXT NOT NULL,
            desc_ko TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (client, name)
        );
"""
# Paths whose schema this process already ensured. The DDL used to run on every
# connection -- several times per 2-second tick -- for a schema that never
# changes after the first call.
_SCHEMA_READY: set[str] = set()
# Last corruption error seen by any query; the widget repairs the file once
# (TrackerService.refresh). Every caller swallows sqlite3.Error so the UI keeps
# running -- which is exactly how a corrupt file went unnoticed for weeks.
_DB_FAULT: str | None = None
_CORRUPT_MARKERS = ("malformed", "not a database", "file is encrypted")


def _apply_schema(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    try:
        con.execute(
            "ALTER TABLE file_state ADD COLUMN turn_offset INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass


def _connect() -> sqlite3.Connection:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=2000")
        key = str(DB_PATH)
        if key not in _SCHEMA_READY:
            con.execute("PRAGMA journal_mode=WAL")
            _apply_schema(con)
            _SCHEMA_READY.add(key)
    except sqlite3.Error as e:
        con.close()
        _note_db_error(e)
        raise
    return con


def _is_corrupt(err: BaseException) -> bool:
    return isinstance(err, sqlite3.DatabaseError) and any(
        m in str(err).lower() for m in _CORRUPT_MARKERS)


def _note_db_error(err: BaseException) -> None:
    global _DB_FAULT
    if _is_corrupt(err):
        _DB_FAULT = str(err)


def _event_id(*parts: object) -> str:
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _normalize_event(client: str, skill: str, mode: str):
    client = client.strip().lower()
    skill = skill.strip().strip("/$").lower()
    if client not in {"claude", "codex"} or not skill:
        return None
    mode = mode if mode in {"auto", "manual", "estimated"} else "estimated"
    return client, skill, mode


def _insert_event(con: sqlite3.Connection, eid: str, ts: float, client: str,
                  skill: str, mode: str, source: str, session_id: str) -> bool:
    cur = con.execute(
        """
        INSERT OR IGNORE INTO events
            (event_id, happened_at, client, skill, mode, source, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (eid, ts, client, skill, mode, source, session_id or None),
    )
    return bool(cur.rowcount)


def record_event(
    client: str,
    skill: str,
    mode: str,
    source: str,
    session_id: str = "",
    identity: str = "",
    happened_at: float | None = None,
    con: sqlite3.Connection | None = None,
) -> bool:
    """Insert one deduplicated event. Returns True only for a new row.

    With ``con`` the insert joins the caller's transaction (no commit here), so
    a scan can commit its events together with the file offset they came from;
    errors then propagate so the caller can roll the whole batch back.
    """
    norm = _normalize_event(client, skill, mode)
    if norm is None:
        return False
    client, skill, mode = norm
    ts = happened_at or time.time()
    eid = _event_id(client, skill, mode, source, session_id, identity or ts)
    if con is not None:
        return _insert_event(con, eid, ts, client, skill, mode, source,
                             session_id)
    try:
        with closing(_connect()) as own:
            added = _insert_event(own, eid, ts, client, skill, mode, source,
                                  session_id)
            own.commit()
            return added
    except sqlite3.Error as e:
        _note_db_error(e)
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
    """Sync the skills table with disk. Writes only when something changed --
    it used to rewrite every row each minute even when nothing had moved."""
    rows = discover_skills()
    want = {(r["client"], r["name"], r["path"], r["source"]) for r in rows}
    now = time.time()
    try:
        with closing(_connect()) as con:
            have = {tuple(r) for r in con.execute(
                "SELECT client, name, path, source FROM skills")}
            if have != want:
                with con:               # one transaction: all rows or none
                    con.execute("DELETE FROM skills")
                    con.executemany(
                        """
                        INSERT INTO skills(client, name, path, source, discovered_at)
                        VALUES (:client, :name, :path, :source, :discovered_at)
                        """,
                        ({**row, "discovered_at": now} for row in rows),
                    )
    except sqlite3.Error as e:
        _note_db_error(e)
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
    con: sqlite3.Connection | None = None,
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
                    happened_at=happened_at, con=con,
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
                    happened_at=happened_at, con=con,
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
                happened_at=happened_at, con=con,
            )
        )
    return added


def _recent_jsonl(root: Path, limit: int) -> list[tuple[float, int, Path]]:
    """(mtime, size, path) of the newest ``limit`` .jsonl files under ``root``.

    os.scandir hands back the size and mtime Windows already returned while
    listing the folder, so this costs no per-file stat: 1,200 session files take
    ~6 ms of CPU against ~37 ms for rglob + stat (measured 2026-09-23).
    """
    found: list[tuple[float, int, Path]] = []
    stack = [str(root)]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.name.endswith(".jsonl"):
                            st = entry.stat()
                            found.append((st.st_mtime, st.st_size,
                                          Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return found[:limit]


def _save_offset(con: sqlite3.Connection, path: str, offset: int,
                 turn_offset: int = 0) -> None:
    con.execute(
        """
        INSERT INTO file_state(path, offset, updated_at, turn_offset)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            offset=excluded.offset, updated_at=excluded.updated_at,
            turn_offset=excluded.turn_offset
        """,
        (path, offset, time.time(), turn_offset),
    )


def scan_codex_sessions(limit: int = 24, initial_bytes: int = 8 * 1024 * 1024) -> int:
    """Incrementally scan recent rollouts; reads at most ``initial_bytes`` per new file.

    Files whose size still equals the stored offset are skipped without being
    opened or written -- every file used to be reopened and its offset
    re-committed on each 3-second tick while Codex ran.
    """
    if not CODEX_SESSIONS.is_dir():
        return 0
    recent = _recent_jsonl(CODEX_SESSIONS, limit)
    if not recent:
        return 0
    added = 0
    try:
        con = _connect()
    except sqlite3.Error as e:
        _note_db_error(e)
        return 0
    try:
        known_skills = {
            row["name"].lower()
            for row in con.execute(
                "SELECT DISTINCT name FROM skills WHERE client='codex'"
            )
        }
        for _mtime, size, path in recent:
            try:
                state = con.execute(
                    "SELECT offset, turn_offset FROM file_state WHERE path=?",
                    (str(path),)
                ).fetchone()
                if state and int(state["offset"]) == size:
                    continue            # nothing new since the last scan
                start = int(state["offset"]) if state else max(0, size - initial_bytes)
                if start > size:
                    start = 0
                context = {
                    "explicit": set(),
                    "turn_offset": int(state["turn_offset"]) if state else start,
                }
                found = 0
                with con:   # this file's events and its new offset commit together
                    with path.open("rb") as src:
                        src.seek(start)
                        if start:
                            src.readline()  # discard a partial JSONL record
                        while True:
                            pos = src.tell()
                            raw = src.readline()
                            if not raw:
                                break
                            found += _parse_codex_line(
                                raw.decode("utf-8", "replace"),
                                path.stem,
                                pos,
                                context,
                                known_skills,
                                con=con,
                            )
                        end = src.tell()
                    _save_offset(con, str(path), end, context["turn_offset"])
                added += found
            except (OSError, sqlite3.Error) as e:
                _note_db_error(e)
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
    except sqlite3.Error as e:
        _note_db_error(e)
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


def desc_hash(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def cached_translation(client: str, name: str, src_hash: str) -> str:
    """저장된 한국어 번역 — 원문 해시가 다르면(설명이 바뀌면) 무효."""
    try:
        with closing(_connect()) as con:
            row = con.execute(
                "SELECT desc_ko FROM translations "
                "WHERE client = ? AND name = ? AND src_hash = ?",
                (client, name, src_hash),
            ).fetchone()
    except sqlite3.Error as e:
        _note_db_error(e)
        return ""
    return row[0] if row else ""


def store_translation(client: str, name: str, src_hash: str,
                      desc_ko: str) -> None:
    try:
        with closing(_connect()) as con:
            con.execute(
                "INSERT OR REPLACE INTO translations"
                "(client, name, src_hash, desc_ko, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (client, name, src_hash, desc_ko, time.time()),
            )
            con.commit()
    except sqlite3.Error as e:
        _note_db_error(e)
        pass


def skill_paths(client: str, name: str) -> list[str]:
    """스킬의 SKILL.md 파일 경로들 (사본이 여러 곳이면 전부)."""
    try:
        with closing(_connect()) as con:
            rows = con.execute(
                "SELECT path FROM skills WHERE client = ? AND name = ? "
                "ORDER BY path", (client, name)).fetchall()
    except sqlite3.Error as e:
        _note_db_error(e)
        return []
    return [r[0] for r in rows]


def skill_description(client: str, name: str) -> str:
    """설치된 스킬의 SKILL.md description — 사본이 여럿이면 첫 파일 기준."""
    try:
        with closing(_connect()) as con:
            row = con.execute(
                "SELECT path FROM skills WHERE client = ? AND name = ? "
                "ORDER BY path LIMIT 1",
                (client, name),
            ).fetchone()
    except sqlite3.Error as e:
        _note_db_error(e)
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


# ---------------------------------------------------------------- repair
_TABLE_COLUMNS = {
    "skills": ("client", "name", "path", "source", "discovered_at"),
    "events": ("event_id", "happened_at", "client", "skill", "mode", "source",
               "session_id"),
    "file_state": ("path", "offset", "updated_at", "turn_offset"),
    "translations": ("client", "name", "src_hash", "desc_ko", "updated_at"),
}


def check_database() -> bool:
    """False when the file is corrupt. Absent or merely busy counts as healthy."""
    global _DB_FAULT
    if not Path(DB_PATH).exists():
        return True
    try:
        with closing(_connect()) as con:
            rows = con.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as e:
        _note_db_error(e)
        return not _is_corrupt(e)
    if len(rows) == 1 and rows[0][0] == "ok":
        return True
    _DB_FAULT = "quick_check: " + "; ".join(str(r[0]) for r in rows[:3])
    return False


def _pad_to_header(path: Path) -> None:
    """Grow a truncated copy back to the page count its own header promises.

    The corrupt files we saw were one page shorter than their header said (40
    of 41) and SQLite then refused even to read the schema. Zero pages at the
    tail make every intact page readable again. Only ever run on a copy.
    """
    with open(path, "r+b") as f:
        head = f.read(100)
        if len(head) < 100 or not head.startswith(b"SQLite format 3\x00"):
            return
        page = struct.unpack(">H", head[16:18])[0]
        page = 65536 if page == 1 else page
        count = struct.unpack(">I", head[28:32])[0]
        if head[24:28] != head[92:96]:  # header page count is not trustworthy
            return
        size = f.seek(0, os.SEEK_END)
        if page and count and size < count * page:
            f.write(b"\x00" * (count * page - size))


def _salvage(path: Path) -> dict[str, list[tuple]]:
    """Every row still readable from each known table (partial tables kept)."""
    kept: dict[str, list[tuple]] = {}
    con = sqlite3.connect(path)
    try:
        for table, cols in _TABLE_COLUMNS.items():
            got: list[tuple] = []
            try:
                for row in con.execute(f"SELECT {', '.join(cols)} FROM {table}"):
                    got.append(tuple(row))
            except sqlite3.Error:
                pass                    # keep whatever came before the bad page
            kept[table] = got
    finally:
        con.close()
    return kept


def recover_database() -> dict[str, int] | None:
    """Back up a corrupt database, rebuild it from every readable row, swap it in.

    The original is never deleted -- it stays beside the new file as
    ``skill-usage.db.corrupt-<time>`` (with its -wal/-shm when present), and the
    rebuilt file only replaces it once complete. Returns rows kept per table, or
    None when there is no file. Raises OSError when the swap is unsafe (a stale
    -wal that cannot be removed would be replayed onto the new file).
    """
    global _DB_FAULT
    db = Path(DB_PATH)
    if not db.exists():
        _DB_FAULT = None
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"{db.name}.corrupt-{stamp}")
    shutil.copy2(db, backup)
    for side in ("-wal", "-shm"):
        src = db.with_name(db.name + side)
        if src.exists():
            shutil.copy2(src, backup.with_name(backup.name + side))
    fresh = db.with_name(db.name + ".rebuild")
    work = Path(tempfile.mkdtemp(prefix="skill-db-salvage-"))
    try:
        probe = work / "probe.db"
        shutil.copy2(backup, probe)
        wal = backup.with_name(backup.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, work / "probe.db-wal")
        _pad_to_header(probe)
        try:
            kept = _salvage(probe)
        except sqlite3.Error:
            kept = {}
        fresh.unlink(missing_ok=True)
        con = sqlite3.connect(fresh)
        try:
            _apply_schema(con)
            with con:
                for table, cols in _TABLE_COLUMNS.items():
                    if kept.get(table):
                        con.executemany(
                            f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
                            f"VALUES ({', '.join('?' * len(cols))})",
                            kept[table])
        finally:
            con.close()
        # The old -wal/-shm belong to the corrupt file (copies are in the
        # backup). A -wal left beside the new file would be replayed onto it.
        for side in ("-wal", "-shm"):
            try:
                db.with_name(db.name + side).unlink(missing_ok=True)
            except OSError:
                pass
        if db.with_name(db.name + "-wal").exists():
            raise OSError("the old write-ahead log is still in use")
        os.replace(fresh, db)
    finally:
        fresh.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    _SCHEMA_READY.discard(str(db))
    _DB_FAULT = None
    counts = {table: len(rows) for table, rows in kept.items()}
    log.warning("skill db was corrupt - backed up to %s, rebuilt with %s",
                backup.name, counts)
    return counts


# ---------------------------------------------------------------- hook inbox
_inbox_cache: tuple[float, list[Path]] = (0.0, [])


def _inbox_paths() -> list[Path]:
    """The real inbox plus copies MSIX containers redirected into package
    shadows. The shadow search walks every installed package, so it is
    cached for a minute."""
    global _inbox_cache
    now = time.time()
    real = APPDATA_DIR / INBOX_NAME
    if now - _inbox_cache[0] >= 60:
        shadows: list[Path] = []
        if PACKAGES_DIR is not None:
            try:
                shadows = sorted(PACKAGES_DIR.glob(
                    f"*/LocalCache/Roaming/{APPDATA_DIR.name}/{INBOX_NAME}"))
            except OSError:
                pass
        _inbox_cache = (now, shadows)
    return [real] + [p for p in _inbox_cache[1] if p != real]


def _ingest_lines(con: sqlite3.Connection, blob: bytes) -> int:
    """Insert inbox lines; anything malformed or forged-looking is skipped."""
    added = 0
    for raw in blob.splitlines():
        try:
            item = json.loads(raw.decode("utf-8"))
            eid = str(item["id"])
            ts = float(item["t"])
            norm = _normalize_event(str(item["c"]), str(item["s"]),
                                    str(item["m"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if norm is None or len(norm[1]) > 128 \
                or not re.fullmatch(r"[0-9a-f]{64}", eid):
            continue
        added += int(_insert_event(
            con, eid, ts, *norm, str(item.get("src") or "hook")[:40],
            str(item.get("sid") or "")[:128]))
    return added


def _ingest_whole(con: sqlite3.Connection, path: Path) -> int:
    """Ingest a set-aside inbox from the start, then delete it. Event ids make
    a repeat harmless, so a crash between the two steps loses nothing."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    with con:
        added = _ingest_lines(con, data)
    try:
        path.unlink()
    except OSError:
        pass
    return added


def _ingest_inbox_file(con: sqlite3.Connection, path: Path) -> int:
    added = 0
    for stale in sorted(path.parent.glob(path.name + ".ingest-*")):
        added += _ingest_whole(con, stale)     # left by an interrupted recycle
    try:
        size = path.stat().st_size
    except OSError:
        return added
    key = "inbox:" + str(path)
    row = con.execute("SELECT offset FROM file_state WHERE path=?",
                      (key,)).fetchone()
    start = int(row["offset"]) if row else 0
    if start > size:
        start = 0                               # a fresh file replaced it
    if start < size:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
        end = data.rfind(b"\n") + 1             # a half-written line waits
        if end:
            with con:           # the events and the offset past them, together
                added += _ingest_lines(con, data[:end])
                _save_offset(con, key, start + end)
            start += end
    if start >= size >= INBOX_ROTATE_BYTES:
        # Recycle by renaming first: a hook appending right now holds the
        # file open, the rename fails, and we try again on the next tick.
        moved = path.with_name(f"{path.name}.ingest-{time.time_ns()}")
        try:
            os.replace(path, moved)
        except OSError:
            return added
        with con:
            _save_offset(con, key, 0)
        added += _ingest_whole(con, moved)
    return added


def ingest_inbox() -> int:
    """Move hook events from the inbox files into the database (widget only)."""
    paths = [p for p in _inbox_paths()
             if p.exists() or any(p.parent.glob(p.name + ".ingest-*"))]
    if not paths:
        return 0
    added = 0
    try:
        with closing(_connect()) as con:
            for path in paths:
                try:
                    added += _ingest_inbox_file(con, path)
                except OSError:
                    continue
    except sqlite3.Error as e:
        _note_db_error(e)
    return added


# ---------------------------------------------------------------- service
class TrackerService:
    """Thread-safe cached facade used by the Tk UI."""

    REPAIR_RETRY_SEC = 600

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: tuple[str, ...] = ()
        self._summary: dict[str, dict] = {}
        self._rows: list[dict] = []
        self._inventory_at = 0.0
        self._scan_at = 0.0
        self._rows_at = 0.0
        self._checked = False
        self._repair_at = 0.0
        self._desc_cache: dict[tuple[str, str], str] = {}
        # Set after an automatic repair; the widget shows it once and clears it.
        self.repair_report: dict[str, int] | None = None

    def describe(self, client: str, name: str) -> str:
        key = (client, name)
        if key not in self._desc_cache:
            self._desc_cache[key] = skill_description(client, name)
        return self._desc_cache[key]

    def cached_ko(self, client: str, name: str, src: str) -> str:
        return cached_translation(client, name, desc_hash(src))

    def store_ko(self, client: str, name: str, src: str, ko: str) -> None:
        store_translation(client, name, desc_hash(src), ko)

    def paths(self, client: str, name: str) -> list[str]:
        return skill_paths(client, name)

    def _repair_if_needed(self, now: float) -> None:
        if _DB_FAULT is None or now - self._repair_at < self.REPAIR_RETRY_SEC:
            return
        self._repair_at = now
        log.warning("skill db fault detected: %s", _DB_FAULT)
        try:
            counts = recover_database()
        except Exception:
            log.exception("skill db repair failed - will retry")
            return
        if counts is not None:
            self.repair_report = counts

    def refresh(self, force=False):
        now = time.time()
        if force and not self._checked:
            self._checked = True
            check_database()
        self._repair_if_needed(now)
        changed = False
        if force or now - self._inventory_at >= 60:
            refresh_inventory()
            self._inventory_at = now
            changed = True
        if ingest_inbox():
            changed = True
        clients = running_clients()
        if "codex" in clients and (force or now - self._scan_at >= 3):
            if scan_codex_sessions():
                changed = True
            self._scan_at = now
        # The rollup only feeds the details window. It used to run -- together
        # with a per-client summary nobody read -- on every 2-second tick.
        rows = None
        if changed or now - self._rows_at >= 60:
            rows = usage_rows()
            self._rows_at = now
        with self._lock:
            self._clients = clients
            if rows is not None:
                self._rows = rows

    def snapshot(self) -> tuple[tuple[str, ...], dict[str, dict], list[dict]]:
        with self._lock:
            return self._clients, dict(self._summary), list(self._rows)


def hook_events(client: str, payload: dict) -> list[dict]:
    """Skill events in a Claude/Codex hook payload, reduced to what is kept.

    What makes an event unique (the tool-use id, the slash-command line) is
    hashed into ``id`` right here, so no prompt text is written anywhere.
    """
    event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
    session = str(payload.get("session_id") or payload.get("sessionId") or "")
    found: list[tuple[str, str, str, str, str]] = []
    if client == "claude" and event == "PreToolUse" \
            and payload.get("tool_name") == "Skill":
        tool_input = payload.get("tool_input") or {}
        name = tool_input.get("skill") or tool_input.get("name") \
            or tool_input.get("command")
        if isinstance(name, str):
            found.append(("claude", name, "auto", "hook-pre-tool",
                          str(payload.get("tool_use_id") or time.time_ns())))
    if client == "claude" and event == "UserPromptExpansion" \
            and payload.get("expansion_type") == "slash_command":
        name = payload.get("command_name")
        if isinstance(name, str):
            found.append((
                "claude", name, "manual", "hook-slash",
                f"{payload.get('prompt', '')}:{payload.get('command_args', '')}"))
    if client == "codex" and event == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        for name in _EXPLICIT_CODEX_RE.findall(prompt):
            found.append(("codex", name, "manual", "hook-explicit",
                          f"{payload.get('turn_id', '')}:{name}"))
    now = time.time()
    out = []
    for cl, name, mode, source, identity in found:
        norm = _normalize_event(cl, name, mode)
        if norm is None:
            continue
        cl, skill, mode = norm
        out.append({"id": _event_id(cl, skill, mode, source, session, identity),
                    "t": now, "c": cl, "s": skill, "m": mode, "src": source,
                    "sid": session})
    return out


def record_hook_payload(client: str, payload: dict) -> int:
    """Record a hook payload straight into the database (in-process callers)."""
    events = hook_events(client, payload)
    if not events:
        return 0
    try:
        with closing(_connect()) as con:
            with con:
                return sum(int(_insert_event(
                    con, e["id"], e["t"], e["c"], e["s"], e["m"], e["src"],
                    e["sid"])) for e in events)
    except sqlite3.Error as e:
        _note_db_error(e)
        return 0


def queue_hook_payload(client: str, payload: dict) -> int:
    """Hook entry point: append the payload's events to the inbox file.

    One O_APPEND write per call -- no database, no locks shared with the widget.
    Returns the number of events queued.
    """
    events = hook_events(client, payload)
    if not events:
        return 0
    blob = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":"))
                   + "\n" for e in events).encode("utf-8")
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(APPDATA_DIR / INBOX_NAME,
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND
                 | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    return len(events)
