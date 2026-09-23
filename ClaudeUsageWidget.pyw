# -*- coding: utf-8 -*-
"""
Claude 사용량 트레이 아이콘 v2 — 시계 옆에 사용률(%)을 항상 표시.

데이터 (우선순위):
 1. 사용량 API — 대화하지 않아도 60초마다 갱신 (계정 정책이 허용할 때만)
 2. ~/.claude/usage-widget.json — Stop 훅이 답변 직후 남기는 값 (API가 막혔을 때)

트레이 아이콘 = 가장 한도에 가까운 항목의 %.
아이콘 클릭 → 항목별 수치와 재설정까지 남은 시간이 메뉴에 표시된다.
"""
import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import re
import sys
import time
import socket
import threading
import queue
import logging
import datetime
import urllib.request
import urllib.error
import urllib.parse

from skill_tracker import TrackerService
from notifications import (NotificationService, LOG_PATH as NOTIFY_LOG,
                           ago as notify_ago, run_target as notify_run_target)

__version__ = "3.18.5"

APP_NAME = "ClaudeUsageWidget"
HOME = os.path.expanduser("~")
CRED_PATH = os.path.join(HOME, ".claude", ".credentials.json")
USAGE_FILE = os.path.join(HOME, ".claude", "usage-widget.json")
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# 2026-07 이전에는 console.anthropic.com이었다. 옮겨간 뒤로 옛 주소는 404
# not_found_error를 돌려줘 "토큰이 죽었다"처럼 보였다 — 실제로는 문 자체가 없었다.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLI_UA = "claude-cli/2.1.207 (external, cli)"
API_URL = "https://api.anthropic.com/api/oauth/usage"
API_HEADERS = {"User-Agent": CLI_UA, "anthropic-beta": "oauth-2025-04-20"}

POLL_SEC = 5
API_INTERVAL_ACTIVE = 60        # 대화 중일 때의 기본 조회 간격
API_INTERVAL_IDLE = 600         # 유휴일 때 — 서버 호출 한도를 아낀다
ACTIVE_WINDOW = 300             # 최근 5분 내 전사 갱신 = 대화 중
EVENT_MIN_GAP = 90              # 답변 직후 조회의 최소 간격
API_INTERVAL_DENIED = 30 * 60
BLINK_EVERY = 9                 # 초 — 이 간격으로 한 번씩 눈을 깜빡인다
BLINK_HOLD = 0.13               # 감고 있는 시간. 계속 움직이면 CPU를 먹는다
SINGLETON_PORT = 53917

REPO = "KimJinWooDa/ai-taskbar-widget"
CHANGELOG_URL = f"https://raw.githubusercontent.com/{REPO}/main/CHANGELOG.md"
CHANGELOG_PAGE = f"https://github.com/{REPO}/blob/main/CHANGELOG.md"
REPO_ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/main.zip"
REPO_ZIP_TOPDIR = "ai-taskbar-widget-main"
UPDATE_CHECK_SEC = 6 * 3600     # 하루 1번이면 새 버전 소식이 최대 하루 늦었다
# EXE 배포본의 자동 업데이트 — v* 태그를 푸시하면 GitHub Actions가 빌드해
# 릴리스에 EXE를 첨부하고(.github/workflows/release.yml), 위젯은 이 API로
# 최신 릴리스를 확인해 스스로 교체한다.
RELEASE_API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
# 여러 버전을 건너뛴 사용자에게 사이의 패치노트를 모두 보여줄 때만 쓴다
RELEASES_API_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
# 릴리스 자산은 이 주소로 시작해야만 받는다 (API 응답이 엉뚱한 곳을 가리키면 거부)
ASSET_URL_PREFIX = f"https://github.com/{REPO}/releases/download/"
WHATS_NEW_DAYS = 3              # 업데이트 패널을 안 눌러도 이만큼 지나면 바에서 내린다
# 실행 버전 기록(last_run_version)이 없던 마지막 버전 — 기록 없이 올라온
# 사용자는 적어도 이 버전 이후의 패치노트를 전부 본다
UNTRACKED_UNTIL = "3.17.2"
WIDGET_ASSET = "AI-Skill-Widget.exe"
HOOK_ASSET = "SkillEventHook.exe"
EXE_MIN_BYTES = 5_000_000       # 잘린 다운로드로 교체하는 사고 방지

APPDATA_DIR = os.path.join(os.environ.get("APPDATA", HOME), APP_NAME)
LOG_PATH = os.path.join(APPDATA_DIR, "widget.log")
CONFIG_PATH = os.path.join(APPDATA_DIR, "config.json")
CACHE_PATH = os.path.join(APPDATA_DIR, "last-usage.json")
CACHE_MAX_AGE = 24 * 3600
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_VBS = os.path.join(STARTUP_DIR, "ClaudeUsageWidget.vbs")
STARTUP_TASK = "AI Taskbar Widget"      # install.ps1이 등록하는 로그온 예약 작업

WINDOW_LABELS = [
    ("five_hour", "현재 세션"),
    ("seven_day", "주간 (모든 모델)"),
    ("seven_day_opus", "주간 Opus"),
    ("seven_day_sonnet", "주간 Sonnet"),
    ("seven_day_oauth_apps", "주간 앱"),
]


def severity_color(pct):
    if pct is None:
        return "#6e7681"
    if pct >= 90:
        return "#da3633"
    if pct >= 70:
        return "#bb8009"
    return "#2ea043"


os.makedirs(APPDATA_DIR, exist_ok=True)
try:
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
        os.remove(LOG_PATH)
except OSError:
    pass
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------- 시간 표기
def reset_phrase(val):
    """'4시간 46분 후' 처럼 남은 시간으로 표기."""
    if val in (None, ""):
        return ""
    try:
        if isinstance(val, (int, float)):
            dt = datetime.datetime.fromtimestamp(float(val)).astimezone()
        else:
            s = str(val)
            if s.replace(".", "", 1).isdigit():
                dt = datetime.datetime.fromtimestamp(float(s)).astimezone()
            else:
                dt = datetime.datetime.fromisoformat(
                    s.replace("Z", "+00:00")).astimezone()
    except (ValueError, OSError, OverflowError):
        return ""
    secs = (dt - datetime.datetime.now().astimezone()).total_seconds()
    if secs <= 0:
        return "곧 재설정"
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}일 {hours}시간 후 재설정"
    if hours:
        return f"{hours}시간 {mins}분 후 재설정"
    return f"{mins}분 후 재설정"


def short_reset(val):
    """플로팅 바용 짧은 표기: '3시간 59분 후' / '곧 리셋'."""
    return reset_phrase(val).replace(" 재설정", "").replace("곧", "곧 리셋")


# ---------------- Codex 사용량
#
# 1차 = 공식 조회(wham/usage, CodexBar와 같은 길): Codex 앱 로그인 토큰
# (auth.json)으로 내 계정 사용량만 GET 한다. Codex 앱의 "남은 사용량"과
# 같은 원천이라 항상 일치한다. 토큰 갱신은 Codex 몫 — 만료(401)·오프라인
# 이면 2차 = 세션 로그(rollout-*.jsonl)의 마지막 token_count 스냅샷으로
# 폴백한다. 폴백 값은 Codex를 실제로 쓸 때만 갱신되는 과거 기록이라,
# 주간 창이 그 사이 리셋됐으면 실제보다 높게 보일 수 있다(2026-08-13
# 실제로 그 어긋남 신고가 있었다 — 그래서 API가 1차다).
#
# 창 구성은 요금제(plan_type)마다 다르다 — 5시간+주간인 요금제가 있고,
# 주간 하나뿐인 요금제(prolite 등)도 있다. 그래서 모든 읽기에 요금제를
# 함께 실어 두고, 요금제가 다른 기록끼리는 절대 섞지 않는다.
CODEX_HOME_DIR = os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex"))
CODEX_AUTH_PATH = os.path.join(CODEX_HOME_DIR, "auth.json")
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_SESS_DIR = os.path.join(CODEX_HOME_DIR, "sessions")
CODEX_SNAP_MAX_AGE = 8 * 86400      # 주간 창이 한 바퀴 돈 스냅샷은 버린다
CODEX_TAIL_BYTES = 512 * 1024
CODEX_SCAN_FILES = 8
CODEX_TAIL_EVENTS = 40              # 창을 다 못 찾았을 때 거슬러 볼 이벤트 수
CODEX_WINDOW_KEEP_SEC = 900         # 이만큼 응답에서 빠진 창은 없어진 창으로 본다

def jsonl_files(root):
    """root 아래 모든 .jsonl의 (mtime, 경로). 못 읽는 폴더는 건너뛴다.

    os.scandir은 폴더를 나열할 때 Windows가 이미 준 크기·시각을 그대로
    돌려줘서 파일마다 stat을 따로 부르지 않는다 — 1,200~1,500개 기준
    os.walk + getmtime의 약 1/5 CPU다(2026-09-23 실측 37ms → 6ms).
    """
    out, stack = [], [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.name.endswith(".jsonl"):
                            out.append((e.stat().st_mtime, e.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


_codex_cache = (None, None)         # ((최신 파일, mtime), 스냅샷)
_codex_api_note = None              # 마지막 API 실패 사유 — 바뀔 때만 로그
_codex_last = None                  # 창 길이별로 살아 있는 마지막 값
_codex_plan = None                  # 마지막으로 확인된 요금제 (plan_type)
_codex_plan_ts = 0                  # 그 요금제를 확인한 읽기의 시각


def codex_usage_api():
    """공식 사용량 조회. 실패하면 None — 호출 쪽이 세션 로그로 폴백한다.

    응답의 used_percent는 "쓴 비율"이다(0 = 방금 리셋, 100 = 한도 도달).
    Codex 앱 사이드바는 같은 값을 "남은 %"로 뒤집어 보여준다.
    """
    global _codex_api_note
    try:
        with open(CODEX_AUTH_PATH, encoding="utf-8") as f:
            tokens = (json.load(f).get("tokens") or {})
        access = tokens.get("access_token")
        if not access:
            raise ValueError("auth.json에 access_token 없음")
        headers = {"Authorization": f"Bearer {access}",
                   "User-Agent": f"ai-taskbar-widget/{__version__}"}
        if tokens.get("account_id"):
            headers["chatgpt-account-id"] = tokens["account_id"]
        req = urllib.request.Request(CODEX_USAGE_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:                  # 조회 실패는 전부 폴백 사유
        note = f"{type(e).__name__}"
        if note != _codex_api_note:
            _codex_api_note = note
            log.info("codex usage api unavailable (%s) - falling back to "
                     "session logs", note)
        return None
    rl = data.get("rate_limit") or {}
    wins = []
    for w in (rl.get("primary_window"), rl.get("secondary_window")):
        if not w or w.get("used_percent") is None:
            continue
        secs = w.get("limit_window_seconds")
        wins.append({"pct": float(w["used_percent"]),
                     "resets_at": w.get("reset_at"),
                     "minutes": secs // 60 if secs else None})
    if not wins:
        return None
    wins.sort(key=lambda w: w["minutes"] or 0)
    plan = data.get("plan_type")
    if _codex_api_note != "ok":
        _codex_api_note = "ok"
        log.info("codex usage api ok (plan %s): %s", plan or "?",
                 [(w["minutes"], round(w["pct"])) for w in wins])
    return {"windows": wins, "ts": time.time(), "plan": plan}


def _codex_tail_snapshot(path):
    """파일 꼬리에서 창(used_percent)이 실린 마지막 스냅샷을 찾는다.

    한 이벤트에 5시간 창이 빠지고 주간 창만 실려 오는 경우가 있다
    (secondary=null, primary=주간). 그 이벤트 하나만 보면 바에서 5시간
    줄이 통째로 사라지므로, 창 길이별로 "가장 최근 값"을 모으며 조금 더
    거슬러 올라간다. 리셋 시각이 이미 지난 창은 지금 값이 아니라서 줍지
    않지만, 그렇게 해서 아무것도 안 남으면 예전처럼 마지막 이벤트를
    그대로 돌려준다(패널이 통째로 사라지지 않게).

    거슬러 올라가다 요금제(plan_type)가 달라지면 거기서 멈춘다 — 요금제를
    바꾸기 전 기록은 지금 없는 창을 되살릴 뿐이다.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - CODEX_TAIL_BYTES))
            data = f.read()
    except OSError:
        return None
    now = time.time()
    found, newest, ts, seen, plan = {}, None, None, 0, None
    for raw in reversed(data.splitlines()):
        if b'"rate_limits"' not in raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        rl = (obj.get("payload") or {}).get("rate_limits") or {}
        wins = [{"pct": float(w["used_percent"]),
                 "resets_at": w.get("resets_at"),
                 "minutes": w.get("window_minutes")}
                for w in (rl.get("primary"), rl.get("secondary"))
                if w and w.get("used_percent") is not None]
        if not wins:
            continue        # limit_id=premium 등 창이 비어 오는 이벤트도 있다
        if ts is None:
            newest = wins
            plan = rl.get("plan_type")
            try:
                ts = datetime.datetime.fromisoformat(
                    obj["timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                ts = os.path.getmtime(path)
        elif rl.get("plan_type") != plan:
            break           # 요금제를 바꾸기 전 기록 — 지금 창과 섞지 않는다
        for w in wins:
            resets = w["resets_at"]
            if w["minutes"] in found or (resets and now >= resets):
                continue    # 더 최근 값이 이미 있거나, 이미 리셋된 창이다
            found[w["minutes"]] = w
        seen += 1
        if len(found) >= 2 or seen >= CODEX_TAIL_EVENTS:
            break
    wins = list(found.values()) or newest
    if not wins:
        return None
    # 짧은 창(일간류)이 먼저 — 바에서 Claude처럼 첫 줄이 짧은 창이 된다
    return {"windows": sorted(wins, key=lambda w: w["minutes"] or 0),
            "ts": ts, "plan": plan}


def codex_rate_snapshot():
    """최근 Codex 세션의 마지막 한도 스냅샷 — 없으면 None.

    최신 파일부터 꼬리만 읽는다(활성 세션 파일은 90MB를 넘기도 한다).
    마지막 쓰기가 있었던 파일이 mtime 최신이 되므로, 최신 파일과 그
    mtime이 그대로면 지난 결과를 그대로 쓴다.
    """
    global _codex_cache
    now = time.time()
    found = sorted(jsonl_files(CODEX_SESS_DIR), reverse=True)[:CODEX_SCAN_FILES]
    paths = [p for m, p in found if now - m < CODEX_SNAP_MAX_AGE]
    if not paths:
        return None
    newest = (paths[0], found[0][0])
    if _codex_cache[0] == newest:
        return _codex_cache[1]
    prev = _codex_cache[1]
    snap = None
    for path in paths:
        snap = _codex_tail_snapshot(path)
        if snap:
            break
    _codex_cache = (newest, snap)
    if snap and (prev is None or prev["windows"] != snap["windows"]):
        log.info("codex usage snapshot: %s (ts %s)",
                 [(w["minutes"], round(w["pct"])) for w in snap["windows"]],
                 time.strftime("%m-%d %H:%M", time.localtime(snap["ts"])))
    return snap


def codex_merge(snap):
    """새로 읽은 값에서 빠진 창을 직전 값으로 메운다 — 없으면 None.

    Codex는 5시간 창이 통째로 빠지고 주간 창만 실려 오는 응답을 종종
    돌려준다(secondary=null). 그때마다 바에서 5시간 줄이 사라졌다가
    다음 조회에 되돌아와, 사용자 눈에는 "주간만 나온다"로 보였다
    (2026-08-26 신고). 창 길이를 키로 직전 값을 이어 붙이되 리셋 시각이
    지난 창은 버려, 이미 리셋된 옛 값을 되살리지는 않는다.

    다만 요금제를 바꾸면 창 구성 자체가 달라진다 — ChatGPT Pro(prolite)는
    5시간 창 없이 주간 하나뿐이라, "잠깐 빠진 창"으로 오해한 옛 5시간
    100%가 그 창의 리셋 시각이 올 때까지 바에 박제됐다(2026-08-31 신고).
    그래서 ①요금제가 바뀌면 직전 값을 통째로 버리고 ②바꾸기 전 기록으로
    만든 읽기는 무시하며 ③같은 요금제라도 CODEX_WINDOW_KEEP_SEC 넘게 안
    실려 온 창은 없어진 창으로 보고 지운다.
    """
    global _codex_last, _codex_plan, _codex_plan_ts
    now = time.time()
    plan = (snap or {}).get("plan")
    ts = (snap or {}).get("ts") or 0
    if snap and plan and _codex_plan and plan != _codex_plan:
        if ts < _codex_plan_ts:
            snap = None                 # 요금제를 바꾸기 전 기록이다
        else:
            log.info("codex plan changed: %s -> %s - dropping old windows",
                     _codex_plan, plan)
            _codex_last = None
    if snap and plan:
        _codex_plan, _codex_plan_ts = plan, max(ts, _codex_plan_ts)
    wins, seen = [], set()
    for w in (snap or {}).get("windows", []):
        wins.append(dict(w, seen=now))
        seen.add(w.get("minutes"))
    for w in (_codex_last or {}).get("windows", []):
        m, resets = w.get("minutes"), w.get("resets_at")
        if m is None or m in seen or not resets or now >= resets:
            continue
        if now - (w.get("seen") or 0) > CODEX_WINDOW_KEEP_SEC:
            continue    # 오래 안 실려 온 창 — 요금제에서 없어진 것으로 본다
        wins.append(w)
        seen.add(m)
    if not wins:
        _codex_last = None
        return None
    wins.sort(key=lambda w: w.get("minutes") or 0)
    _codex_last = {"windows": wins, "ts": (snap or _codex_last)["ts"],
                   "plan": _codex_plan}
    return _codex_last


# 로컬 SKILL.md가 없는 내장 스킬들의 기본 설명 (한국어로 미리 조사해 내장)
BUILTIN_DESCS = {
    "artifact-design": "Claude 내장 — 아티팩트(웹 페이지·문서·시각물)를 만들 때 "
                       "디자인 완성도 기준과 지침을 불러온다. 요청 성격에 맞춰 "
                       "디자인 투자 수준을 조정하는 역할.",
    "artifact-capabilities": "Claude 내장 — 아티팩트가 실행 중 쓸 수 있는 기능"
                             "(라이브 데이터 읽기, 공유 상태, 자가 갱신 등)의 "
                             "정의를 불러온다.",
    "dataviz": "Claude 내장 — 차트·그래프·대시보드를 만들 때 색·형태·접근성 "
               "규칙을 갖춘 디자인 시스템 지침을 불러온다.",
    "Presentations": "Codex 내장 — 프레젠테이션(슬라이드) 파일을 만들고 "
                     "편집하는 스킬.",
    "Spreadsheets": "Codex 내장 — 스프레드시트(엑셀류) 파일을 만들고 "
                    "편집하는 스킬.",
    "visualize": "Codex 내장 — 데이터나 구조를 차트·다이어그램 같은 시각 "
                 "자료로 그려 보여주는 스킬.",
    "control-in-app-browser": "Codex 내장 — 앱 안의 브라우저를 조작(페이지 "
                              "이동·클릭·입력)해 웹 작업을 대신하는 스킬.",
}


def send_to_recycle(path):
    """파일/폴더를 휴지통으로 — 완전 삭제가 아니라 복구 가능하게."""
    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p),
                    ("wFunc", ctypes.wintypes.UINT),
                    ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", ctypes.wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p)]
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3                                # FO_DELETE
    op.pFrom = path + "\x00"                    # 이중 널 종료 목록 형식
    op.fFlags = 0x40 | 0x10 | 0x04              # ALLOWUNDO|NOCONFIRM|SILENT
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


def builtin_desc(name):
    d = BUILTIN_DESCS.get(name)
    if d:
        return d
    if name.startswith("artifact-template-"):
        kind = name[len("artifact-template-"):].replace("-", " ")
        return (f"Codex 내장 템플릿 — {kind} 형태의 아티팩트(보고서·대시보드 "
                "페이지)를 빠르게 만드는 틀.")
    return ""


def _mostly_korean(text):
    """이미 한국어면 번역할 필요가 없다 — 앞부분 한글 비율로 판단."""
    head = text[:400]
    hangul = sum("가" <= c <= "힣" for c in head)
    return hangul >= max(len(head) // 10, 4)


def translate_ko(text):
    """스킬 설명을 한국어로 — 구글 번역 비공식 GET, 실패하면 ''.

    스킬 설명(공개 문서)만 보내며, 결과는 세션 동안 캐시된다.
    """
    q = urllib.parse.quote(text[:3000])
    url = ("https://translate.googleapis.com/translate_a/single"
           "?client=gtx&sl=auto&tl=ko&dt=t&q=" + q)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
    except Exception:
        log.info("translate failed", exc_info=True)
        return ""


# 장수 토큰은 config.json에 평문으로 두지 않는다 — Windows 계정에 묶인
# DPAPI로 잠가 저장한다. '로그 폴더 열기'로 연 폴더를 통째로 보내거나
# 동기화·백업 도구가 %APPDATA%를 옮겨도 다른 계정·PC에서는 풀리지 않는다.
TOKEN_ENC_KEY = "setup_token_dpapi"
_DPAPI_ENTROPY = b"ai-taskbar-widget/setup-token"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi(data, protect):
    """CryptProtectData / CryptUnprotectData (현재 사용자 범위). 실패는 OSError."""
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    src = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    salt = _DataBlob(len(_DPAPI_ENTROPY),
                     ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    out = _DataBlob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 1 = CRYPTPROTECT_UI_FORBIDDEN — 트레이 앱이 대화상자를 띄우면 안 된다
    if not fn(ctypes.byref(src), None, ctypes.byref(salt), None, None, 1,
              ctypes.byref(out)):
        raise OSError(ctypes.GetLastError(), "DPAPI 실패")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def load_config():
    # utf-8-sig: PowerShell류 외부 도구가 BOM을 붙여 저장하면 json.load가
    # 터져 설정 전체(위치·토글)가 기본값으로 날아간다 — 실측된 실패 경로.
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    enc = cfg.pop(TOKEN_ENC_KEY, None)
    if enc and not cfg.get("setup_token"):
        try:
            cfg["setup_token"] = dpapi(base64.b64decode(enc),
                                       False).decode("utf-8")
        except (OSError, ValueError):
            # 다른 계정·PC에서 옮겨 온 설정 — 이 계정으로는 못 푼다
            log.warning("stored setup token could not be decrypted - ignored")
    return cfg


def save_config(cfg):
    """메모리의 설정(평문 토큰 포함)을 디스크에 — 토큰만 DPAPI로 잠근다."""
    data = dict(cfg)
    tok = data.pop("setup_token", None)
    if tok:
        try:
            data[TOKEN_ENC_KEY] = base64.b64encode(
                dpapi(str(tok).encode("utf-8"), True)).decode("ascii")
        except OSError:
            data["setup_token"] = tok   # DPAPI를 못 쓰는 환경 — 예전 방식 유지
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


def rows_from_limits(limits):
    """API의 limits 배열 → [(라벨, %, 리셋원본)]. 모델 스코프(Fable 등) 포함."""
    rows = []
    for it in limits or []:
        if not isinstance(it, dict) or it.get("percent") is None:
            continue
        kind = it.get("kind")
        model = ((it.get("scope") or {}).get("model") or {}).get("display_name")
        if kind == "session":
            label = "현재 세션"
        elif kind == "weekly_all":
            label = "주간 (모든 모델)"
        elif model:
            label = f"주간 {model}"
        else:
            label = str(kind or "?")
        try:
            rows.append((label, float(it["percent"]), it.get("resets_at")))
        except (TypeError, ValueError):
            continue
    return rows


def rows_from_windows(d):
    """[(라벨, %, 리셋원본)] — used_percentage / utilization(0~1) 모두 지원."""
    rows, seen = [], set()

    def pct_of(v):
        p = v.get("used_percentage")
        if p is None and v.get("utilization") is not None:
            u = float(v["utilization"])
            p = u * 100 if u <= 1 else u
        return None if p is None else float(p)

    for key, label in WINDOW_LABELS:
        seen.add(key)
        v = d.get(key)
        if isinstance(v, dict):
            try:
                p = pct_of(v)
            except (TypeError, ValueError):
                continue
            if p is not None:
                rows.append((label, p, v.get("resets_at")))
    for key, v in d.items():
        if key in seen or not isinstance(v, dict):
            continue
        try:
            p = pct_of(v)
        except (TypeError, ValueError):
            continue
        if p is not None:
            rows.append((key, p, v.get("resets_at")))
    return rows


def clipboard_text():
    """클립보드의 유니코드 텍스트 — 장수 토큰 등록용."""
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(0):
        return ""
    try:
        h = u.GetClipboardData(13)      # CF_UNICODETEXT
        p = k.GlobalLock(h) if h else None
        try:
            return ctypes.wstring_at(p) if p else ""
        finally:
            if p:
                k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


# ---------------------------------------------------------------- API
class ApiDenied(Exception):
    pass


class ApiThrottled(Exception):
    """HTTP 429 — 사용량 엔드포인트의 자체 호출 제한에 걸림."""
    def __init__(self, retry_after=None):
        super().__init__("HTTP 429")
        self.retry_after = retry_after


def _retry_after(e):
    try:
        return float(e.headers.get("Retry-After")) if e.headers else None
    except (TypeError, ValueError):
        return None


_refresh_lock = threading.Lock()
_tok_sig = None
_mem_oauth = {}     # 마지막 갱신 결과 — 파일 쓰기가 실패해도 체인이 안 끊기게


_shape_logged = object()    # 마지막으로 기록한 리프레시 토큰 만료값


def _log_cred_shape(oauth):
    """자격증명의 필드 이름과 만료 시각만 1회 기록 — 리프레시 토큰 수명 진단용.

    토큰 값이나 계정 정보는 절대 남기지 않는다 (이름과 타임스탬프뿐).
    """
    global _shape_logged
    if _shape_logged == oauth.get("refreshTokenExpiresAt"):
        return          # 리프레시 창이 움직였을 때만 다시 기록
    _shape_logged = oauth.get("refreshTokenExpiresAt")
    exps = []
    for k, v in sorted(oauth.items()):
        if "xpires" in k.lower() and isinstance(v, (int, float)):
            try:
                exps.append(f"{k}=" + datetime.datetime.fromtimestamp(
                    v / 1000).isoformat(" ", "minutes"))
            except (ValueError, OSError, OverflowError):
                exps.append(f"{k}={v}")
    log.info("cred fields: [%s] %s", ",".join(sorted(oauth)), "; ".join(exps))


def get_access_token(force_refresh=False):
    global _mem_oauth
    with _refresh_lock:
        try:
            with open(CRED_PATH, encoding="utf-8") as f:
                creds = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise ApiDenied(f"인증 파일 없음: {e}")
        oauth = creds.get("claudeAiOauth") or {}
        if _mem_oauth.get("expiresAt", 0) > (oauth.get("expiresAt") or 0):
            oauth = {**oauth, **_mem_oauth}
        token = oauth.get("accessToken")
        global _tok_sig
        sig = ((token or "")[:11], oauth.get("expiresAt"))
        if sig != _tok_sig:        # 토큰 종류·만료 진단용 — 값 자체는 남기지 않는다
            _tok_sig = sig
            exp = oauth.get("expiresAt")
            try:
                when = datetime.datetime.fromtimestamp(
                    exp / 1000).isoformat(" ", "minutes") if exp else "?"
            except (TypeError, ValueError, OSError, OverflowError):
                when = str(exp)
            log.info("cred token %s... expires %s", (token or "")[:11], when)
            _log_cred_shape(oauth)
        if not force_refresh and token and \
                oauth.get("expiresAt", 0) > time.time() * 1000 + 120_000:
            return token
        rt = oauth.get("refreshToken")
        if not rt:
            if token and not force_refresh:
                return token    # 갱신은 못 해도 저장된 토큰이 살아있을 수 있다
            raise ApiDenied("리프레시 토큰 없음")
        req = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps({"grant_type": "refresh_token", "refresh_token": rt,
                             "client_id": CLIENT_ID}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": CLI_UA},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                t = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
            log.info("token refresh HTTP %d: %s", e.code, body)
            # 리프레시 토큰은 1회용 — 내 갱신이 실패했다면 CLI 등 다른
            # 클라이언트가 먼저 회전시켰을 수 있으니 파일을 다시 읽어본다.
            try:
                with open(CRED_PATH, encoding="utf-8") as f:
                    o2 = json.load(f).get("claudeAiOauth") or {}
                t2 = o2.get("accessToken")
                if t2 and t2 != token and \
                        (o2.get("expiresAt") or 0) > time.time() * 1000 + 60_000:
                    log.info("credentials rotated by another client - adopting")
                    return t2
            except (OSError, json.JSONDecodeError):
                pass
            if token and not force_refresh:
                log.info("refresh failed - trying stored token anyway")
                return token
            raise ApiDenied(f"토큰 갱신 실패 HTTP {e.code}")
        except OSError as e:
            raise RuntimeError(f"네트워크: {e}")
        _mem_oauth = {
            "accessToken": t["access_token"],
            "refreshToken": t.get("refresh_token") or rt,
            "expiresAt": int(time.time() * 1000)
                         + int(t.get("expires_in", 3600)) * 1000,
        }
        # 서버가 리프레시 토큰 수명도 알려주면 파일에 반영한다 — 갱신할 때마다
        # 이 창이 새로 열리는지가 "재로그인이 정말 끝났는가"를 가른다.
        for key in ("refresh_token_expires_in", "refresh_expires_in"):
            if isinstance(t.get(key), (int, float)):
                when = int(time.time() * 1000) + int(t[key]) * 1000
                # 이 파일은 CLI도 읽는다 — 초/밀리초를 잘못 해석한 값을 쓰면
                # CLI가 멀쩡한 토큰을 만료로 볼 수 있으니 상식 범위만 기록
                if time.time() * 1000 < when < (time.time() + 400 * 86400) * 1000:
                    _mem_oauth["refreshTokenExpiresAt"] = when
                break
        log.info("token response: [%s]%s", ",".join(sorted(t)),
                 "".join(f" {k}={v}" for k, v in sorted(t.items())
                         if isinstance(v, (int, float))))
        creds = {}
        try:
            with open(CRED_PATH, encoding="utf-8") as f:
                creds = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        creds.setdefault("claudeAiOauth", {}).update(_mem_oauth)
        try:
            tmp = CRED_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(creds, f)
            os.replace(tmp, CRED_PATH)
        except OSError as e:
            # 디스크 반영은 실패해도 _mem_oauth가 새 체인을 들고 있다
            log.warning("credential write failed (%s) - token kept in memory", e)
        log.info("token refreshed")
        return t["access_token"]


def fetch_usage_api(cfg=None):
    # 장수 토큰(claude setup-token)이 등록돼 있으면 우선 쓰되, 401로 죽은
    # 토큰이면 그 자리에서 폐기하고 아래 리프레시 경로로 넘어간다 —
    # 매 폴마다 죽은 토큰을 두드리면 헛 호출로 429만 부른다.
    if cfg is None:
        cfg = load_config()
    setup_tok = cfg.get("setup_token")
    if setup_tok:
        req = urllib.request.Request(
            API_URL, headers={"Authorization": f"Bearer {setup_tok}", **API_HEADERS})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                cfg.pop("setup_token", None)
                save_config(cfg)
                log.info("setup token dead (HTTP 401) - dropped, "
                         "falling back to refresh path")
            elif e.code == 403:
                raise ApiDenied("설정 토큰 거부 HTTP 403")
            elif e.code == 429:
                raise ApiThrottled(_retry_after(e))
            else:
                raise RuntimeError(f"HTTP {e.code}")
        except OSError as e:
            raise RuntimeError(f"네트워크: {e}")
    for attempt in (0, 1):
        token = get_access_token(force_refresh=(attempt == 1))
        req = urllib.request.Request(
            API_URL, headers={"Authorization": f"Bearer {token}", **API_HEADERS})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            if e.code in (401, 403):
                raise ApiDenied(f"HTTP {e.code} {body[:120]}")
            if e.code == 429:
                raise ApiThrottled(_retry_after(e))
            raise RuntimeError(f"HTTP {e.code}")
        except OSError as e:
            raise RuntimeError(f"네트워크: {e}")
    raise ApiDenied("인증 실패")


# ---------------------------------------------------------------- 업데이트
def _ver_tuple(s):
    """'2.7.0' → (2, 7, 0). 자릿수가 달라도 비교되게 3자리로 맞춘다."""
    try:
        nums = [int(x) for x in str(s).split(".")]
    except ValueError:
        return (0, 0, 0)
    return tuple((nums + [0, 0, 0])[:3])


def fetch_changelog():
    req = urllib.request.Request(CHANGELOG_URL, headers={"User-Agent": CLI_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def parse_release(text):
    """releases/latest 응답 →
    (버전튜플, '3.16.0', 패치노트, {파일명: url}, {파일명: sha256 hex}).

    태그가 v3.16.0 꼴이 아니면(프리릴리스 실험 태그 등) None — 엉뚱한
    태그로 자동 교체가 돌면 안 된다. sha256은 GitHub가 자산마다 계산해
    주는 digest("sha256:…")이고, 없는 자산은 사전에서 빠진다.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").lstrip("vV").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)*", tag):
        return None
    assets, digests = {}, {}
    for a in data.get("assets") or []:
        name, url = a.get("name"), a.get("browser_download_url")
        if not name or not url:
            continue
        assets[name] = url
        digest = str(a.get("digest") or "")
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            digests[name] = digest[7:].lower()
    return _ver_tuple(tag), tag, str(data.get("body") or ""), assets, digests


def fetch_latest_release():
    # 오버라이드는 배포 전 검증용 — 가짜 릴리스 JSON을 물릴 수 있다
    url = os.environ.get("CLAUDE_WIDGET_RELEASE_API") or RELEASE_API_URL
    req = urllib.request.Request(url, headers={"User-Agent": CLI_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return parse_release(r.read().decode("utf-8", "replace"))


def release_notes_between(text, cur_t, latest_t):
    """releases 목록 응답에서 (지금, 최신] 사이 버전들의 패치노트 — 최신이 먼저.

    본문은 릴리스 워크플로가 CHANGELOG에서 잘라 넣은 `## v… — 날짜` 절이라
    이어 붙이면 그대로 CHANGELOG 형식이 된다.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    parts = []
    for rel in data if isinstance(data, list) else []:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "").lstrip("vV").strip()
        if not re.fullmatch(r"\d+(?:\.\d+)*", tag):
            continue
        ver_t = _ver_tuple(tag)
        if cur_t < ver_t <= latest_t:
            parts.append((ver_t, str(rel.get("body") or "").strip()))
    parts.sort(reverse=True)
    return "\n\n".join(body for _, body in parts if body)


def fetch_release_notes(cur_t, latest_t):
    req = urllib.request.Request(RELEASES_API_URL, headers={"User-Agent": CLI_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return release_notes_between(r.read().decode("utf-8", "replace"),
                                     cur_t, latest_t)


def trusted_asset_url(url):
    """이 저장소 릴리스의 다운로드 주소인가 — 아니면 받지 않는다.

    배포 전 검증용 오버라이드(CLAUDE_WIDGET_RELEASE_API)를 켠 동안만
    가짜 릴리스가 가리키는 임의 주소를 허용한다.
    """
    if os.environ.get("CLAUDE_WIDGET_RELEASE_API"):
        return str(url).startswith(("https://", "http://127.0.0.1"))
    return str(url).startswith(ASSET_URL_PREFIX)


def download_file(url, dst, sha256=None):
    """url → dst. sha256을 주면 받은 내용이 그 값과 같을 때만 남긴다.

    크기·MZ 검사(check_exe)는 잘린 다운로드만 잡는다. 중간에 바뀐 파일은
    GitHub가 릴리스 자산마다 주는 SHA-256과 맞춰 봐야 걸러진다.
    실패하면 반쯤 받은 파일을 지운다 — 다음 교체 단계로 넘어가지 않게.
    """
    if not trusted_asset_url(url):
        raise RuntimeError(f"신뢰할 수 없는 다운로드 주소: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": CLI_UA})
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=120) as r, \
                open(dst, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
        if sha256 and h.hexdigest() != sha256.lower():
            raise RuntimeError("SHA-256 불일치 — 손상됐거나 바뀐 파일이라 "
                               "설치하지 않았습니다")
    except BaseException:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise


def local_changelog():
    """이 실행본에 딸린 CHANGELOG.md — EXE는 빌드 때 함께 묶는다(build.ps1).

    업데이트 직후 "무엇이 바뀌었나"를 네트워크 없이 정확히 이 버전 기준으로
    보여주기 위해서다. 못 읽으면 ''.
    """
    base = getattr(sys, "_MEIPASS", None) or \
        os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base, "CHANGELOG.md"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


_CHANGELOG_FULLHEAD = re.compile(
    r"^##\s+v?(\d+(?:\.\d+)*)\b\s*(?:[—–-]\s*(\S+))?")


def changelog_entries(text):
    """CHANGELOG 형식 → [{'t','v','date','items'}] 파일 순서(최신이 먼저).

    items는 불릿 하나가 문자열 하나 — 들여쓴 이어지는 줄은 그 불릿에 붙인다.
    """
    entries = []
    for line in text.splitlines():
        s = line.strip()
        m = _CHANGELOG_FULLHEAD.match(s)
        if m:
            entries.append({"t": _ver_tuple(m.group(1)), "v": m.group(1),
                            "date": m.group(2) or "", "items": []})
        elif entries and s and not s.startswith("#"):
            items = entries[-1]["items"]
            if s.startswith(("- ", "* ")):
                items.append(s[2:].strip())
            elif items:
                items[-1] += " " + s
            else:
                items.append(s)
    return entries


def headline(items):
    """패치노트의 한 줄 요약 — 첫 불릿의 굵은 글씨, 없으면 첫 불릿 앞부분."""
    if not items:
        return ""
    m = re.search(r"\*\*(.+?)\*\*", items[0])
    text = m.group(1) if m else re.sub(r"[*`]", "", items[0])
    text = text.strip().rstrip(".")
    return text if len(text) <= 60 else text[:58] + "…"


def rename_retry(src, dst, tries=20, wait=0.25):
    """os.rename — 잠깐의 공유 위반(WinError 32)은 몇 초까지 다시 해 본다.

    PyInstaller EXE는 모듈을 지연 import할 때마다 자기 EXE를 잠깐 연다
    (삭제 공유 없이). 그 몇 ms에 교체용 이름 바꾸기가 겹치면 실패한다 —
    2026-09-23 v3.17.2 → v3.18.0 자동 업데이트가 다른 스레드가 429 응답을
    처리하던 순간 이렇게 실패했다. 잠금은 곧 풀리므로 기다렸다 다시 한다.
    """
    for attempt in range(tries):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(wait)


def check_exe(path):
    """받은 파일이 온전한 EXE인지 — 크기와 PE 머리표(MZ)만 본다."""
    if os.path.getsize(path) < EXE_MIN_BYTES:
        raise RuntimeError("내려받은 EXE가 너무 작음 (잘린 다운로드)")
    with open(path, "rb") as f:
        if f.read(2) != b"MZ":
            raise RuntimeError("내려받은 파일이 EXE가 아님")


def finish_exe_update():
    """직전 자동 업데이트의 뒷정리 — 옛 EXE 삭제, 미뤄 둔 훅 교체.

    .old는 새 버전이 정상 기동했다는 뜻이므로 지운다(못 떴으면 남아 있어
    수동 복구 단서가 된다). 구 프로세스의 파일 핸들이 늦게 놓일 수 있어
    짧게 재시도한다.
    """
    if not getattr(sys, "frozen", False):
        return
    old = sys.executable + ".old"
    for _ in range(6):
        try:
            if os.path.exists(old):
                os.remove(old)
                log.info("previous exe cleaned up")
            break
        except OSError:
            time.sleep(0.5)
    hook = os.path.join(os.path.dirname(sys.executable), HOOK_ASSET)
    try:
        if os.path.exists(hook + ".new"):
            os.replace(hook + ".new", hook)
            log.info("pending hook exe swap finished")
    except OSError:
        pass                    # 훅이 하필 도는 중 — 다음 시작 때 다시


def download_repo(dst):
    """저장소 zip을 받아 풀고 위젯 파일이 든 폴더 경로를 돌려준다."""
    import io
    import zipfile
    req = urllib.request.Request(REPO_ZIP_URL, headers={"User-Agent": CLI_UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dst)
    src = os.path.join(dst, REPO_ZIP_TOPDIR)
    if not os.path.exists(os.path.join(src, "ClaudeUsageWidget.pyw")):
        raise RuntimeError("내려받은 압축에 위젯 파일이 없음")
    return src


# ---------------------------------------------------------------- 자동 실행
def pythonw_exe():
    exe = sys.executable
    if os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def startup_installed():
    return os.path.exists(STARTUP_VBS)


# 설치본의 자동 실행은 install.ps1이 만든 로그온 예약 작업(STARTUP_TASK)이
# 맡는다. 메뉴 체크를 예전 방식의 시작프로그램 vbs로만 판정하던 탓에 켜져
# 있는데도 꺼진 것처럼 보였고, 누르면 vbs가 하나 더 생겨 로그온마다 두 번
# 떴다(2026-09-23 발견). 예약 작업이 있으면 그 로그온 트리거가 곧 스위치다 —
# 작업 자체는 남겨 두므로 Claude 세션 시작 훅(schtasks /run)은 계속 된다.
NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW — 콘솔 창을 띄우지 않는다


def logon_trigger_state(xml):
    """schtasks /query /xml 결과 → 로그온 트리거가 켜져 있나. 트리거가 없으면 None."""
    m = re.search(r"<LogonTrigger>(.*?)</LogonTrigger>", xml, re.S)
    if not m:
        return None
    return not re.search(r"<Enabled>\s*false\s*</Enabled>", m.group(1))


def task_autostart():
    """예약 작업 기준 자동 실행 상태 — (작업이 있나, 로그온 시 켜지나)."""
    import subprocess
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", STARTUP_TASK, "/xml"],
                           capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False, False
    if r.returncode != 0:
        return False, False             # 작업 없음 — 소스 실행·옛 설치
    return True, bool(logon_trigger_state(r.stdout.decode("utf-8", "replace")))


def set_task_autostart(on):
    """예약 작업의 로그온 트리거를 켜거나 끈다. 트리거가 없으면 켤 때 만든다."""
    import subprocess
    flag = "$true" if on else "$false"
    ps = ("$ErrorActionPreference = 'Stop'; "
          f"$t = Get-ScheduledTask -TaskName '{STARTUP_TASK}'; "
          "if ($t.Triggers.Count -eq 0) { "
          f"if ({flag}) {{ Set-ScheduledTask -TaskName '{STARTUP_TASK}' "
          "-Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) "
          "| Out-Null } } else { "
          f"foreach ($tr in $t.Triggers) {{ $tr.Enabled = {flag} }}; "
          "Set-ScheduledTask -InputObject $t | Out-Null }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", ps], capture_output=True, timeout=60,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise OSError(r.stderr.decode("cp949", "replace").strip()[:200]
                      or f"exit {r.returncode}")


def install_startup():
    if getattr(sys, "frozen", False):
        content = ('CreateObject("Wscript.Shell").Run '
                   f'"""{sys.executable}""", 0, False')
    else:
        content = ('CreateObject("Wscript.Shell").Run '
                   f'"""{pythonw_exe()}"" ""{os.path.abspath(__file__)}""", '
                   '0, False')
    with open(STARTUP_VBS, "w", encoding="utf-16") as f:
        f.write(content)
    log.info("startup registered")


def uninstall_startup():
    try:
        os.remove(STARTUP_VBS)
    except OSError:
        pass


def demote_tray_icon():
    """아이콘을 시계 옆이 아니라 오버플로(^) 패널 안에 두기 — 바가 숫자를 대신 보여준다."""
    import winreg
    exe = pythonw_exe().lower()
    changed = 0
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r"Control Panel\NotifyIconSettings")
    except OSError:
        return 0
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub, 0,
                                    winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                    if str(winreg.QueryValueEx(k, "ExecutablePath")[0]).lower() == exe:
                        if winreg.QueryValueEx(k, "IsPromoted")[0] != 0:
                            winreg.SetValueEx(k, "IsPromoted", 0,
                                              winreg.REG_DWORD, 0)
                            changed += 1
            except OSError:
                continue
    if changed:
        log.info("tray demoted: %d", changed)
    return changed


# ---------------------------------------------------------------- 아이콘
# Claude 마스코트 "Clawd" — 12x8 픽셀 스프라이트 (o=몸통, x=눈, .=투명).
# 트레이는 16px까지 줄어들어 로고의 가는 살은 뭉개지지만, 이 격자는 살아남는다.
CLAWD = (
    "..oooooooo..",
    "..oxooooxo..",
    "oooooooooooo",
    "oooooooooooo",
    "..oooooooo..",
    "..oooooooo..",
    "..o.o..o.o..",
    "..o.o..o.o..",
)
CLAWD_EYE = "#1c1917"


_icon_cache = {}


def make_icon_image(pct, blink=False):
    """Clawd. 몸 색이 곧 사용량 — 초록(여유)·주황(70%↑)·빨강(90%↑).

    같은 그림을 매번 다시 그리지 않게 (색, 눈 상태)로 캐시한다 —
    깜빡임이 트레이 갱신 비용을 늘리지 않도록.
    """
    key = (severity_color(pct), blink)
    img = _icon_cache.get(key)
    if img is not None:
        return img
    from PIL import Image, ImageDraw
    # 96은 16·24·32px의 정수배 — 트레이가 줄여도 픽셀이 덜 뭉개진다.
    # s=10이면 스프라이트가 120x80이라 좌우 팔이 살짝 잘려 나가는 대신
    # 세로를 83%까지 채운다(딱 맞추는 s=8은 67%라 아이콘이 작아 보였다).
    n, s = 96, 10
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, y0 = (n - len(CLAWD[0]) * s) // 2, (n - len(CLAWD) * s) // 2
    for y, row in enumerate(CLAWD):
        for x, ch in enumerate(row):
            if ch == ".":
                continue
            eye = ch == "x" and not blink        # 깜빡일 땐 눈만 몸통색으로
            d.rectangle([x0 + x * s, y0 + y * s,
                         x0 + (x + 1) * s - 1, y0 + (y + 1) * s - 1],
                        fill=CLAWD_EYE if eye else key[0])
    _icon_cache[key] = img
    return img


# ---------------------------------------------------------------- Claude 감시
class _PE32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260)]


TRANSCRIPT_DIR = os.path.join(HOME, ".claude", "projects")
_ts_cache = {"path": None, "scan_at": 0.0}


def latest_transcript_mtime():
    """가장 최근 대화 전사(.jsonl)의 mtime — Claude가 방금 답했는지의 신호.

    Stop 훅은 환경에 따라 안 불리기도 하지만, 전사 파일은 데스크톱·터미널
    어디서든 답변마다 갱신되므로 더 믿을 만한 활동 감지 수단이다.
    전체 스캔은 수천 파일에 80ms쯤 걸리므로 60초에 1번만 하고,
    평소에는 마지막으로 찾아둔 파일 하나만 stat 한다 (진행 중 대화의
    이어쓰기는 그 파일에서 바로 잡힌다).
    """
    now = time.time()
    best_path, best_m = _ts_cache["path"], 0.0
    if best_path:
        try:
            best_m = os.path.getmtime(best_path)
        except OSError:
            best_path = None
    if best_path is None or now - _ts_cache["scan_at"] >= 60:
        _ts_cache["scan_at"] = now
        for m, p in jsonl_files(TRANSCRIPT_DIR):
            if m > best_m:
                best_m, best_path = m, p
        _ts_cache["path"] = best_path
    return best_m


def claude_running():
    """claude.exe(데스크톱 앱 또는 CLI)가 하나라도 실행 중인가."""
    k = ctypes.windll.kernel32
    snap = k.CreateToolhelp32Snapshot(2, 0)
    if snap in (0, -1):
        return True     # 조회 실패 시엔 종료하지 않는 쪽으로
    try:
        e = _PE32W()
        e.dwSize = ctypes.sizeof(_PE32W)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == "claude.exe":
                return True
            ok = k.Process32NextW(snap, ctypes.byref(e))
        return False
    finally:
        k.CloseHandle(snap)


# ---------------------------------------------------------------- 플로팅 바
SNIP_EXES = {
    # 화면 캡처 도구의 전체화면 오버레이 — 가림으로 치지 않는다 (바가 안 숨음)
    "screenclippinghost.exe", "snippingtool.exe", "screensketch.exe",
    "sharex.exe", "picpick.exe", "snagit32.exe", "snagitcapture.exe",
    "flameshot.exe", "greenshot.exe", "lightshot.exe",
    "kakaotalk.exe", "alcapture.exe", "alsee.exe", "bandicam.exe",
    "snipaste.exe", "pixpin.exe",
}
_last_cover_exe = None
_last_snip_at = 0.0     # 캡처 오버레이를 마지막으로 본 시각 — 종료 직후 오탐 방지
# PID → 실행파일 이름. 전체화면 게임 중에는 복귀 감시(_watch_restore)가 30ms
# 마다 캡처 오버레이인지 묻느라 이 조회를 네 번씩 했고, 매번 OpenProcess로
# 이름을 읽던 것이 숨은 위젯 CPU(코어의 약 5%)의 대부분이었다(2026-09-23
# py-spy 실측). 판단은 그대로 두고 이름만 기억한다. PID는 재사용될 수 있어
# 1분마다 비운다.
_exe_cache = {}
_exe_cache_at = 0.0


def _window_exe(hwnd):
    """창을 소유한 프로세스의 실행파일 이름(소문자) — 실패 시 ''."""
    global _exe_cache_at
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        pid = ctypes.wintypes.DWORD()
        u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        now = time.time()
        if now - _exe_cache_at > 60:
            _exe_cache.clear()
            _exe_cache_at = now
        name = _exe_cache.get(pid.value)
        if name is not None:
            return name
        name = ""
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
        if h:
            try:
                buf = ctypes.create_unicode_buffer(512)
                n = ctypes.wintypes.DWORD(512)
                if k.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf,
                                                ctypes.byref(n)):
                    name = os.path.basename(buf.value).lower()
            finally:
                k.CloseHandle(ctypes.c_void_p(h))
        # 못 읽은 것('')도 기억한다 — 보호된 프로세스를 30ms마다 다시 두드리지 않게
        _exe_cache[pid.value] = name
        return name
    except Exception:
        pass
    return ""


def _taskbar_covered():
    """(가려짐, 캡처오버레이) — 작업표시줄이 전체화면 앱에 덮였는지 판정.

    전면 창 좌표 비교는 테두리 없는 최대화 창(Electron 앱 등)을 오탐하므로,
    작업표시줄 중앙 픽셀을 실제로 차지한 창이 무엇인지로 판정한다.
    캡처 도구의 오버레이(Win+Shift+S 등)는 가림으로 치지 않되, 화면이
    어두워진 동안 배경을 잘못 찍지 않게 둘째 값으로 알려준다.
    """
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
        u.GetAncestor.restype = ctypes.c_void_p
        tray = u.FindWindowW("Shell_TrayWnd", None)
        if not tray or not u.IsWindowVisible(ctypes.c_void_p(tray)):
            return True, False
        r = ctypes.wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(tray), ctypes.byref(r))
        pt = ctypes.wintypes.POINT((r.left + r.right) // 2,
                                   (r.top + r.bottom) // 2)
        h0 = u.WindowFromPoint(pt)
        h = u.GetAncestor(ctypes.c_void_p(h0), 2) if h0 else None
        if not h or h == tray:
            return False, False
        hr = ctypes.wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(hr))
        if (hr.right - hr.left) < u.GetSystemMetrics(0) * 3 // 5:
            return False, False     # 툴팁·플라이아웃 같은 작은 창
        # UWP 캡처 오버레이는 최상위가 ApplicationFrameHost일 수 있어
        # 직계 창의 프로세스도 함께 본다
        exes = {_window_exe(h), _window_exe(h0)} - {""}
        if exes & SNIP_EXES:
            global _last_snip_at
            _last_snip_at = time.time()
            return False, True
        covered = hr.top < r.top - 4
        global _last_cover_exe
        if covered and exes != _last_cover_exe:
            _last_cover_exe = exes
            log.info("taskbar covered by %s", "/".join(sorted(exes)) or "?")
        elif not covered:
            _last_cover_exe = None
        return covered, False
    except Exception:
        return False, False


def _tray_topmost():
    """작업표시줄이 topmost인가 — 아니면 전체화면 앱이 떠 있다는 뜻.

    Windows가 직접 내리는 비트라 가장 정확하다. 다만 전체화면이 된 뒤 몇 초
    늦게 내려갈 때가 있어 `_fullscreen_now()`에서 다른 신호와 함께 쓴다.
    못 읽으면 평상시로 본다.
    """
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        tray = u.FindWindowW("Shell_TrayWnd", None)
        return not tray or bool(
            u.GetWindowLongW(ctypes.c_void_p(tray), -20) & 0x8)
    except Exception:
        return True


def _foreground_pair():
    """전면 창과 그 최상위 조상 — UWP는 최상위가 ApplicationFrameHost다."""
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetAncestor.restype = ctypes.c_void_p
    fg = u.GetForegroundWindow()
    if not fg:
        return None, None
    return fg, u.GetAncestor(ctypes.c_void_p(fg), 2)


def _covers_screen(hwnd):
    """그 창이 화면 전체를 덮는가."""
    try:
        u = ctypes.windll.user32
        r = ctypes.wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
        return (r.right - r.left >= u.GetSystemMetrics(0)
                and r.bottom - r.top >= u.GetSystemMetrics(1))
    except Exception:
        return False


def _snip_overlay():
    """화면 캡처 도구의 오버레이가 떠 있는가 — 그동안은 바를 숨기면 안 된다.

    전면 창만 보면 놓친다. 오버레이는 **화면을 덮은 다음에 포그라운드가 되고**,
    그 사이 100ms 남짓 동안 전면은 아직 이전 앱이라 '전체화면 앱'으로 오해한다.
    하필 그때 캡처가 찍히면 사진에서 바만 빠진다(사용자 신고, 2026-07-28).
    그래서 화면 한가운데를 실제로 차지한 창까지 함께 본다.
    """
    exes = set()
    try:
        u = ctypes.windll.user32
        fg, top = _foreground_pair()
        exes |= {_window_exe(fg), _window_exe(top)}
        u.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.GetAncestor.restype = ctypes.c_void_p
        pt = ctypes.wintypes.POINT(u.GetSystemMetrics(0) // 2,
                                   u.GetSystemMetrics(1) // 2)
        h = u.WindowFromPoint(pt)
        # 화면을 통째로 덮은 창일 때만 오버레이로 친다 — 핀으로 띄워 둔 캡처
        # 이미지 창까지 여기 걸리면 영상 전체화면에서 바가 안 숨는다
        if h and _covers_screen(u.GetAncestor(ctypes.c_void_p(h), 2) or h):
            exes |= {_window_exe(h),
                     _window_exe(u.GetAncestor(ctypes.c_void_p(h), 2))}
    except Exception:
        return False
    hit = bool((exes - {""}) & SNIP_EXES)
    if hit:
        global _last_snip_at
        _last_snip_at = time.time()
    return hit


def _fullscreen_foreground():
    """전면 창이 화면을 통째로 덮는가 — 바탕화면 같은 셸 창은 제외.

    테두리 없는 최대화 창은 작업표시줄 높이만큼 모자라 여기 안 걸린다.
    """
    try:
        u = ctypes.windll.user32
        fg, _ = _foreground_pair()
        if not fg:
            return False
        cn = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(ctypes.c_void_p(fg), cn, 64)
        # 뒤 네 개는 explorer의 일시 오버레이(Alt-Tab·작업 보기 등) — 화면을
        # 통째로 덮은 채 잠깐 전면이 되어 전체화면 앱으로 오탐됐다(2026-08-27
        # 로그: fs_fg=True fg=explorer.exe가 반복되며 바가 수시로 숨었다).
        if cn.value in ("Progman", "WorkerW", "Shell_TrayWnd",
                        "Shell_SecondaryTrayWnd",
                        "XamlExplorerHostIslandWindow", "MultitaskingViewFrame",
                        "ForegroundStaging", "TaskListThumbnailWnd"):
            return False
        r = ctypes.wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(fg), ctypes.byref(r))
        return (r.right - r.left >= u.GetSystemMetrics(0)
                and r.bottom - r.top >= u.GetSystemMetrics(1))
    except Exception:
        return False


def _fullscreen_now():
    """지금 전체화면 앱이 떠 있는가 (캡처 오버레이는 아니다).

    두 신호를 같이 본다 — ①Windows가 작업표시줄 topmost를 뗐다(정확하지만 몇 초
    늦기도 한다) ②전면 창이 화면을 다 덮는다(즉시 알 수 있다). 둘 중 하나면
    전체화면으로 보고 바를 숨긴다. 창 이벤트마다 불리므로 값싼 검사를 먼저 하고,
    프로세스 이름을 읽는 캡처 검사는 정말 숨기기 직전에만 한다.
    """
    if _tray_topmost() and not _fullscreen_foreground():
        return False
    if _snip_overlay():
        return False
    # 캡처 오버레이가 방금 닫힌 참이면 작업표시줄 topmost 복구가 몇 초 늦는다 —
    # 오버레이 자체가 전체화면·포그라운드라 Windows가 비트를 떼기 때문. 그 잔상
    # (비트 내려감)만으로는 숨기지 않고, 화면을 실제로 덮은 전면 창이 보일 때만
    # 숨긴다. (캡처를 끝낼 때마다 바가 몇 초 사라지던 원인)
    if time.time() - _last_snip_at < 8 and not _fullscreen_foreground():
        return False
    return True


# 전체화면 창이 뜨는 '그 순간'을 받기 위한 훅 —
# 폴링(0.1초)으로는 한두 프레임이 비친다. 전면 전환과 창 크기변경 둘 다 본다
# (크롬이 전체화면이 될 땐 전면은 그대로고 창 크기만 바뀐다).
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.c_void_p,
    ctypes.wintypes.LONG, ctypes.wintypes.LONG,
    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD)

SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.wintypes.DWORD),
                ("biWidth", ctypes.wintypes.LONG),
                ("biHeight", ctypes.wintypes.LONG),
                ("biPlanes", ctypes.wintypes.WORD),
                ("biBitCount", ctypes.wintypes.WORD),
                ("biCompression", ctypes.wintypes.DWORD),
                ("biSizeImage", ctypes.wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.wintypes.LONG),
                ("biYPelsPerMeter", ctypes.wintypes.LONG),
                ("biClrUsed", ctypes.wintypes.DWORD),
                ("biClrImportant", ctypes.wintypes.DWORD)]


def blit_bgra(x, y, w, h):
    """화면의 (x, y, w, h)만 GDI BitBlt로 떠서 BGRA 바이트로 — 실패하면 None.

    PIL ImageGrab은 bbox를 줘도 화면 전체를 뜬 뒤 잘라낸다(2560×1440 실측
    CPU 20ms). 이 경로는 그 영역만 옮겨 CPU 1ms 안팎이다. 다만 벽시계로는
    DWM 합성 한 프레임(약 13ms)을 기다리므로, 자주 부를 곳은 Tk 스레드
    밖에서 부른다. GDI만 쓰므로 어느 스레드에서 불러도 된다.
    """
    if w <= 0 or h <= 0:
        return None
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    sdc = mdc = bmp = None
    try:
        sdc = u.GetDC(0)
        mdc = g.CreateCompatibleDC(sdc)
        bmp = g.CreateCompatibleBitmap(sdc, w, h)
        g.SelectObject(mdc, bmp)
        g.BitBlt(mdc, 0, 0, w, h, sdc, x, y, SRCCOPY)
        hdr = BITMAPINFOHEADER()
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight = w, -h           # 음수 = 위에서 아래로
        hdr.biPlanes, hdr.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if not g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(hdr), 0):
            return None
        return buf.raw
    finally:
        if bmp:
            g.DeleteObject(bmp)
        if mdc:
            g.DeleteDC(mdc)
        if sdc:
            u.ReleaseDC(0, sdc)


class FloatingBar(threading.Thread):
    """작업표시줄에 얹히는 투명 세 줄 바 — 스킬 패널 + 사용량 패널.

    왼쪽부터 실행 중인 앱의 스킬 패널(Claude·Codex), 맨 오른쪽(트레이 쪽)에
    예전 사용량 바 그대로의 세션/주간/모델별 패널을 나란히 표시한다.
    클릭하면 전체 스킬 목록이 열린다. 드래그로 이동(위치 저장), 우클릭 숨김,
    잠금 시 클릭이 통과한다.
    z순서는 작업표시줄에 맞춘다 — 소유자로 지정해 바로 위에 두고, topmost
    여부까지 따라가서 전체화면 앱이 뜨면 작업표시줄과 함께 아래로 내려간다.
    """

    LINES = 3               # 작업표시줄 48px에 12px 줄 3개 + 여백 6px
    MAX_PANELS = 4          # 업데이트 + 루틴 알림 + Codex 사용량 + Claude 사용량
    PANEL_GAP = 20
    ACCENTS = {"claude": "#d97757", "codex": "#45a79a", "notify": "#c58a1a",
               "update": "#4f86e8"}
    FONT_PX = -10           # 음수 = 픽셀 지정. 8pt(11px)에서 한 단계만 줄인 값
    PAD = 8                 # 좌우 여백 — 모든 줄의 라벨이 여기서 시작한다
    TICK_MS = 500           # 전체화면 전환을 늦게 알아채지 않도록 짧게 (2초→0.5초)
    RESTORE_MS = 30         # 숨어 있는 동안에만 도는 복귀 확인 (평소엔 안 돈다)
    HIDE_MS = 100           # 훅이 놓쳤을 때를 위한 보험 (평소엔 훅이 먼저 잡는다)
    CAMO_EVERY = 20         # 10초마다 동결 검사(_health_check)
    ADOPT_EVERY = 120       # 60초마다 소유 관계 재확인
    CAMO_MAX_AGE = 300      # 옆 픽셀이 그대로여도 이 시간이 지나면 한 번 다시 찍는다
    CAMO_SYNC_SEC = 0.5     # 작업표시줄 색 따라가기 주기 (_camo_loop)
    CAMO_CONFIRM = 2        # 왼쪽 조각이 이만큼 연속으로 다르면 다시 입힌다 (≈1초)
    CAMO_CONFIRM_RIGHT = 4  # 오른쪽은 트레이 아이콘 호버가 닿아 더 오래 본다 (≈2초)
    CAMO_TOL = 3            # 채널당 이 이하 차이는 같은 색
    SIDE = 12               # 배경을 떠올 좌우 여백 폭
    CAMO_SETTLE_MS = 180    # 폭이 바뀐 뒤 배경을 찍기까지 기다리는 시간
    CAMO_MAX_SD = 12        # 이보다 거친 조각은 작업표시줄이 아니다(글자·아이콘)
    BG = "#1f1f1f"          # 첫 픽셀 샘플링 전까지의 임시 배경
    PAL_DARK = {"label": "#a6a6a6", "value": "#dcdcdc", "time": "#7a7a7a"}
    PAL_LIGHT = {"label": "#5f5f5f", "value": "#1f1f1f", "time": "#909090"}

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app

    def run(self):
        while not self.app.stop_evt.is_set():
            try:
                self._run()
            except Exception:
                log.exception("floating bar crashed")
            finally:
                self._unhook_events()   # 예외로 튀어나온 경우까지 확실히
            if self.app.stop_evt.is_set():
                break
            time.sleep(5)   # 탐색기 재시작 등으로 창이 죽으면 새로 만든다

    def _run(self):
        import tkinter as tk
        import tkinter.font as tkfont
        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        # topmost는 고정값이 아니라 작업표시줄을 따라간다(_sync_topmost).
        # 소유 관계만으로는 위에 못 뜬다 — topmost 창은 별도 밴드라서
        # 소유자가 topmost면 non-topmost 소유 창은 그 아래로 가라앉는다.
        root.configure(bg=self.BG)
        # 모니터 배율(DPI) 반영 — 바의 글자·여백은 픽셀 단위라, 배율을 곱하지
        # 않으면 125%·150% 모니터에서 작업표시줄만 커지고 글자는 그대로 남아
        # 깨알같이 보인다. 100% 모니터에서는 배율 1.0이라 지금과 똑같다.
        scale = self._scale = self._dpi_scale()
        px = self._px = lambda v: int(round(v * scale))
        try:
            # 포인트 단위 폰트(스킬 내역·알림 창)도 같은 배율을 따르게 한다
            root.tk.call("tk", "scaling", scale * 96 / 72.0)
        except Exception:
            pass
        self.PAD = px(FloatingBar.PAD)
        self.PANEL_GAP = px(FloatingBar.PANEL_GAP)
        self.SIDE = px(FloatingBar.SIDE)
        f = self._font = tkfont.Font(family="맑은 고딕",
                                     size=px(self.FONT_PX))
        # -8px는 한글이 뭉개진다 — 본문(-10)보다 한 단계만 작게
        self._font_small = tkfont.Font(family="맑은 고딕", size=px(-9))
        self._col_gap = max(px(9), f.measure(" ") * 3)  # 라벨-숫자 사이 간격
        self._panel_w = f.measure("image-prompt-craft  999회  · 자동 999") + px(24)
        self._usage_w = f.measure("Sonnet 100%  · 16시간 59분 후") + px(24)
        self._fix_w = self._usage_w
        self._fix_h = self.LINES * f.metrics("linespace") + px(6)
        self._shown = False
        self._covered = 0
        self._ticks = 0
        # 배경 동기화 — 틱이 지금 바 자리(_camo_geom)를 알려 주고,
        # _camo_loop 스레드가 떠 둔 새 배경(_camo_pending)을 틱이 입힌다
        self._camo_geom = None          # (x, y, w, h) — 숨었거나 캡처 중이면 None
        self._camo_pending = None
        self._camo_ref = (None, None)   # 지금 입힌 배경을 뜬 순간의 양옆 평균색
        self._camo_log_at = 0.0
        self._snip_active = False
        self._recapture = False
        self._mapped = False        # Tk deiconify는 처음 한 번만 (이후 Win32로)
        self._hwnd = 0              # 콜백이 쓸 창 핸들 캐시 — 틱이 갱신한다
        self._clear = 0             # 가림이 풀린 연속 틱 수 (되보이기 디바운스)
        self._fs_hidden = False     # 전체화면 때문에 숨은 상태인가
        self._restore = False       # 훅이 먼저 띄웠으니 틱이 마무리하라는 표시
        self._watching = False      # 복귀 감시 루프가 도는 중인가
        self._hide_pending_log = False  # 훅이 숨겼다 — 판단 근거는 틱이 남긴다
        self._sunk_log_at = 0.0     # "가라앉음" 로그 최근 시각 (5초 간격 제한)
        self._camo_at = 0.0
        self._camo_retry = 0        # 거친 조각을 만나 다시 노린 횟수 (상한 5)
        self._rebuild = False       # 동결·배율 변화 감지 — 다음 틱에 창 재생성
        self._strikes = 0           # 동결 의심 연속 횟수 (2회면 재생성)
        # 이 창에 칠하려 한 배경색들의 파랑 채널 — 동결 판정의 기준점이다
        # (_health_check). 첫 항목은 첫 촬영 전까지의 임시 배경.
        self._painted = [int(self.BG[5:7], 16)]
        self._pal = self.PAL_DARK
        self._bgimg = None
        self._probe_warned = False
        self._last = [None] * (self.LINES * self.MAX_PANELS)
        self._panel_widths = [self._usage_w]
        self._panel_kinds = ["usage"]
        self._details = None
        self._notes = None      # 루틴 알림 목록 창
        self._whats = None      # 업데이트 소식 창
        self._whats_mode = None
        self._whats_status = None
        self._whats_btn = None
        self._detail_tree = None
        self._detail_summary = None
        self._detail_filter = "all"     # 전체 / claude / codex
        self._detail_rows_key = None    # 내용이 바뀔 때만 목록을 다시 그린다
        self._detail_desc = None
        self._detail_filter_btns = {}
        self._desc_lang = "kr"          # kr = 영어 설명을 한국어로 번역해 표시
        self._desc_current = None       # 지금 설명을 보여주는 (client, name)
        self._desc_waiting = None       # 번역을 기다리는 (client, name)
        self._lang_btns = {}
        self._trans_cache = {}
        self._trans_pending = set()
        cv = self.cv = tk.Canvas(root, width=self._fix_w, height=self._fix_h,
                                 highlightthickness=0, bd=0, bg=self.BG)
        cv.pack()
        self._img_item = cv.create_image(0, 0, anchor="nw")
        self._ys = tuple(self._fix_h * (2 * i + 1) // (2 * self.LINES)
                         for i in range(self.LINES))
        # 값은 오른쪽 정렬(anchor="e") — 9%·76%·100%의 끝이 한 줄로 맞고,
        # 뒤따르는 시간도 세 줄이 같은 x에서 시작한다
        self.items = [(cv.create_text(self.PAD, y, anchor="w", font=f, text="",
                                      fill=self._pal["label"]),
                       cv.create_text(self.PAD, y, anchor="e", font=f, text="",
                                      fill=self._pal["value"]),
                       cv.create_text(self.PAD, y, anchor="w", font=f, text="",
                                      fill=self._pal["time"]),
                       cv.create_text(self.PAD, y, anchor="w", text="",
                                      font=self._font_small,
                                      fill=self._pal["time"]))
                      for _ in range(self.MAX_PANELS) for y in self._ys]
        for w in (root, cv):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._save_pos)
            w.bind("<Button-3>", self._hide_click)
        self._place_initial()
        self._adopt_by_taskbar()
        root.update_idletasks()  # 이걸 해야 최상위 창이 생긴다(그전엔 GetParent=0)
        self._apply_lock()       # 첫 deiconify 전에 걸어야 그때부터 안 뺏는다
        self._hook_events()      # 전체화면 전환은 훅이 즉시 받는다
        # 창을 새로 만들 때마다 세대를 올린다 — 옛 창의 동기화 스레드는 스스로 끝난다
        self._gen = getattr(self, "_gen", 0) + 1
        threading.Thread(target=self._camo_loop, args=(self._gen,),
                         daemon=True).start()
        self._tick()
        self._watch_hide()       # 훅이 놓친 경우의 보험
        root.mainloop()

    def _adopt_by_taskbar(self):
        """작업표시줄을 소유자(owner)로 지정 — 그 바로 위 z에 상시 고정.

        작업표시줄이 z순서를 되찾을 때 소유 창은 같은 순간 함께 올라오므로
        타이머 lift로 쫓아갈 필요가 없고(깜빡임 소멸), 전체화면 앱이
        작업표시줄을 덮으면 같이 덮여 자연스럽게 가려진다.
        """
        try:
            u = ctypes.windll.user32
            u.FindWindowW.restype = ctypes.c_void_p
            u.GetWindow.restype = ctypes.c_void_p
            tray = u.FindWindowW("Shell_TrayWnd", None)
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            if not tray or not hwnd:
                return
            if u.GetWindow(ctypes.c_void_p(hwnd), 4) == tray:
                return              # 이미 걸려 있음 — Tk가 지웠을 때만 다시 건다
            u.SetWindowLongPtrW.restype = ctypes.c_void_p
            u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_void_p]
            u.SetWindowLongPtrW(ctypes.c_void_p(hwnd), -8,
                                ctypes.c_void_p(tray))
            self._hwnd = hwnd       # 소유가 풀렸다 = Tk가 창을 새로 만들었다
            log.info("bar owned by taskbar")
        except Exception:
            log.exception("adopt failed")

    def _sync_topmost(self, raise_now=False):
        """작업표시줄이 topmost일 때만 바를 그 바로 위로 올린다.

        전체화면 앱이 뜨면 Windows가 작업표시줄의 topmost를 뗀다. 그렇다고 바를
        `HWND_NOTOPMOST`로 함께 내리면 안 된다 — 그 호출은 '내린다'가 아니라
        '일반 창 중 맨 위로 올린다'라서, 소유자인 작업표시줄까지 영상 바로 위로
        끌어올려 **작업표시줄이 전체화면 위에 남는다**(실측: 위젯을 끄면 작업표시줄
        z가 20위로 가라앉고, 켜면 영상 바로 위 3위에 고정됐다).
        그래서 전체화면 동안에는 z를 아예 건드리지 않고 바를 숨기기만 한다
        (`_update`가 작업표시줄 topmost를 보고 숨긴다).
        """
        try:
            u = ctypes.windll.user32
            u.FindWindowW.restype = ctypes.c_void_p
            if not _tray_topmost():
                return              # 전체화면 — z는 그대로 두고 숨는 쪽에 맡긴다
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            # 실제 상태를 매번 읽으므로 어긋나 있으면 스스로 복구된다
            if not bool(u.GetWindowLongW(hwnd, -20) & 0x8):
                u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1),
                               0, 0, 0, 0, 0x0013)  # NOSIZE|NOMOVE|NOACTIVATE
                raise_now = True
                log.info("bar topmost -> True")
            if not raise_now and not self._above_tray(u, hwnd):
                # 캡처 오버레이가 닫힐 때 Windows가 작업표시줄을 밴드 맨 위로
                # 되올리며 바 위로 올라탄다(실측: 이때 바는 '표시 중'인데
                # 작업표시줄 뒤라 안 보이고, 60초 주기 재정렬 때야 돌아왔다)
                raise_now = True
                if time.time() - self._sunk_log_at > 5:
                    self._sunk_log_at = time.time()
                    log.info("bar sank below taskbar - re-raising")
            if raise_now:
                u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(0),
                               0, 0, 0, 0, 0x0013)  # HWND_TOP — 작업표시줄 위로
        except Exception:
            log.exception("topmost sync failed")

    def _above_tray(self, u, hwnd):
        """바가 작업표시줄보다 z 위에 있는가 — 작업표시줄에서 위로 걸어 찾는다.

        위로 걷다 바를 만나면 위에 있는 것이고, 꼭대기까지 못 만나면
        작업표시줄 아래로 가라앉은 것이다.
        """
        u.GetWindow.restype = ctypes.c_void_p
        u.FindWindowW.restype = ctypes.c_void_p
        tray = u.FindWindowW("Shell_TrayWnd", None)
        if not tray or not hwnd:
            return True
        h = tray
        for _ in range(64):
            h = u.GetWindow(ctypes.c_void_p(h), 3)  # GW_HWNDPREV — 한 칸 위
            if not h:
                return False
            if h == hwnd:
                return True
        return True     # 밴드에 창이 이례적으로 많으면 판단 보류 (오탐 방지)

    def _sample_sides(self, geom):
        """(x, y, w, h)에 놓인 바의 양옆 작업표시줄 조각 — BitBlt 한 번.

        돌려주는 값은 (왼쪽, 오른쪽). 각 쪽은 (이미지, 평균색, 매끈한가),
        화면 밖이면 None이다. 둘 다 없으면 None. Tk를 건드리지 않으므로
        동기화 스레드에서도 부른다.
        """
        from PIL import Image, ImageStat
        x, y, w, h = geom
        s = self.SIDE
        u = ctypes.windll.user32
        vx, vw = u.GetSystemMetrics(76), u.GetSystemMetrics(78)  # 가상 화면
        x0, x1 = max(x - s, vx), min(x + w + s, vx + vw)
        lw, rw = x - x0, x1 - (x + w)
        if lw <= 0 and rw <= 0:
            return None
        raw = blit_bgra(x0, y, x1 - x0, h)
        if raw is None:
            return None
        span = Image.frombytes("RGB", (x1 - x0, h), raw, "raw", "BGRX")

        def side(box):
            part = span.crop(box)
            st = ImageStat.Stat(part)
            mean = tuple(int(round(v)) for v in st.mean[:3])
            # 거친 조각은 작업표시줄 빈 구간이 아니다(글자·아이콘·툴팁) —
            # 바가 방금 비운 자리엔 우리 글자가 아직 남아 있기도 하다.
            # 그걸 늘여 배경에 구우면 화면이 깨진 것처럼 보인다(사용자 신고)
            return part, mean, max(st.stddev[:3]) <= self.CAMO_MAX_SD

        left = side((0, 0, lw, h)) if lw > 0 else None
        right = side((span.width - rw, 0, span.width, h)) if rw > 0 else None
        return left, right

    def _compose(self, left, right, w, h):
        """매끈한 조각만으로 바 배경을 만든다 — 둘이면 가로 그라데이션.

        바가 놓인 구간은 아이콘 없는 매끈한 자리라 좌우 끝을 이어 붙이면
        실제와 같다. 예전처럼 바를 잠깐 숨기고 그 자리를 찍으면 그 순간이
        눈에 띈다. 쓸 조각이 없으면 None.
        """
        from PIL import Image
        lft = left[0].resize((w, h)) if left and left[2] else None
        rgt = right[0].resize((w, h)) if right and right[2] else None
        if lft is None or rgt is None:
            return lft or rgt
        ramp = Image.new("L", (w, 1))
        ramp.putdata([255 * i // max(w - 1, 1) for i in range(w)])
        return Image.composite(rgt, lft, ramp.resize((w, h)))

    def _apply_camo(self, img, left, right, why):
        """만든 배경을 입힌다 (Tk 스레드). 글자 팔레트도 밝기에 맞춘다."""
        from PIL import ImageTk
        self._bgimg = ImageTk.PhotoImage(img)
        self.cv.itemconfigure(self._img_item, image=self._bgimg)
        r, gr, b = img.resize((1, 1)).getpixel((0, 0))[:3]
        lum = 0.299 * r + 0.587 * gr + 0.114 * b
        pal = self.PAL_LIGHT if lum >= 128 else self.PAL_DARK
        if pal is not self._pal:
            self._pal = pal
            self._last = [None] * (self.LINES * self.MAX_PANELS)  # 글자색 다시
        self._camo_ref = (left[1] if left and left[2] else None,
                          right[1] if right and right[2] else None)
        self._camo_at = time.time()
        self._note_painted(b)
        # 따라가기(sync)는 게임·영상이 뒤에서 돌면 초마다 일어난다 — 로그는
        # 분에 한 줄만. 즉시 뜬 것(now)은 원인 추적용이라 늘 남긴다
        now = time.time()
        if why != "sync" or now - self._camo_log_at >= 60:
            self._camo_log_at = now
            log.info("bar camo #%02x%02x%02x (%s)", r, gr, b, why)

    def _match_background(self, force=True):
        """지금 바 양옆을 떠서 곧바로 배경으로 입힌다 (Tk 스레드).

        표시 직후·폭이 바뀐 뒤·옮긴 뒤처럼 기다리면 안 되는 순간에 부른다.
        평소 작업표시줄 색을 따라가는 일은 _camo_loop가 한다(force는 예전
        호출과의 호환용이다 — 이제 늘 즉시 뜬다).
        """
        if self._snip_active and self._bgimg is not None:
            return      # 캡처 오버레이로 어두워진 화면을 배경으로 찍으면 안 됨
        try:
            self.root.update_idletasks()    # geometry 반영 전 winfo_x()=0 방지
            geom = (self.root.winfo_x(), self.root.winfo_y(),
                    self._fix_w, self._fix_h)
            sides = self._sample_sides(geom)
        except Exception:
            log.exception("bg capture failed")
            return
        if not sides:
            # 조용히 포기하면 바가 임시 배경(#1f1f1f) 그대로 검게 남는다 —
            # 원인 추적이 되도록 창당 한 번은 남긴다. 재시도는 틱이 한다.
            if self._bgimg is None and not self._probe_warned:
                self._probe_warned = True
                log.info("camo probe failed at %d,%d", geom[0], geom[1])
            return
        left, right = sides
        if self._bgimg is None:
            # 첫 촬영 전에도 검정을 보여주지 않는다 — 옆 조각 평균색으로 먼저
            # 칠하고 글자 팔레트도 그 밝기에 맞춘다
            m = (left or right)[1]
            solid = "#%02x%02x%02x" % m
            self.root.configure(bg=solid)
            self.cv.configure(bg=solid)
            lum = 0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]
            self._pal = self.PAL_LIGHT if lum >= 128 else self.PAL_DARK
            self._note_painted(m[2])
        img = self._compose(left, right, geom[2], geom[3])
        if img is None:
            # 양쪽 다 못 믿을 조각 — 쓰던 배경을 그대로 두고 다시 노린다.
            # 폭이 막 바뀐 참이면 남은 배경은 폭이 안 맞으니 곧바로 다시
            # 노린다. 몇 번 해도 안 되면 동기화 스레드에 맡긴다.
            if self._camo_retry < 5:
                self._camo_retry += 1
                self.root.after(self.CAMO_SETTLE_MS, self._match_background)
            log.info("camo skipped: both strips look busy (retry %d)",
                     self._camo_retry)
            return
        self._camo_retry = 0
        self._apply_camo(img, left, right, "now")

    def _apply_pending_camo(self, geom):
        """동기화 스레드가 떠 둔 새 배경을 입힌다 — 그사이 옮기거나 폭이
        바뀌었으면(다른 자리의 색이다) 버린다."""
        pending, self._camo_pending = self._camo_pending, None
        if pending is None or self._snip_active or pending[0] != geom:
            return
        _, img, (left, right) = pending
        self._apply_camo(img, left, right, "sync")

    def _camo_loop(self, gen):
        """작업표시줄 색을 따라가는 동기화 스레드 — 0.5초마다 양옆만 떠 본다.

        Windows 11 작업표시줄은 반투명(아크릴)이라 뒤에 있는 창에 따라 색이
        계속 변한다 — 뒤에서 게임·영상이 돌면 매 프레임. 예전에는 10초마다
        픽셀 넷을 보고 두 번 연속 같은 색일 때만 다시 떠서, 색이 계속
        변하는 동안엔 최대 5분까지 옛 색으로 남았다(2026-09-23 제보: 바만
        베이지로 떠 있음). 이제 옆 조각이 입힌 색과 1초(오른쪽은 트레이
        아이콘 호버가 닿아 2초) 넘게 다르면 곧바로 새 배경을 만들어 둔다.

        화면 읽기는 DWM 한 프레임(약 13ms)을 기다리므로 Tk 스레드에서 하지
        않는다 — 여기서 뜨고 만들고, 입히기만 틱이 한다(_apply_pending_camo).
        """
        miss = [0, 0]
        need = (self.CAMO_CONFIRM, self.CAMO_CONFIRM_RIGHT)
        while gen == self._gen and \
                not self.app.stop_evt.wait(self.CAMO_SYNC_SEC):
            geom = self._camo_geom
            if geom is None or self._camo_pending is not None:
                continue
            try:
                sides = self._sample_sides(geom)
            except Exception:
                continue
            if not sides:
                continue
            ref = self._camo_ref
            for i, cur in enumerate(sides):
                if cur is not None and cur[2] and ref[i] is not None and \
                        max(abs(a - b) for a, b in zip(cur[1], ref[i])) \
                        > self.CAMO_TOL:
                    miss[i] += 1
                else:
                    miss[i] = 0
            stale = time.time() - self._camo_at > self.CAMO_MAX_AGE
            if not (stale or miss[0] >= need[0] or miss[1] >= need[1]):
                continue
            img = self._compose(sides[0], sides[1], geom[2], geom[3])
            if img is not None:
                self._camo_pending = (geom, img, sides)
                miss = [0, 0]

    def _value_color(self, pct):
        """pct는 항상 '쓴 비율' — 표시 모드와 무관하게 위험색 기준은 같다."""
        if pct >= 90:
            return "#da3633"
        if pct >= 70:
            return "#bb8009"
        return self._pal["value"]

    def _disp_pct(self, pct):
        """표시용 값 — 기본은 쓴 비율(0에서 시작), 트레이 메뉴의 "남은
        비율로 표시"를 켜면 100에서 깎이는 값이 된다."""
        if self.app.cfg.get("usage_remaining"):
            return max(0.0, 100.0 - pct)
        return pct

    def _apply_lock(self):
        """잠금이면 클릭 통과(WS_EX_TRANSPARENT), 아니면 해제.

        Alt-Tab 제외(TOOLWINDOW)와 활성화 금지(NOACTIVATE)는 항상 건다.
        NOACTIVATE가 없으면 숨었다 다시 나올 때 바가 포그라운드를 빼앗는다
        (deiconify는 SW_RESTORE라 '표시'가 아니라 '활성화하고 표시'다) —
        그러면 전체화면 영상이 전면에서 밀려나 Windows가 전체화면 모드를
        끝내고 작업표시줄을 영상 위로 다시 올린다.
        """
        locked = bool(self.app.cfg.get("bar_locked"))
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            # 실제 스타일을 매번 읽는다 — 창이 만들어지기 전에는 GetParent가 0이라
            # 엉뚱한 창에 걸릴 수 있는데, 그래도 다음 틱에 제자리를 찾는다
            style = u.GetWindowLongW(hwnd, -20)
            want = style | 0x80 | 0x08000000
            want = (want | 0x20) if locked else (want & ~0x20)
            if want != style:
                u.SetWindowLongW(hwnd, -20, want)
        except Exception:
            log.exception("lock apply failed")

    def _dpi_scale(self):
        """바가 놓일 모니터의 배율 (96dpi = 1.0). 못 읽으면 1.0.

        창을 만들기 전에 폰트 크기를 정해야 해서, 창이 아니라 저장된 바
        위치(없으면 주 모니터 트레이 근처)가 속한 모니터의 DPI를 좌표로
        찾는다. 이후 배율이 바뀌면(_health_check) 창을 새로 만들며 다시 읽는다.
        """
        try:
            u = ctypes.windll.user32
            right, y = self.app.cfg.get("bar_right"), self.app.cfg.get("bar_y")
            if right is None or y is None:
                pt = ctypes.wintypes.POINT(u.GetSystemMetrics(0) - 40,
                                           u.GetSystemMetrics(1) - 10)
            else:
                pt = ctypes.wintypes.POINT(int(right) - 10, int(y) + 5)
            u.MonitorFromPoint.restype = ctypes.c_void_p
            u.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT,
                                           ctypes.wintypes.DWORD]
            mon = u.MonitorFromPoint(pt, 2)     # MONITOR_DEFAULTTONEAREST
            dx, dy = ctypes.c_uint(96), ctypes.c_uint(96)
            ctypes.windll.shcore.GetDpiForMonitor(
                ctypes.c_void_p(mon), 0, ctypes.byref(dx), ctypes.byref(dy))
            return min(max(dx.value / 96.0, 1.0), 4.0)
        except Exception:
            return 1.0

    def _place_initial(self):
        """위치는 오른쪽 끝(트레이 쪽) 기준으로 복원한다.

        왼쪽 끝을 저장하면 시작 직후(패널 폭이 아직 좁을 때) 그 좌표에 놓였다가
        패널이 붙으며 왼쪽으로 자라서, 재시작 한 번마다 바 전체가 스킬 패널
        폭만큼 왼쪽으로 밀렸다(실측: 하루 새 1985→1550). 오른쪽 끝을 앵커로
        저장하면 폭이 어떻게 변해도 사용자가 지정한 자리가 유지된다.
        """
        w, h = self._fix_w, self._fix_h
        right, y = self.app.cfg.get("bar_right"), self.app.cfg.get("bar_y")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                   ctypes.byref(r), 0)
        if right is None:
            right = sw - self._px(330)             # 트레이 아이콘 왼쪽
        # 줄이 늘어 바가 높아지면 저장된 y로는 화면 아래로 넘친다 — 다시 맞춘다
        if y is None or int(y) + h > sh:
            if sh > r.bottom:                      # 작업표시줄이 아래쪽
                y = r.bottom + max((sh - r.bottom - h) // 2, 0)
            else:
                y = sh - h - 8
        # 여기서 정한 자리가 이후 모든 위치 계산(_anchor_xy)의 앵커다 — 창의
        # 현재 좌표를 되읽지 않으므로 숨김 중의 스테일 값이 끼어들 수 없다
        self.app.cfg["bar_right"] = int(right)
        self.app.cfg["bar_y"] = int(y)
        self.root.geometry(f"{w}x{h}+{int(right) - w}+{int(y)}")

    def _anchor_xy(self):
        """저장된 앵커(오른쪽 끝·y)로 현재 폭의 바가 놓일 왼쪽 위 좌표."""
        right = self.app.cfg.get("bar_right")
        y = self.app.cfg.get("bar_y")
        if right is None or y is None:      # _place_initial 전 — 있을 수 없지만
            self._place_initial()
            right, y = self.app.cfg["bar_right"], self.app.cfg["bar_y"]
        return int(right) - self._fix_w, int(y)

    def _press(self, e):
        if self.app.cfg.get("bar_locked"):
            return
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()
        self._press_xy = (e.x_root, e.y_root)
        self._dragged = False

    def _drag(self, e):
        if self.app.cfg.get("bar_locked") or not hasattr(self, "_dx"):
            return
        if abs(e.x_root - self._press_xy[0]) + abs(e.y_root - self._press_xy[1]) < 4:
            return
        self._dragged = True
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _save_pos(self, e):
        if self.app.cfg.get("bar_locked") or not hasattr(self, "_dx"):
            return
        if not self._dragged:
            # 알림 패널은 알림 목록, 업데이트 패널은 업데이트 소식, 그 밖은
            # 기존대로 스킬 내역
            kind = self._panel_kind_at(e.x_root - self.root.winfo_x())
            if kind == "notify":
                self._toggle_notifications()
            elif kind == "update":
                self._toggle_whats_new()
            else:
                self._toggle_details()
            return
        self.app.cfg["bar_right"] = self.root.winfo_x() + self._fix_w
        self.app.cfg["bar_y"] = self.root.winfo_y()
        save_config(self.app.cfg)
        self._match_background(force=True)  # 옮긴 자리의 배경으로 다시 위장

    def _hide_click(self, e):
        # 알림 패널 위에서의 우클릭은 "확인했다" — 목록을 열지 않고 그 자리에서
        # 읽음 처리하고, 다음 틱에 패널이 사라진다. 창을 여닫는 수고 없이 끄는
        # 길이 없어서 알림이 계속 남아 있다는 신고가 있었다.
        kind = self._panel_kind_at(e.x_root - self.root.winfo_x())
        if kind == "notify":
            self.app.notifications.mark_all_read()
            log.info("alerts marked read (right-click)")
            return
        if kind == "update":            # 같은 규칙 — 우클릭은 "봤다"
            self.app.mark_whats_new_seen(self._update_mode())
            log.info("update panel dismissed (right-click)")
            return
        self.app.cfg["bar_visible"] = False
        save_config(self.app.cfg)
        self._show(False)

    def _update_mode(self):
        """바의 업데이트 패널이 알리는 것 — 'available' · 'installed' · None.

        설치할 새 버전이 있고 아직 그 소식을 안 열어 봤으면 'available',
        방금 업데이트됐는데 바뀐 점을 아직 안 봤으면(WHATS_NEW_DAYS까지)
        'installed'. 설치 중에는 패널을 내리지 않는다 — 곧 재시작한다.
        """
        app = self.app
        info = app.update_info
        if info and (app.cfg.get("update_seen") != info[0] or app._updating):
            return "available"
        wn = app.cfg.get("whats_new")
        if wn and time.time() - (wn.get("at") or 0) < WHATS_NEW_DAYS * 86400:
            return "installed"
        return None

    def _update_panel(self):
        """업데이트 소식 — 알릴 게 있을 때만 생기고, 누르면 패치노트 창.

        루틴 알림과 같은 규칙: 평소에는 흔적이 없고, 열어 보거나 우클릭하면
        다음 틱에 사라진다.
        """
        mode = self._update_mode()
        if mode is None:
            return None
        accent = self.ACCENTS["update"]
        # 잠긴 바는 클릭이 통과한다 — 누르라는 안내 대신 트레이 메뉴를 가리킨다
        press = "트레이 메뉴에서" if self.app.cfg.get("bar_locked") else "눌러서"
        if mode == "available":
            ver = self.app.update_info[0]
            head = self.app.range_headline(_ver_tuple(__version__),
                                           _ver_tuple(ver),
                                           self.app.update_info[1])
            if self.app._updating:
                hint = "설치 중… 곧 재시작"
            elif self.app.update_error:
                hint = f"설치 실패 · {press} 다시 시도"
            else:
                hint = f"{press} 바뀐 점 보기·설치"
            # 지금 버전을 옆에 둔다 — 새 버전 번호만 있으면 무엇에서 무엇으로
            # 바뀌는지 알 수 없다
            lines = [("새 버전", f"v{ver}", f" · 지금 v{__version__}",
                      accent, accent, "")]
        else:
            ver = self.app.cfg["whats_new"].get("to") or __version__
            head = self.app.range_headline(*self.app.whats_new_range())
            hint = f"{press} 바뀐 점 보기"
            lines = [("업데이트", f"v{ver}", "", accent, accent, "완료")]
        if head:
            lines.append((head if len(head) <= 22 else head[:21] + "…",
                          "", "", None, None, ""))
        lines.append((hint, "", "", None, None, ""))
        while len(lines) < self.LINES:
            lines.append(("", "", "", None, None, ""))
        return {"width": self._panel_width(lines), "lines": lines}

    def _notify_panel(self):
        """루틴(예약 작업) 알림 — 안 읽은 게 없으면 패널을 아예 만들지 않는다.

        `_usage_panel`과 같은 규약으로 None을 돌려주면 `_panels`가 빼고,
        빈자리 없이 나머지 패널이 당겨진다. 그래서 평소에는 흔적이 없다가
        새 결과가 올 때만 바에 나타난다.
        """
        _, unread = self.app.notifications.snapshot()
        if not unread:
            return None
        accent = self.ACCENTS["notify"]
        lines = [("알림", f"{len(unread)}건", "", accent, accent, "루틴")]
        now = time.time()
        for row in reversed(unread[-(self.LINES - 1):]):        # 최신부터
            name = row["title"] or row["body"]
            if len(name) > 22:
                name = name[:20] + "…"
            rel = notify_ago(row["ts"], now)
            lines.append((name, "", f" · {rel}" if rel else "",
                          None, None, ""))
        while len(lines) < self.LINES:
            lines.append(("", "", "", None, None, ""))
        return {"width": self._panel_width(lines), "lines": lines}

    def _codex_panel(self):
        """Codex 사용량 — Claude 패널과 똑같은 꼴.

        첫 줄은 앱명(Codex, 앱색) 줄로 Claude의 세션처럼 짧은 창(일간류)이
        붙고, "주간"은 Claude처럼 제 줄에 기본색 라벨로 내려간다. 요금제에
        짧은 창이 없으면(ChatGPT Pro는 주간 하나뿐이다) 첫 줄에 주간 값을
        올리고 "주간" 꼬리표를 달아, 앱명만 덩그러니 남지 않게 한다.
        리셋 지난 값은 지어내지 않고 '리셋 지남'으로 둔다.

        앱명 옆에 창 길이 꼬리표("Codex 5시간")를 붙이지 않는다 — Claude
        패널 첫 줄이 라벨 없는 "Claude"라서 규격이 어긋난다(2026-08-26
        사용자 지시).
        """
        snap = self.app.codex_usage
        if not snap or not snap.get("windows"):
            return None
        # 실행 중인 앱만 바에 올린다 — 다른 패널들과 같은 규칙. 스냅샷
        # 수집은 계속 돌므로 Codex를 켜면 그 자리에서 바로 나타난다.
        if "codex" not in self.app.skill_tracker.snapshot()[0]:
            return None
        accent = self.ACCENTS["codex"]
        now = time.time()

        def row(label, win, lcolor, tag=""):
            resets = win.get("resets_at")
            if resets and now >= resets:
                return (label, "—", " · 리셋 지남", lcolor, accent, tag)
            t = short_reset(resets)
            return (label, f"{round(self._disp_pct(win['pct']))}%",
                    f" · {t}" if t else "",
                    lcolor, self._value_color(win["pct"]), tag)

        # 이틀 미만 창(일간류)만 앱명 줄에, 그 이상은 "주간" 줄로
        short = [w for w in snap["windows"] if (w.get("minutes") or 0) < 2880]
        week = [w for w in snap["windows"] if (w.get("minutes") or 0) >= 2880]
        if short:
            lines = [row("Codex", short[0], accent)]
        elif week:
            # 주간 창 하나뿐인 요금제(ChatGPT Pro 등) — 앱명 줄을 빈칸으로
            # 두지 않고 주간 값을 올리되, 세션 값으로 오해하지 않게 꼬리표를
            # 단다. Claude 패널의 첫 줄과 규격이 어긋나 보이지 않는 선.
            lines = [row("Codex", week.pop(0), accent, "주간")]
        else:
            lines = [("Codex", "", "", accent, accent, "")]
        for win in week[:self.LINES - 1]:
            lines.append(row("주간", win, None))
        while len(lines) < self.LINES:
            lines.append(("", "", "", None, None, ""))
        return {"width": self._panel_width(lines), "lines": lines}

    def _usage_panel(self):
        """예전 사용량 바의 세 줄 — 세션 / 주간(모든 모델) / 모델별."""
        rows, notice = self.app.rows, self.app.auth_notice
        if not rows and not notice:
            return None
        sess = week = model = None
        mname = "모델"
        for label, pct, reset in rows:
            if label == "현재 세션" and sess is None:
                sess = (pct, reset)
            elif label.startswith("주간 (") and week is None:
                week = (pct, reset)
            elif label.startswith("주간 ") and model is None:
                model = (pct, reset)
                mname = label[3:]           # "주간 Fable" → "Fable"
        lines = []
        # 첫 줄에 어느 앱의 사용량인지 표기 — Codex 패널과 헷갈리지 않게
        picks = (("Claude", sess), ("주간", week), (mname, model))
        for idx, (title, row) in enumerate(picks):
            if idx == self.LINES - 1 and notice:    # 마지막 줄을 재발급 안내로
                lines.append((notice, "", "", "#da3633", "#da3633", ""))
            elif row:
                pct, reset = row
                t = short_reset(reset)
                lines.append((title, f"{round(self._disp_pct(pct))}%",
                              f" · {t}" if t else "",
                              self.ACCENTS["claude"] if idx == 0 else None,
                              self._value_color(pct), ""))
            else:
                lines.append(("", "", "", None, None, ""))
        return {"width": self._panel_width(lines), "lines": lines}

    def _panels(self):
        """왼쪽부터 루틴 알림 · Codex 사용량 · Claude 사용량. 스킬 확인은
        바가 아니라 스킬 내역 창에서 한다(바 클릭 또는 트레이 메뉴).

        바는 오른쪽 끝이 앵커라 왼쪽으로 자라므로, 자주 나타났다 사라지는
        패널일수록 왼쪽에 둔다 — 알림이 가장 잦고, Codex는 기록이 8일
        넘게 없을 때만 빠진다.
        """
        panels, kinds = [], []
        # 업데이트 소식은 가장 드물게 나타났다 사라지므로 맨 왼쪽
        update = self._update_panel()
        if update:
            panels.append(update)
            kinds.append("update")
        notify = self._notify_panel()
        if notify:
            panels.append(notify)
            kinds.append("notify")
        codex = self._codex_panel()
        if codex:
            panels.append(codex)
            kinds.append("usage")
        usage = self._usage_panel()
        if usage:
            panels.append(usage)
            kinds.append("usage")
        # 클릭한 자리가 어느 패널인지 판정할 때 쓴다 (_panel_kind_at)
        self._panel_kinds = kinds[:self.MAX_PANELS]
        return panels[:self.MAX_PANELS]

    def _panel_kind_at(self, x):
        """바 안의 x 좌표가 어느 패널인지 — 알림 패널만 다른 창을 연다."""
        kinds = getattr(self, "_panel_kinds", [])
        base = 0
        for idx, width in enumerate(self._panel_widths):
            if base <= x <= base + width:
                return kinds[idx] if idx < len(kinds) else ""
            base += width + self.PANEL_GAP
        return ""

    def _label_block(self, row):
        """라벨 + 작은 꼬리표("루틴")가 차지하는 폭."""
        w = self._font.measure(row[0])
        if len(row) > 5 and row[5]:
            w += self._font_small.measure(row[5]) + self._px(4)
        return w

    def _panel_width(self, lines):
        """패널 내용에 꼭 맞는 폭 — 패널 사이 간격이 PANEL_GAP으로 균일해진다."""
        f = self._font
        label_w = max(self._label_block(row) for row in lines)
        value_w = max([f.measure(row[1]) for row in lines if row[1]] or [0])
        when_w = max([f.measure(row[2]) for row in lines if row[2]] or [0])
        return label_w + self._col_gap + value_w + when_w + 2 * self.PAD

    def _fit_widths(self, widths):
        """폭이 조금 준 것은 유지 — '3분 후'→'2분 후' 같은 분 단위 변화로
        매번 리사이즈(배경 재촬영·위치 저장)하지 않게. 늘어난 것은 즉시 반영해
        글자가 옆 패널을 침범하지 않는다."""
        if len(widths) != len(self._panel_widths):
            return widths
        return [old if new <= old and old - new <= 16 else new
                for new, old in zip(widths, self._panel_widths)]

    def _resize_panels(self, widths):
        if widths == self._panel_widths:
            return
        self._panel_widths = list(widths)
        self._fix_w = sum(widths) + self.PANEL_GAP * (len(widths) - 1)
        # 오른쪽 끝을 고정해 패널이 늘 때 트레이 쪽이 아니라 왼쪽으로 자란다.
        #
        # 앵커는 설정에 저장된 bar_right·bar_y가 유일한 진실이다. 예전처럼 창의
        # 현재 x에서 되계산하면(x + 폭), 기동 직후 _place_initial의 geometry가
        # 아직 반영되지 않은 x를 읽어 앵커가 패널 폭만큼 오염된다 — 재시작 한
        # 번에 +137px씩 오른쪽으로 밀려 결국 화면 밖으로 나갔다(실측 2548→2685→
        # 2822). y도 같은 이유로 winfo_y()를 읽지 않는다 — 창이 숨어 있는 동안
        # Tk는 위치 요청을 무시하고 옛 좌표(첫 실행이면 0,0)를 돌려주므로,
        # 그 값을 저장하면 바가 화면 위 끝에 붙어 버린다. 첫 실행이면
        # _place_initial이 정한 자리를 앵커로 삼는다. 쓰는 곳은 사용자가
        # 드래그로 자리를 정하는 _save_pos 한 곳뿐이다.
        x, y = self._anchor_xy()
        self.cv.configure(width=self._fix_w)
        self.root.geometry(f"{self._fix_w}x{self._fix_h}+{x}+{y}")
        save_config(self.app.cfg)
        self._last = [None] * (self.LINES * self.MAX_PANELS)
        self.root.update_idletasks()    # 새 geometry가 잡힌 뒤에 배경을 뜬다
        # 곧바로 찍지 않는다 — 바가 방금 비운 자리를 작업표시줄이 다시 그리기
        # 전이라 우리 글자가 남아 있고, 그걸 배경으로 구우면 화면이 깨져 보인다.
        # 쓰던 배경은 지우지 않고 두므로 그 사이 검은 사각형이 뜨지도 않는다.
        self.root.after(self.CAMO_SETTLE_MS,
                        lambda: self._match_background(force=True))

    def _toggle_notifications(self):
        if self._notes is not None:
            try:
                if self._notes.winfo_exists():
                    self._notes.destroy()
                    self._notes = None
                    return
            except Exception:
                pass
            self._notes = None
        self._open_notifications()

    def _open_notifications(self):
        """루틴이 남긴 결과 목록. 여는 순간 전부 읽음 처리 → 패널이 사라진다.

        항목은 카드형 — 안 읽은 것은 노란 틴트, 제목 줄 오른쪽에 상대
        시각, 아래 본문, 바닥에 기록 시각과 (있으면) 실행 버튼. '모두
        지우기'는 로그 파일이 아니라 위젯의 표시 상태만 지운다.
        """
        import tkinter as tk

        all_rows, unread = self.app.notifications.snapshot()
        rows = all_rows[self.app.notifications.cleared_count():]
        unread_from = len(all_rows) - len(unread)

        win = self._notes = tk.Toplevel(self.root)
        win.title("루틴 알림")
        win.overrideredirect(True)
        win.configure(bg="#f4f5f7", highlightbackground="#d9dce1",
                      highlightthickness=1)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)
        except tk.TclError:
            pass

        width, height = self._px(560), self._px(440)
        x = max(8, min(self.root.winfo_x() + self._fix_w - width,
                       self.root.winfo_screenwidth() - width - 8))
        y = max(8, self.root.winfo_y() - height - 10)
        win.geometry(f"{width}x{height}+{x}+{y}")

        head = tk.Frame(win, bg="#ffffff", height=54)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="Routine Alerts", bg="#ffffff", fg="#202124",
                 font=("Segoe UI Semibold", 14)).pack(side="left", padx=(18, 8))
        tk.Label(head, text=f"새 알림 {len(unread)}건 · 전체 {len(rows)}건",
                 bg="#ffffff", fg="#6b7280", font=("맑은 고딕", 9)
                 ).pack(side="left", pady=(3, 0))
        tk.Button(head, text="×", command=self._toggle_notifications,
                  relief="flat", borderwidth=0, bg="#ffffff",
                  activebackground="#eeeeee", fg="#6b7280",
                  font=("Segoe UI", 15), cursor="hand2"
                  ).pack(side="right", padx=(0, 14))
        if rows:
            tk.Button(head, text="모두 지우기", command=self._clear_alerts,
                      relief="flat", borderwidth=0, bg="#f4f5f7",
                      activebackground="#e9ebee", fg="#6b7280",
                      font=("맑은 고딕", 8), cursor="hand2", padx=10
                      ).pack(side="right", padx=(0, 10), pady=13)

        wrap = tk.Frame(win, bg="#f4f5f7")
        wrap.pack(fill="both", expand=True, padx=18, pady=(10, 6))
        bar = tk.Scrollbar(wrap, orient="vertical")
        bar.pack(side="right", fill="y")
        cv = tk.Canvas(wrap, bg="#f4f5f7", highlightthickness=0,
                       yscrollcommand=bar.set)
        cv.pack(side="left", fill="both", expand=True)
        bar.configure(command=cv.yview)
        card_w = width - 2 * 18 - 18            # 좌우 여백 + 스크롤바 자리
        inner = tk.Frame(cv, bg="#f4f5f7")
        cv.create_window((0, 0), window=inner, anchor="nw", width=card_w)
        inner.bind("<Configure>",
                   lambda e: cv.configure(scrollregion=cv.bbox("all")))
        # 카드 안 어디서 굴려도 목록이 굴러가게 전역으로 잡고, 닫힐 때 푼다
        win.bind_all("<MouseWheel>", lambda e: cv.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))
        win.bind("<Destroy>", lambda e: (
            win.unbind_all("<MouseWheel>") if e.widget is win else None))

        if not rows:
            tk.Label(inner, text="알림이 없습니다.", bg="#f4f5f7",
                     fg="#8a9099", font=("맑은 고딕", 9)
                     ).pack(anchor="w", pady=(6, 2))
            tk.Label(inner, text="예약 작업이 결과를 남기면 여기에 쌓이고, "
                                 "안 읽은 게 있을 때만 작업표시줄에 표시됩니다.",
                     bg="#f4f5f7", fg="#8a9099", font=("맑은 고딕", 9),
                     wraplength=card_w - 8, justify="left").pack(anchor="w")
        for row in reversed(rows[-100:]):       # 최신부터, 최근 100건
            fresh = row["index"] >= unread_from
            bg = "#fff7e6" if fresh else "#ffffff"
            edge = "#ecd9ac" if fresh else "#e3e6ea"
            card = tk.Frame(inner, bg=bg, highlightbackground=edge,
                            highlightthickness=1)
            card.pack(fill="x", pady=(0, 8))
            top = tk.Frame(card, bg=bg)
            top.pack(fill="x", padx=12, pady=(8, 0))
            if fresh:
                tk.Label(top, text="●", bg=bg, fg=self.ACCENTS["notify"],
                         font=("맑은 고딕", 8)).pack(side="left", padx=(0, 6))
            tk.Label(top, text=row["title"] or "(제목 없음)", bg=bg,
                     fg="#202124", font=("맑은 고딕", 9, "bold")
                     ).pack(side="left")
            rel = notify_ago(row["ts"])
            if rel:
                tk.Label(top, text=rel, bg=bg, fg="#8a9099",
                         font=("맑은 고딕", 8)).pack(side="right")
            if row["body"]:
                tk.Label(card, text=row["body"], bg=bg, fg="#4b5158",
                         font=("맑은 고딕", 9), wraplength=card_w - 26,
                         justify="left").pack(anchor="w", padx=12, pady=(2, 0))
            foot = tk.Frame(card, bg=bg)
            foot.pack(fill="x", padx=12, pady=(3, 8))
            tk.Label(foot, text=row["when"] or "", bg=bg, fg="#a3a9b1",
                     font=("맑은 고딕", 8)).pack(side="left")
            # 루틴이 "이걸 실행하면 된다"를 알려준 알림에만 버튼이 붙는다.
            # 없거나 파일이 사라졌으면 아무것도 안 붙는다(대부분의 알림).
            target = notify_run_target(row)
            if target is not None:
                tk.Button(foot, text="실행", relief="flat", borderwidth=0,
                          bg="#e8f0fe", activebackground="#d7e5fd",
                          fg="#1a56c4", font=("맑은 고딕", 8), cursor="hand2",
                          padx=10,
                          command=lambda p=target: self._run_alert_target(p)
                          ).pack(side="right")

        tk.Label(win, text=f"기록: {NOTIFY_LOG}", bg="#f4f5f7", fg="#8a9099",
                 font=("맑은 고딕", 8)).pack(anchor="w", padx=18, pady=(0, 10))

        win.bind("<Escape>", lambda e: self._toggle_notifications())
        # 창에 실제로 띄운 것까지만 읽음 — 그 사이 도착한 새 알림은 남겨 둔다.
        # 다음 틱에 바에서 패널이 사라진다(안 읽은 게 0이면 _notify_panel이 None).
        self.app.notifications.mark_all_read(len(all_rows))

    def _clear_alerts(self):
        """알림 창의 '모두 지우기' — 확인 후 목록을 비우고 창을 새로 그린다."""
        from tkinter import messagebox

        all_rows, _ = self.app.notifications.snapshot()
        n = len(all_rows) - self.app.notifications.cleared_count()
        if not n:
            return
        if not messagebox.askyesno(
                "모두 지우기",
                f"알림 {n}건을 목록에서 지울까요?\n"
                f"루틴 기록 파일은 그대로 남습니다.", parent=self._notes):
            return
        self.app.notifications.clear_all()
        log.info("alerts cleared (%d rows hidden)", n)
        if self._notes is not None:
            try:
                self._notes.destroy()
            except Exception:
                pass
            self._notes = None
        self._open_notifications()

    def _run_alert_target(self, path):
        """알림이 가리킨 것을 연다 — 탐색기에서 더블클릭한 것과 같다.

        묻고 나서 연다: 로그는 평문이라 이 계정으로 쓸 수 있는 것이면 무엇이든
        한 줄 붙일 수 있으니, 무엇이 열리는지 전체 경로를 보여주고 사용자가
        승인할 때만 실행한다. 명령줄은 아예 받지 않는다(`run_target` 참고).
        """
        from tkinter import messagebox

        if not messagebox.askokcancel(
                "루틴 알림 — 실행",
                f"아래 항목을 실행할까요?\n\n{path}",
                parent=self._notes):
            return
        try:
            os.startfile(str(path))         # 셸 기본 동작 = 더블클릭
            log.info("alert target launched: %s", path)
        except OSError as e:
            log.error("alert target failed: %s (%s)", path, e)
            messagebox.showerror("루틴 알림 — 실행 실패",
                                 f"열지 못했습니다.\n\n{path}\n\n{e}",
                                 parent=self._notes)

    def _toggle_whats_new(self):
        if self._whats is not None:
            try:
                if self._whats.winfo_exists():
                    self._whats.destroy()
                    self._whats = None
                    return
            except Exception:
                pass
            self._whats = None
        self._open_whats_new()

    def _open_whats_new(self):
        """업데이트 소식 창 — 버전별 패치노트 카드, 필요하면 [지금 설치].

        세 가지로 열린다: 설치할 새 버전이 있을 때(그 사이 패치노트 +
        [지금 설치]), 방금 업데이트됐을 때(이전 → 지금 사이 패치노트),
        메뉴로 열었을 때(최근 변경 내용). 여는 순간 '봤다'로 처리돼 바의
        업데이트 패널이 내려간다. 패치노트는 EXE에 묶인 CHANGELOG에서
        읽으므로 업데이트 직후 오프라인이어도 보인다.
        """
        import tkinter as tk
        import webbrowser

        mode, subtitle, entries = self.app.whats_new_view()
        self._whats_mode = mode
        win = self._whats = tk.Toplevel(self.root)
        win.title("업데이트 소식")
        win.overrideredirect(True)
        win.configure(bg="#f4f5f7", highlightbackground="#d9dce1",
                      highlightthickness=1)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)
        except tk.TclError:
            pass

        width, height = self._px(560), self._px(460)
        x = max(8, min(self.root.winfo_x() + self._fix_w - width,
                       self.root.winfo_screenwidth() - width - 8))
        y = max(8, self.root.winfo_y() - height - 10)
        win.geometry(f"{width}x{height}+{x}+{y}")

        head = tk.Frame(win, bg="#ffffff", height=54)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="What's New", bg="#ffffff", fg="#202124",
                 font=("Segoe UI Semibold", 14)).pack(side="left", padx=(18, 8))
        tk.Label(head, text=subtitle, bg="#ffffff", fg="#6b7280",
                 font=("맑은 고딕", 9)).pack(side="left", pady=(3, 0))
        tk.Button(head, text="×", command=self._toggle_whats_new,
                  relief="flat", borderwidth=0, bg="#ffffff",
                  activebackground="#eeeeee", fg="#6b7280",
                  font=("Segoe UI", 15), cursor="hand2"
                  ).pack(side="right", padx=(0, 14))

        # 바닥 줄을 먼저 붙여야 본문이 늘어나도 버튼이 밀려나지 않는다
        foot = tk.Frame(win, bg="#f4f5f7")
        foot.pack(side="bottom", fill="x", padx=18, pady=(0, 12))
        tk.Button(foot, text="전체 패치 이력", relief="flat", borderwidth=0,
                  bg="#e9ebee", activebackground="#dfe2e6", fg="#374151",
                  font=("맑은 고딕", 8), cursor="hand2", padx=10, pady=3,
                  command=lambda: webbrowser.open(CHANGELOG_PAGE)
                  ).pack(side="left")
        self._whats_btn = None
        if mode == "available":
            if self.app.update_method() == "git":
                tk.Label(foot, text="개발 폴더(git)에서 실행 중 — git pull 로 "
                                    "업데이트하세요", bg="#f4f5f7",
                         fg="#6b7280", font=("맑은 고딕", 8)
                         ).pack(side="right")
            else:
                self._whats_btn = tk.Button(
                    foot, text="지금 설치", relief="flat", borderwidth=0,
                    bg="#1a56c4", activebackground="#174ea6", fg="#ffffff",
                    activeforeground="#ffffff", font=("맑은 고딕", 9, "bold"),
                    cursor="hand2", padx=14, pady=3,
                    command=self._install_from_whats_new)
                self._whats_btn.pack(side="right")
        self._whats_status = tk.Label(win, text="", bg="#f4f5f7",
                                      fg="#b42318", font=("맑은 고딕", 8),
                                      wraplength=width - 40, justify="left")
        self._whats_status.pack(side="bottom", anchor="w", padx=18,
                                pady=(0, 4))

        body = tk.Frame(win, bg="#f4f5f7")
        body.pack(fill="both", expand=True, padx=18, pady=(10, 8))
        bar = tk.Scrollbar(body, orient="vertical")
        bar.pack(side="right", fill="y")
        txt = tk.Text(body, wrap="word", bg="#ffffff", fg="#4b5158",
                      relief="flat", borderwidth=0, highlightthickness=1,
                      highlightbackground="#e3e6ea", padx=14, pady=10,
                      font=("맑은 고딕", 9), cursor="arrow",
                      spacing1=2, spacing3=3, yscrollcommand=bar.set)
        txt.pack(side="left", fill="both", expand=True)
        bar.configure(command=txt.yview)
        txt.tag_configure("ver", font=("Segoe UI Semibold", 12),
                          foreground="#202124", spacing1=8)
        txt.tag_configure("date", font=("맑은 고딕", 8), foreground="#8a9099")
        txt.tag_configure("item", lmargin1=4, lmargin2=18, spacing1=4)
        txt.tag_configure("b", font=("맑은 고딕", 9, "bold"),
                          foreground="#202124")
        txt.tag_configure("code", font=("Consolas", 9), background="#f1f3f5")
        if not entries:
            txt.insert("end", "패치노트를 불러오지 못했습니다 — 아래 '전체 패치 "
                              "이력'에서 볼 수 있어요.")
        for i, e in enumerate(entries):
            if i:
                txt.insert("end", "\n")
            txt.insert("end", f"v{e['v']}", "ver")
            if e["date"]:
                txt.insert("end", f"   {e['date']}", "date")
            txt.insert("end", "\n")
            for item in e["items"]:
                txt.insert("end", "•  ", "item")
                # **굵게** 와 `코드`만 살리고 나머지 마크다운은 그대로 둔다
                for part in re.split(r"(\*\*.+?\*\*|`[^`]+`)", item):
                    if part.startswith("**") and part.endswith("**"):
                        txt.insert("end", part[2:-2].replace("`", ""),
                                   ("item", "b"))
                    elif part.startswith("`") and part.endswith("`"):
                        txt.insert("end", part[1:-1], ("item", "code"))
                    elif part:
                        txt.insert("end", part, "item")
                txt.insert("end", "\n", "item")
        txt.configure(state="disabled")
        win.bind_all("<MouseWheel>", lambda ev: txt.yview_scroll(
            -1 if ev.delta > 0 else 1, "units"))
        win.bind("<Destroy>", lambda ev: (
            win.unbind_all("<MouseWheel>") if ev.widget is win else None))
        win.bind("<Escape>", lambda ev: self._toggle_whats_new())
        self._refresh_whats_new()
        self.app.mark_whats_new_seen(mode)
        log.info("whats-new opened (%s, %d entries)", mode, len(entries))

    def _install_from_whats_new(self):
        """[지금 설치] — 누른 것이 곧 확인이다. 진행 상황은 틱이 창에 그린다."""
        if self.app._updating:
            return
        self.app.update_error = None
        self.app.q.put(("update_now",))
        if self._whats_btn is not None:
            self._whats_btn.configure(state="disabled", text="설치 중…")
        if self._whats_status is not None:
            self._whats_status.configure(
                fg="#1a56c4", text="새 버전을 받는 중입니다 — 끝나면 위젯이 "
                                   "스스로 다시 시작합니다.")

    def _refresh_whats_new(self):
        """열린 업데이트 창의 설치 상태 줄 — 설치 중/실패와 다시 시도 안내."""
        if self._whats is None:
            return
        try:
            if not self._whats.winfo_exists():
                self._whats = None
                return
        except Exception:
            self._whats = None
            return
        err, busy = self.app.update_error, self.app._updating
        if self._whats_btn is not None:
            want = ("disabled", "설치 중…") if busy else \
                ("normal", "다시 시도" if err else "지금 설치")
            if (str(self._whats_btn.cget("state")),
                    self._whats_btn.cget("text")) != want:
                self._whats_btn.configure(state=want[0], text=want[1])
        if self._whats_status is not None and err and not busy:
            text = (f"설치하지 못했습니다: {err}\n다시 시도해도 안 되면 "
                    "저장소 폴더의 install.cmd를 다시 실행하세요 — 설정과 "
                    "기록은 그대로 남습니다.")
            if self._whats_status.cget("text") != text:
                self._whats_status.configure(fg="#b42318", text=text)

    def _toggle_details(self):
        if self._details is not None:
            try:
                if self._details.winfo_exists():
                    self._details.destroy()
                    self._details = None
                    return
            except Exception:
                self._details = None
        self._open_details()

    def _open_details(self):
        import tkinter as tk
        from tkinter import ttk

        win = self._details = tk.Toplevel(self.root)
        win.title("AI 스킬 사용 내역")
        win.overrideredirect(True)
        win.configure(bg="#f4f5f7", highlightbackground="#d9dce1",
                      highlightthickness=1)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)
        except tk.TclError:
            pass

        width, height = self._px(640), self._px(520)
        x = max(8, min(self.root.winfo_x() + self._fix_w - width,
                       self.root.winfo_screenwidth() - width - 8))
        y = max(8, self.root.winfo_y() - height - 10)
        win.geometry(f"{width}x{height}+{x}+{y}")

        head = tk.Frame(win, bg="#ffffff", height=54)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="AI Skill Activity", bg="#ffffff", fg="#202124",
                 font=("Segoe UI Semibold", 14)).pack(side="left", padx=(18, 8))
        self._detail_summary = tk.Label(
            head, text="", bg="#ffffff", fg="#6b7280",
            font=("맑은 고딕", 9)
        )
        self._detail_summary.pack(side="left", pady=(3, 0))
        tk.Button(head, text="×", command=self._toggle_details, relief="flat",
                  borderwidth=0, bg="#ffffff", activebackground="#eeeeee",
                  fg="#6b7280", font=("Segoe UI", 15), cursor="hand2"
                  ).pack(side="right", padx=14)

        bar = tk.Frame(win, bg="#f4f5f7")
        bar.pack(fill="x", padx=18, pady=(10, 6))
        self._detail_filter_btns = {}
        for key, label in (("all", "전체"), ("claude", "Claude"),
                           ("codex", "Codex")):
            b = tk.Button(bar, text=label, relief="flat", borderwidth=0,
                          cursor="hand2", font=("맑은 고딕", 9), padx=12,
                          pady=2,
                          command=lambda k=key: self._set_detail_filter(k))
            b.pack(side="left", padx=(0, 6))
            self._detail_filter_btns[key] = b
        tk.Label(
            bar,
            text="자동 = 모델 호출 · 수동 = /skill · ~ = Codex 추정",
            bg="#f4f5f7", fg="#7b818a", font=("맑은 고딕", 8),
        ).pack(side="right")
        self._style_filter_btns()

        style = ttk.Style(win)
        style.configure("Skill.Treeview", rowheight=27, borderwidth=0,
                        font=("맑은 고딕", 9), background="#ffffff",
                        fieldbackground="#ffffff", foreground="#30343b")
        style.configure("Skill.Treeview.Heading", font=("맑은 고딕", 8),
                        foreground="#69707a", background="#eef0f3")
        style.map("Skill.Treeview", background=[("selected", "#e8f0fe")],
                  foreground=[("selected", "#202124")])
        cols = ("app", "skill", "auto", "manual", "estimated", "total", "last")
        tree = self._detail_tree = ttk.Treeview(
            win, columns=cols, show="headings", style="Skill.Treeview", height=9
        )
        labels = {
            "app": "앱", "skill": "스킬", "auto": "자동", "manual": "수동",
            "estimated": "~추정", "total": "합계", "last": "마지막 실행",
        }
        widths = {
            "app": 66, "skill": 242, "auto": 48, "manual": 48,
            "estimated": 48, "total": 48, "last": 105,
        }
        for col in cols:
            tree.heading(col, text=labels[col])
            tree.column(col, width=widths[col], minwidth=widths[col],
                        anchor="w" if col in {"app", "skill", "last"} else "center")
        tree.tag_configure("claude", foreground="#a54f36")
        tree.tag_configure("codex", foreground="#176b63")
        tree.tag_configure("odd", background="#f7f8fa")
        tree.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        tree.bind("<<TreeviewSelect>>", self._on_detail_select)
        tree.bind("<Button-3>", self._detail_context)

        row = tk.Frame(win, bg="#f4f5f7")
        row.pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(row, text="스킬 설명", bg="#f4f5f7", fg="#69707a",
                 font=("맑은 고딕", 8)).pack(side="left")
        self._lang_btns = {}
        for key, label in (("kr", "한국어"), ("en", "원문")):
            b = tk.Button(row, text=label, relief="flat", borderwidth=0,
                          cursor="hand2", font=("맑은 고딕", 8), padx=9, pady=1,
                          command=lambda k=key: self._set_desc_lang(k))
            b.pack(side="right", padx=(4, 0))
            self._lang_btns[key] = b
        self._style_lang_btns()

        # 선택한 스킬의 SKILL.md description — 무엇에 쓰는 스킬인지
        desc = self._detail_desc = tk.Text(
            win, height=6, wrap="word", relief="flat", bg="#ffffff",
            fg="#30343b", font=("맑은 고딕", 9), padx=12, pady=8,
            state="disabled", cursor="arrow",
            highlightbackground="#e3e6ea", highlightthickness=1,
        )
        desc.tag_configure("title", font=("맑은 고딕", 9, "bold"),
                           foreground="#202124", spacing3=4)
        desc.tag_configure("dim", foreground="#8a9099")
        desc.pack(fill="x", padx=18, pady=(0, 14))
        self._set_desc_text("스킬을 클릭하면 설명이 여기 표시됩니다.", dim=True)

        win.bind("<Escape>", lambda e: self._toggle_details())
        self._detail_rows_key = None
        self._refresh_details()

    def _style_filter_btns(self):
        for key, b in self._detail_filter_btns.items():
            try:
                if not b.winfo_exists():
                    return
            except Exception:
                return
            on = key == self._detail_filter
            b.configure(bg="#3b4252" if on else "#e8eaee",
                        fg="#ffffff" if on else "#4b5563",
                        activebackground="#3b4252" if on else "#dde0e5",
                        activeforeground="#ffffff" if on else "#4b5563")

    def _set_detail_filter(self, key):
        if key == self._detail_filter:
            return
        self._detail_filter = key
        self._style_filter_btns()
        self._detail_rows_key = None
        self._refresh_details()

    def _set_desc_text(self, body, title=None, dim=False):
        t = self._detail_desc
        if t is None:
            return
        try:
            if not t.winfo_exists():
                return
        except Exception:
            return
        t.configure(state="normal")
        t.delete("1.0", "end")
        if title:
            t.insert("end", title + "\n", "title")
        t.insert("end", body, "dim" if dim else "")
        t.configure(state="disabled")

    def _on_detail_select(self, event=None):
        tree = self._detail_tree
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            return
        client, _, name = sel[0].partition("|")
        self._desc_current = (client, name)
        self._render_desc()

    def _style_lang_btns(self):
        for key, b in self._lang_btns.items():
            try:
                if not b.winfo_exists():
                    return
            except Exception:
                return
            on = key == self._desc_lang
            b.configure(bg="#3b4252" if on else "#e8eaee",
                        fg="#ffffff" if on else "#4b5563",
                        activebackground="#3b4252" if on else "#dde0e5",
                        activeforeground="#ffffff" if on else "#4b5563")

    def _set_desc_lang(self, lang):
        if lang == self._desc_lang:
            return
        self._desc_lang = lang
        self._style_lang_btns()
        self._render_desc()

    def _render_desc(self):
        """설명 패널 갱신 — 한국어 모드면 영어 설명을 번역해 보여준다.

        번역은 3단 캐시: 메모리 → DB(재시작해도 유지, 원문 해시가 바뀌면
        무효) → 그때만 네트워크. 로컬 SKILL.md가 없는 내장 스킬은
        내장 기본 설명(한국어)을 쓴다.
        """
        self._desc_waiting = None
        if self._desc_current is None:
            return
        client, name = self._desc_current
        title = f"{'Claude' if client == 'claude' else 'Codex'} · {name}"
        desc = self.app.skill_tracker.describe(client, name)
        if not desc:
            b = builtin_desc(name)
            if b:
                self._set_desc_text(b + "\n(내장 스킬 — 기본 제공 설명)",
                                    title=title)
            else:
                self._set_desc_text(
                    "정보 없음 — 이 스킬은 로컬 SKILL.md 설명도, 위젯에 "
                    "내장된 기본 설명도 없습니다.",
                    title=title, dim=True)
            return
        if self._desc_lang == "en" or _mostly_korean(desc):
            self._set_desc_text(desc, title=title)
            return
        ko = self._trans_cache.get((client, name))
        if not ko:
            ko = self.app.skill_tracker.cached_ko(client, name, desc)
            if ko:
                self._trans_cache[(client, name)] = ko
        if ko:
            self._set_desc_text(ko, title=title)
            return
        self._set_desc_text("한국어로 번역 중…", title=title, dim=True)
        self._desc_waiting = (client, name)
        if (client, name) not in self._trans_pending:
            self._trans_pending.add((client, name))
            threading.Thread(target=self._translate_worker,
                             args=(client, name, desc), daemon=True).start()

    def _translate_worker(self, client, name, text):
        ko = translate_ko(text)
        if ko:
            self.app.skill_tracker.store_ko(client, name, text, ko)
        self._trans_cache[(client, name)] = \
            ko or "(번역에 실패했습니다 — '원문' 버튼으로 봐 주세요)"
        self._trans_pending.discard((client, name))

    def _poll_translation(self):
        """번역 스레드가 끝났으면 설명 패널을 다시 그린다 (틱에서 호출)."""
        if self._desc_waiting and self._desc_waiting in self._trans_cache:
            self._render_desc()

    def _detail_context(self, e):
        """스킬 우클릭 메뉴 — 폴더 열기 / 휴지통으로 삭제."""
        import tkinter as tk
        tree = self._detail_tree
        if tree is None or self._details is None:
            return
        iid = tree.identify_row(e.y)
        if not iid:
            return
        tree.selection_set(iid)
        client, _, name = iid.partition("|")
        paths = self.app.skill_tracker.paths(client, name)
        dirs = sorted({os.path.dirname(p) for p in paths})
        home = os.path.normcase(HOME)
        # 사용자 홈 아래 + 스킬 전용 폴더만 삭제 대상 — 루트를 지우는 사고 방지
        deletable = [
            d for d in dirs
            if os.path.normcase(d).startswith(home)
            and os.path.basename(d).lower() not in
            ("skills", ".claude", ".codex", "")
        ]
        menu = tk.Menu(self._details, tearoff=0)
        if dirs:
            menu.add_command(label="폴더 열기",
                             command=lambda d=dirs[0]: os.startfile(d))
        if deletable:
            menu.add_command(
                label="휴지통으로 삭제…",
                command=lambda: self._delete_skill(client, name, deletable))
        else:
            menu.add_command(label="내장 스킬 — 삭제 불가", state="disabled")
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _delete_skill(self, client, name, dirs):
        from tkinter import messagebox
        ok = messagebox.askyesno(
            "스킬 삭제",
            f"'{name}' 스킬 폴더를 휴지통으로 보낼까요?\n\n"
            + "\n".join(dirs)
            + "\n\n(휴지통에서 언제든 복구할 수 있습니다)",
            parent=self._details)
        if not ok:
            return
        failed = [d for d in dirs if not send_to_recycle(d)]
        try:
            self.app.skill_tracker.refresh(force=True)
        except Exception:
            log.exception("refresh after skill delete failed")
        self._detail_rows_key = None
        self._refresh_details()
        log.info("skill deleted to recycle bin: %s/%s (%d/%d dirs)",
                 client, name, len(dirs) - len(failed), len(dirs))
        if failed:
            messagebox.showwarning(
                "스킬 삭제", "일부 폴더를 옮기지 못했습니다:\n"
                + "\n".join(failed), parent=self._details)

    def _refresh_details(self):
        if self._details is None or self._detail_tree is None:
            return
        try:
            if not self._details.winfo_exists():
                self._details = None
                return
        except Exception:
            self._details = None
            return
        _, _, rows = self.app.skill_tracker.snapshot()
        if self._detail_filter != "all":
            rows = [r for r in rows if r["client"] == self._detail_filter]
        # 많이 쓴 순 → 최근 쓴 순 → 이름 순
        rows = sorted(rows, key=lambda r: (-r["total_count"],
                                           -(r["last_used"] or 0), r["name"]))
        key = tuple(
            (r["client"], r["name"], r["auto_count"], r["manual_count"],
             r["estimated_count"], r["total_count"], r["last_used"])
            for r in rows)
        if key == self._detail_rows_key:
            return      # 내용 그대로 — 다시 그리면 선택·스크롤이 풀린다
        self._detail_rows_key = key
        tree = self._detail_tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        total_today = sum(row["today_count"] for row in rows)
        installed = sum(1 for row in rows if row["copies"])
        if self._detail_summary is not None:
            self._detail_summary.configure(
                text=f"설치 {installed}개  ·  오늘 {total_today}회"
            )
        for i, row in enumerate(rows):
            last = ""
            if row["last_used"]:
                last = time.strftime("%m/%d %H:%M",
                                     time.localtime(row["last_used"]))
            tags = [row["client"]]
            if i % 2:
                tags.append("odd")
            tree.insert(
                "", "end", iid=f"{row['client']}|{row['name']}",
                values=(
                    "Claude" if row["client"] == "claude" else "Codex",
                    row["name"], row["auto_count"], row["manual_count"],
                    row["estimated_count"], row["total_count"], last,
                ),
                tags=tuple(tags),
            )
        # 선택 보존 — 없어졌으면 첫 행을 골라 설명이 비지 않게 한다
        keep = [i for i in selected if tree.exists(i)]
        children = tree.get_children()
        if keep:
            tree.selection_set(keep)
        elif children:
            tree.selection_set(children[0])

    def _tick(self):
        if self.app.stop_evt.is_set() or self._rebuild:
            # _rebuild면 mainloop가 끝나고 run()의 재시도 루프가 새 창을 만든다.
            # 훅을 먼저 떼야 한다 — 죽은 창을 가리키는 콜백이 남으면 크래시
            self._unhook_events()
            self.root.destroy()
            return
        try:
            self._update()
        except Exception:
            log.exception("bar update failed")
        self.root.after(self.TICK_MS, self._tick)

    def _hook_events(self):
        """전체화면 창이 뜨는 즉시 콜백을 받도록 WinEvent 훅을 건다.

        훅은 이 스레드(Tk 메인루프)에 걸어야 콜백도 이 스레드로 온다 —
        그래야 콜백 안에서 바를 숨겨도 스레드 문제가 없다. 콜백에서 Tk 함수는
        부르지 않는다(`_win_show`가 Win32만 쓴다).
        """
        try:
            u = ctypes.windll.user32
            u.SetWinEventHook.restype = ctypes.c_void_p
            self._winproc = WINEVENTPROC(self._on_win_event)   # 참조 유지 필수
            self._hooks = [
                u.SetWinEventHook(ev, ev, None, self._winproc, 0, 0,
                                  WINEVENT_SKIPOWNPROCESS)
                for ev in (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_LOCATIONCHANGE)
            ]
            log.info("win event hooks: %s",
                     [bool(h) for h in self._hooks])
        except Exception:
            log.exception("hook install failed")

    def _unhook_events(self):
        """WinEvent 훅 해제 — 창을 새로 만들기 전에 반드시 부른다.

        훅을 건 채로 `_winproc`를 새 것으로 갈아치우면 옛 콜백 객체가
        수거되는데, 훅은 살아 있어서 Windows가 그 빈 자리를 호출한다.
        그 순간 프로세스가 통째로 죽는다 — 로그 한 줄 없이 사라진다
        (실측 2026-08-27: 재부팅 뒤 재생성 직후 APPCRASH c000041d).
        """
        for h in getattr(self, "_hooks", None) or []:
            try:
                if h:
                    ctypes.windll.user32.UnhookWinEvent(ctypes.c_void_p(h))
            except Exception:
                pass
        self._hooks = []

    def _watch_hide(self):
        """훅이 놓친 전환을 위한 보험 — 숨기는 쪽만 본다.

        훅은 창 이벤트가 있어야 오는데, 앱이 이벤트를 한 번만 보내고 그때 아직
        크기가 안 잡혀 있으면 놓친다(실측: 그럴 때 틱까지 0.4초를 기다렸다).
        값싼 검사 7번이라 이 주기가 CPU에 잡히지 않는다.
        """
        if self.app.stop_evt.is_set():
            return
        try:
            if self._shown and _fullscreen_now():
                self._log_hide("watch")
                self._show(False)
                self._fs_hidden = True
                if not self._watching:
                    self._watching = True
                    self.root.after(self.RESTORE_MS, self._watch_restore)
            elif self._shown and _tray_topmost():
                # z침몰 보험 — 창 이벤트가 없어도 100ms 안에는 되올라온다
                u = ctypes.windll.user32
                if not self._above_tray(u, self._hwnd):
                    u.SetWindowPos(ctypes.c_void_p(self._hwnd),
                                   ctypes.c_void_p(0), 0, 0, 0, 0, 0x0013)
        except Exception:
            log.exception("hide watch failed")
        self.root.after(self.HIDE_MS, self._watch_hide)

    def _watch_restore(self):
        """전체화면 때문에 숨어 있는 동안에만 도는 짧은 확인 — 끝나면 바로 돌아온다.

        평상시에는 아무것도 돌지 않는다(숨긴 뒤에만 살아나고, 돌아오면 죽는다).
        훅은 창 이벤트가 있어야 오는데, 전체화면이 끝나는 순간 작업표시줄이
        제자리를 찾기까지 10ms쯤 걸려서 그 사이 온 이벤트로는 판정이 안 된다 —
        그 틈을 이 루프가 메운다. Tk 스레드라 여기서는 배경까지 제대로 입힌다.
        """
        if self.app.stop_evt.is_set() or self._shown or not self._fs_hidden:
            self._watching = False
            return
        try:
            if not _fullscreen_now() and self.app.cfg.get("bar_visible", True):
                self._fs_hidden = False
                self._restore = False
                self._watching = False
                self._covered = 0
                self._show(True)
                return
        except Exception:
            log.exception("restore watch failed")
        self.root.after(self.RESTORE_MS, self._watch_restore)

    def _on_win_event(self, hook, event, hwnd, idobj, idchild, thread, ms):
        """창이 전면이 되거나 크기가 바뀐 순간 — 전체화면이면 숨고, 끝나면 돌아온다.

        이 콜백은 Tk의 메시지 펌프 한가운데서 불린다. 여기서 Tk/Tcl을 건드리면
        재진입이라 프로세스가 그대로 죽는다(실측: `winfo_id()`를 부르는 경로를
        넣었더니 pythonw가 0xc0000409로 크래시). 그래서 Win32만 쓰고, 창 핸들도
        미리 받아 둔 것을 쓴다 — 갱신은 틱이 한다.

        돌아올 때 배경·소유관계까지 여기서 손대면 Tk가 필요하므로, 창만 먼저
        띄우고 나머지는 `_restore` 표시를 남겨 다음 틱에 맡긴다.
        """
        if idobj != OBJID_WINDOW or not self._hwnd:
            return
        try:
            u = ctypes.windll.user32
            u.GetForegroundWindow.restype = ctypes.c_void_p
            if hwnd and hwnd != u.GetForegroundWindow():
                return              # 뒤쪽 창이 움직인 것 — 대부분 여기서 끝난다
            full = _fullscreen_now()
            if self._shown and full:
                u.ShowWindow(ctypes.c_void_p(self._hwnd), 0)    # SW_HIDE
                self._camo_geom = None      # 전체화면을 배경으로 뜨지 않게
                self._shown = False
                self._fs_hidden = True
                self._hide_pending_log = True   # 로그는 틱이 남긴다 (Tk 금지)
            elif (not self._shown and self._fs_hidden and not full
                  and self.app.cfg.get("bar_visible", True)):
                u.ShowWindow(ctypes.c_void_p(self._hwnd), 4)    # SHOWNOACTIVATE
                u.SetWindowPos(ctypes.c_void_p(self._hwnd), ctypes.c_void_p(0),
                               0, 0, 0, 0, 0x0013)              # HWND_TOP
                self._shown = True
                self._fs_hidden = False
                self._restore = True
            elif (self._shown and not full and _tray_topmost()
                  and not self._above_tray(u, self._hwnd)):
                # 캡처 오버레이가 닫히는 순간 작업표시줄이 바 위로 올라탄다 —
                # 다음 틱(최대 0.5초)을 기다리지 않고 이 이벤트에서 바로 되올린다
                u.SetWindowPos(ctypes.c_void_p(self._hwnd), ctypes.c_void_p(0),
                               0, 0, 0, 0, 0x0013)              # HWND_TOP
        except Exception:
            pass                    # 콜백에서 로그를 쏟지 않는다

    def _update(self):
        self._ticks += 1
        if self._ticks % 600 == 0:
            # 5분 심장박동 — "로그가 조용한 채 바가 안 보이던" 구간의 원인 추적용
            # (2026-07-30 저녁, 시작 후 17분간 아무 로그 없이 안 보인 사례)
            log.info("hb shown=%s fs_hidden=%s covered=%d clear=%d panels=%d",
                     self._shown, self._fs_hidden, self._covered, self._clear,
                     len(self._panel_widths))
        # 콜백에서 쓸 창 핸들은 여기(Tk 스레드)서만 구한다
        u0 = ctypes.windll.user32
        self._hwnd = u0.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        if self._hide_pending_log:
            self._hide_pending_log = False
            self._log_hide("hook")
        if self.app.details_requested.is_set():
            self.app.details_requested.clear()
            self._toggle_details()
        if self.app.notes_requested.is_set():
            self.app.notes_requested.clear()
            self._toggle_notifications()
        if self.app.whats_new_requested.is_set():
            self.app.whats_new_requested.clear()
            self._toggle_whats_new()
        self._refresh_whats_new()
        if not self.app.cfg.get("bar_visible", True):
            self._show(False)
            return
        panels = self._panels()
        if not panels:
            self._show(False)
            if self._details is not None:
                self._refresh_details()
            return
        self._resize_panels(self._fit_widths([p["width"] for p in panels]))
        covered, snip = _taskbar_covered()
        if self._snip_active and not snip:
            self._recapture = True  # 캡처가 끝났으면 그동안의 변화를 다시 입는다
        self._snip_active = snip
        self._sync_topmost()
        # 판정은 훅·보험과 같은 함수로 한다 — 기준이 다르면 한쪽은 숨기고 한쪽은
        # 띄워서 0.5초마다 깜빡인다(캡처 중 실측). `_fullscreen_now`가 캡처
        # 오버레이 예외까지 안에서 처리한다.
        fullscreen = _fullscreen_now()
        hide = covered or fullscreen
        self._covered = self._covered + 1 if hide else 0
        self._clear = 0 if hide else self._clear + 1
        if fullscreen or self._covered >= 2:    # 픽셀 판정만 2회 연속을 요구한다
            if self._shown:
                self._log_hide("tick", fullscreen)
            self._show(False)
            self._fs_hidden = bool(fullscreen)  # 되살릴 수 있는 건 이 경우뿐
            if self._fs_hidden and not self._watching:
                self._watching = True           # 숨어 있는 동안만 짧게 지켜본다
                self.root.after(self.RESTORE_MS, self._watch_restore)
            return
        if not self._shown and self._clear < 2 and not self._fs_hidden:
            return          # 가림이 풀린 직후 한 틱은 더 본다 — 되보이기 깜빡임 방지
        if self._restore:   # 훅이 먼저 띄워 놨다 — 배경·z는 여기서 마무리한다
            self._restore = False
            try:
                if self.root.state() != "normal":
                    # Win32로만 되살리면 Tk는 창이 내려간 줄 알고 그리기를
                    # 전부 버린다 — Tk 쪽 상태도 함께 되살린다(NOACTIVATE라
                    # deiconify가 포커스를 뺏지 않는다)
                    self.root.deiconify()
            except Exception:
                pass
            self._match_background(force=True)
            self._adopt_by_taskbar()
            self._sync_topmost(raise_now=True)
        geom = (self.root.winfo_x(), self.root.winfo_y(),
                self._fix_w, self._fix_h)
        if self._recapture and not snip:
            self._recapture = False
            self._match_background(force=True)
        elif self._bgimg is None:
            self._match_background(force=True)  # 첫 배경을 얻기까지 매 틱 재시도
        else:
            self._apply_pending_camo(geom)      # 동기화 스레드가 떠 둔 새 배경
        base = 0
        for panel_idx, panel in enumerate(panels):
            lines = panel["lines"]
            cols = self._columns(lines, base)
            for row_idx, (label, value, when, lcolor, vcolor, tag) in \
                    enumerate(lines):
                flat_idx = panel_idx * self.LINES + row_idx
                self._set_line(flat_idx, label, value, when,
                               vcolor or self._pal["value"], cols, base,
                               lcolor=lcolor, tag=tag)
            # 적용된 폭 기준 — _fit_widths가 유지시킨 폭과 어긋나지 않게
            base += self._panel_widths[panel_idx] + self.PANEL_GAP
        for panel_idx in range(len(panels), self.MAX_PANELS):
            for row_idx in range(self.LINES):
                flat_idx = panel_idx * self.LINES + row_idx
                self._set_line(flat_idx, "", "", "", self._pal["value"],
                               (self.PAD, self.PAD), 0)
        self._refresh_details()
        if self._details is not None:
            self._poll_translation()
        self._show(True)
        self._apply_lock()
        # 동기화 스레드가 떠 볼 자리 — 표시 뒤에 읽는다(숨은 Tk 창은 옛 좌표를
        # 돌려준다). 캡처 오버레이 중에는 어두워진 화면을 배경으로 삼지 않게
        # 비워 둔다
        self._camo_geom = None if snip else (
            self.root.winfo_x(), self.root.winfo_y(), self._fix_w, self._fix_h)
        if self._ticks % self.CAMO_EVERY == 0:
            self._health_check()

    def _columns(self, lines, base=0):
        """세 줄이 같은 열에 서도록 (값 오른쪽끝 x, 시간 시작 x)를 구한다."""
        f = self._font
        label_w = max([self._label_block(row) for row in lines] or [0])
        value_w = max([f.measure(row[1]) for row in lines if row[1]]
                      or [f.measure("999회")])
        end = base + self.PAD + label_w + self._col_gap + value_w
        return (end, end)

    def _set_line(self, idx, label, value, when, vcolor, cols, base,
                  lcolor=None, tag=""):
        """실제로 달라졌을 때만 캔버스 텍스트를 다시 그린다."""
        key = (label, value, when, vcolor, lcolor, cols, base, tag,
               id(self._pal))
        if self._last[idx] == key:
            return
        self._last[idx] = key
        l, v, w, tg = self.items[idx]
        self.cv.itemconfigure(l, text=label, fill=lcolor or self._pal["label"])
        self.cv.itemconfigure(v, text=value, fill=vcolor)
        self.cv.itemconfigure(w, text=when, fill=self._pal["time"])
        self.cv.itemconfigure(tg, text=tag, fill=lcolor or self._pal["label"])
        y = self._ys[idx % self.LINES]
        self.cv.coords(l, base + self.PAD, y)
        self.cv.coords(tg,
                       base + self.PAD + self._font.measure(label) + self._px(4),
                       y)
        self.cv.coords(v, cols[0], y)      # 값은 오른쪽 정렬 — 끝이 맞는다
        self.cv.coords(w, cols[1], y)

    def _note_painted(self, blue):
        """칠한 배경색의 파랑 채널을 남긴다 — 동결 판정의 기준점.

        비슷한 색은 합치고(±4, 위장색 자체의 무시 폭과 같다) 24개에서 멈춘다.
        가득 차면 새 색을 버리고 옛 색을 남긴다 — 동결은 시작 직후에 생기고
        (2026-08-27 실측) 그때 화면에 굳은 색이 기록에서 가장 오래된 축이다.
        """
        if any(abs(blue - b) <= 4 for b in self._painted):
            return
        if len(self._painted) < 24:
            self._painted.append(blue)

    def _bar_contrast(self, x, y, w, h):
        """바 영역의 밝기 폭 (가장 어두운 값, 가장 밝은 값).

        글자가 그려져 있으면 폭이 크고(실측 31~225), 동결돼 단색만 남으면
        0에 가깝다. PIL의 ImageGrab은 쓰지 않는다 — bbox를 줘도 화면 전체를
        뜬 뒤 잘라내서(실측: 840px나 370만px이나 똑같이 26.5ms, CPU 12.5ms)
        모니터가 크고 많을수록 비싸진다. 바 영역만 뜨면 CPU 0.4ms로 끝난다.
        """
        raw = blit_bgra(x, y, w, h)
        if raw is None:
            return None
        # 한 채널만 잘라 C 속도로 min/max — 알파는 고정값이라 건너뛴다
        chan = raw[0::4]
        return (min(chan), max(chan)) if chan else None

    def _health_check(self):
        """동결·배율 변화 감시 — 걸리면 바 창을 버리고 새로 만든다.

        2026-08-27 실측: 시작 직후 숨김/복귀가 겹친 뒤 Tk가 이 창에 대한
        그리기를 화면에 전혀 반영하지 않는 상태가 생겼다 — 캔버스는 최초
        단색 배경으로 얼고, 외부에서 RedrawWindow를 보내도 깨어나지 않았다
        (프로세스 재시작만 유효). 원인이 Tk/DWM 내부라 직접 풀 수 없어,
        겉으로 드러나는 증상(글자를 그렸는데 바 영역이 균일한 단색)을 10초
        간격 2회 연속으로 확인하면 창을 재생성한다. 모니터 배율(DPI)
        변화도 같은 경로로 창을 다시 만든다.

        위치 비교(Tk가 아는 x vs 실제 x)는 쓰지 않는다 — `winfo_x()`는
        Tk가 요청한 값이 아니라 Windows의 실제 값을 되읽어오므로 둘은 항상
        같다(실측). 동결을 가려내지 못하는 검사다.
        """
        try:
            u = ctypes.windll.user32
            hwnd = self._hwnd
            if not hwnd or not self._shown:
                return
            # 창을 만든 직후 1분은 판정하지 않는다 — 로그인 직후에는 바탕이
            # 아직 안 그려져 화면 읽기가 통째로 검게 나온다(실측 2026-08-27:
            # 재부팅 30초 뒤 멀쩡한 바를 동결로 오판해 재생성했다).
            # 재생성 뒤에도 _ticks가 0부터라 새 창에도 같은 유예가 걸린다.
            if self._ticks < 120:
                return
            try:
                dpi = u.GetDpiForWindow(ctypes.c_void_p(hwnd))
            except Exception:
                dpi = 0                 # Win10 1607 미만 — 배율 감시만 포기
            if dpi and abs(dpi / 96.0 - self._scale) > 0.01:
                log.info("bar dpi %d -> %d - rebuilding",
                         round(self._scale * 96), dpi)
                self._rebuild = True
                return
            if not any(k and k[0] for k in self._last if k):
                return                  # 그려 둔 글자가 없다 — 판정 보류
            r = ctypes.wintypes.RECT()
            u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
            span = self._bar_contrast(r.left, r.top,
                                      r.right - r.left, r.bottom - r.top)
            if span is None:
                return
            stuck = span[1] - span[0] < 24
            # 진짜 동결이면 우리가 칠해 둔 배경색이 그대로 남아 보인다.
            # 전혀 다른 색(대개 검정)이면 동결이 아니라 화면을 못 읽은 것 —
            # 그걸 세면 멀쩡한 바를 부순다.
            # 기준은 '마지막으로 요청한 색'이 아니라 '이 창에 칠한 색 전부'다.
            # 동결은 화면을 옛 색에 굳혀 두는데 위장색은 그 뒤로도 계속 새로
            # 잡히므로, 마지막 색과만 견주면 동결일수록 어긋나 영영 재생성되지
            # 않는다 (2026-09-08 실측: 화면은 #e3edf8에 굳은 채 요청색만
            # #dcdcdc로 바뀌어, 워치독이 내내 "not frozen"만 찍었다).
            if stuck and not any(abs(span[1] - b) <= 24
                                 for b in self._painted):
                log.info("screen unreadable (flat %d, painted %s) - not frozen",
                         span[1], self._painted)
                self._strikes = 0
                return
            self._strikes = self._strikes + 1 if stuck else 0
            if self._strikes >= 3:      # 10초 간격 3연속 — 오탐 여유를 둔다
                log.info("bar frozen (contrast %d) - rebuilding",
                         span[1] - span[0])
                self._rebuild = True
        except Exception:
            log.exception("health check failed")

    def _log_hide(self, who, fullscreen=None):
        """바가 숨는 순간의 판단 근거 — 캡처류 오탐이 재발하면 이 줄로 원인을 본다."""
        try:
            fg, top = _foreground_pair()
            log.info(
                "bar hide (%s): fs=%s cover=%d tray_top=%s fs_fg=%s "
                "snip_age=%.1fs fg=%s/%s", who, fullscreen, self._covered,
                _tray_topmost(), _fullscreen_foreground(),
                min(time.time() - _last_snip_at, 99999.0),
                _window_exe(fg) if fg else "", _window_exe(top) if top else "")
        except Exception:
            pass

    def _win_show(self, on):
        """표시·숨김은 Win32로 직접 한다 — Tk의 deiconify/withdraw를 안 쓴다.

        withdraw로 숨긴 뒤의 deiconify는 Tk가 상태를 'normal'로 알고 있으면
        무시될 수 있다. SW_SHOWNOACTIVATE는 활성화도 하지 않아 전체화면 앱에서
        포커스를 뺏지 않는다. 핸들은 틱에서 캐시해 둔 것을 쓴다(콜백 경로가
        Tk를 못 부르기 때문 — `_on_win_event` 참고).
        """
        u = ctypes.windll.user32
        if self._hwnd:
            u.ShowWindow(ctypes.c_void_p(self._hwnd), 4 if on else 0)

    def _deiconify_in_place(self):
        """Tk deiconify — 단, 창이 저장된 자리에서 나타나게 한다.

        Tk는 숨어 있는 동안의 위치 요청을 무시하고, 다시 매핑할 때 창(HWND)을
        새로 만들며 마지막으로 '보이던' 좌표를 쓴다(tkWinWm.c UpdateWrapper).
        한 번도 보인 적이 없거나 오래 숨어 있었으면 그 좌표가 스테일이라 —
        부팅 직후엔 0,0 — 바가 화면 왼쪽 위에 잠깐 떴다가 다음 idle에야
        제자리로 온다(실측: 매 부팅). 그래서 deiconify 직후 새 HWND를 곧바로
        앵커 좌표로 옮기고 idle까지 돌려, 첫 프레임부터 제자리에 있게 한다.
        새 HWND는 훅·표시/숨김이 쓰는 캐시에도 바로 반영한다 — 옛 핸들로는
        ShowWindow가 조용히 실패해 한 틱(0.5초) 동안 명령이 증발했다.
        """
        x, y = self._anchor_xy()
        self.root.geometry(f"+{x}+{y}")
        self.root.deiconify()
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            self._hwnd = hwnd
            u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(0), x, y,
                           0, 0, 0x0015)      # NOSIZE|NOZORDER|NOACTIVATE
        except Exception:
            log.exception("deiconify reposition failed")
        self.root.update_idletasks()

    def _show(self, on):
        """상태가 바뀔 때만 표시/숨김 — 매 틱 재표시로 인한 깜빡임 방지."""
        if on and not self._shown:
            if not self._mapped:
                self._deiconify_in_place()  # Tk 상태를 normal로 만드는 건 한 번만
                self._mapped = True
            else:
                try:
                    if self.root.state() != "normal":
                        # Win32 숨김을 Tk가 '내려감'으로 기록한 채 남으면 이후
                        # 그리기·이동이 전부 무시된다(2026-08-27 동결 실측) —
                        # 표시할 때마다 상태가 어긋나 있으면 되살린다
                        self._deiconify_in_place()
                except Exception:
                    pass
            self._win_show(True)        # 먼저 띄운다 — 배경 촬영이 복귀를 늦추면 안 된다
            self._adopt_by_taskbar()    # 표시 후에 걸어야 Tk가 안 지운다
            self._sync_topmost(raise_now=True)
            # 숨어 있는 동안 아래가 바뀌었을 수 있으니(테마·아이콘) 곧바로 맞춘다.
            # 바 양옆을 뜨는 방식이라 바가 보이는 채로 찍어도 된다(v2.16).
            self._match_background(force=True)
        elif not on and self._shown:
            self._camo_geom = None      # 숨은 동안은 배경을 떠 보지 않는다
            self._win_show(False)
        elif on and self._ticks % self.ADOPT_EVERY == 0:
            self._adopt_by_taskbar()    # 연결이 풀린 경우를 위한 드문 보험
            self._sync_topmost(raise_now=True)
        self._shown = on


# ---------------------------------------------------------------- 앱
class TrayApp:
    def __init__(self):
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.wake = threading.Event()
        self.force_api = threading.Event()
        self.rows = []
        self.source = None      # "api" | "hook"
        self.updated_at = None
        self.status = "불러오는 중…"
        self.auth_notice = None     # 토큰 만료 시 플로팅 바에 띄울 문구
        # (새 버전, 패치노트(CHANGELOG 형식), {자산: url}, {자산: sha256})
        # — 있으면 바의 업데이트 패널·메뉴에 뜬다
        self.update_info = None
        self.update_error = None    # 마지막 설치 실패 사유 — 업데이트 창이 보여준다
        self.update_checked = False     # 새 버전 확인을 한 번이라도 마쳤나
        self._updating = False
        # (예약 작업이 있나, 로그온 시 켜지나) — 메뉴가 열릴 때마다 schtasks를
        # 부르지 않게 시작할 때와 토글 뒤에만 읽어 둔다
        self.autostart = (False, startup_installed())
        self._headlines = {}        # 버전 → 한 줄 요약 (바가 0.5초마다 묻는다)
        self.icon = None
        self.cfg = load_config()
        self.skill_tracker = TrackerService()
        self.codex_usage = None         # 워커가 쓰고 Tk 틱이 읽는다
        self.notifications = NotificationService()
        self.notes_requested = threading.Event()
        self.details_requested = threading.Event()
        self.whats_new_requested = threading.Event()
        if os.environ.get("SKILL_WIDGET_SHOW_DETAILS") == "1":
            self.details_requested.set()
        if os.environ.get("SKILL_WIDGET_SHOW_ALERTS") == "1":
            self.notes_requested.set()
        if os.environ.get("SKILL_WIDGET_SHOW_WHATSNEW") == "1":
            self.whats_new_requested.set()
        self._fresh_install = self._note_version()

        self._load_file(initial=True)   # 켜자마자 마지막 값 표시
        if not self.rows:
            self._load_cache()          # 훅 데이터가 없으면 지난 실행의 API 값

    def _note_version(self):
        """이번 실행이 업데이트 직후인지 기록한다. 새로 설치된 거면 True.

        업데이트(자동이든 수동이든) 뒤 첫 실행이면 cfg["whats_new"]에
        (이전 → 지금) 버전을 남긴다 — 바에 '업데이트' 패널이 뜨고, 누르면
        그 사이 패치노트를 보여준다. 설정이 아예 없으면 첫 설치라 남기지 않는다.
        평문으로 남아 있던 장수 토큰도 이때 한 번 DPAPI로 다시 저장된다.
        """
        prev = self.cfg.get("last_run_version")
        fresh = not self.cfg
        if prev == __version__:
            if self.cfg.get("setup_token"):
                save_config(self.cfg)   # 옛 평문 토큰 → DPAPI (멱등)
            return False
        if not fresh and (prev is None
                          or _ver_tuple(prev) < _ver_tuple(__version__)):
            start = prev
            pending = self.cfg.get("whats_new")
            if pending and pending.get("to") == prev:
                # 지난 업데이트 소식을 아직 안 봤다 — 덮어쓰지 않고 범위를
                # 이어 붙인다(예: 3.17.2 → 3.18.1을 안 연 채 3.18.2가 오면
                # 3.18.0부터 전부 보여야 한다)
                start = pending.get("from")
            self.cfg["whats_new"] = {"from": start, "to": __version__,
                                     "at": time.time()}
            log.info("updated %s -> %s - whats-new pending",
                     start or "?", __version__)
        self.cfg["last_run_version"] = __version__
        save_config(self.cfg)
        return fresh

    def range_headline(self, lo, hi, notes=None):
        """(lo, hi] 사이 패치노트의 한 줄 요약 — 알림과 바 패널이 쓴다.

        여러 버전을 한꺼번에 건너뛰었으면 불릿이 가장 많은(가장 큰 변경이
        담긴) 버전의 요약을 쓴다. 최신 버전만 보면 작은 수정 한 줄이
        큰 릴리스를 가려 버린다(3.17.2 → 3.18.1에서 실제로 그랬다).
        """
        key = (lo, hi, notes is None)
        if key not in self._headlines:
            text = notes if notes is not None else local_changelog()
            span = [e for e in changelog_entries(text) if lo < e["t"] <= hi]
            best = max(span, key=lambda e: (len(e["items"]), e["t"]),
                       default=None)
            self._headlines[key] = headline(best["items"]) if best else ""
        return self._headlines[key]

    def whats_new_range(self):
        """업데이트 소식이 다루는 (이전, 지금) 버전 튜플 — 기록이 없던
        버전에서 올라왔으면 이전은 UNTRACKED_UNTIL."""
        wn = self.cfg.get("whats_new") or {}
        return (_ver_tuple(wn.get("from") or UNTRACKED_UNTIL),
                _ver_tuple(wn.get("to") or __version__))

    def version_line(self):
        """트레이 메뉴의 버전 줄 — 바의 '업데이트 완료 vX'만으로는 그게 지금
        버전인지 새 버전인지 헷갈린다는 지적이 있었다(2026-09-23)."""
        if self.update_info:
            return (f"현재 버전 v{__version__} · "
                    f"새 버전 v{self.update_info[0]} 있음")
        if self.update_checked:
            return f"현재 버전 v{__version__} · 최신"
        return f"현재 버전 v{__version__}"

    def _refresh_autostart(self):
        """자동 실행 상태를 다시 읽는다. 예약 작업이 있으면 그 로그온
        트리거가 기준이고, 옛 토글이 남긴 시작프로그램 vbs는 중복 실행을
        막으려고 치운다."""
        has_task, on = task_autostart()
        if has_task:
            if startup_installed():
                uninstall_startup()
                log.info("legacy startup vbs removed (task handles autostart)")
            self.autostart = (True, on)
        else:
            self.autostart = (False, startup_installed())
        self.q.put(("menu",))

    def _toggle_autostart(self):
        """메뉴의 'Windows 시작 시 자동 실행' — 별도 스레드 (PowerShell이 느리다)."""
        has_task, on = self.autostart
        try:
            if has_task:
                set_task_autostart(not on)
            elif on:
                uninstall_startup()
            else:
                install_startup()
            log.info("autostart -> %s (%s)", not on,
                     "task" if has_task else "vbs")
        except Exception as e:
            log.warning("autostart toggle failed: %s", e)
            if self.icon:
                self.icon.notify(f"자동 실행 설정을 바꾸지 못했습니다 — {e}",
                                 "Claude 위젯")
        self._refresh_autostart()

    def where_to_look(self):
        """토스트가 가리킬 곳 — 잠긴 바는 클릭이 통과하므로 트레이 메뉴를 댄다."""
        if self.cfg.get("bar_locked") or not self.cfg.get("bar_visible", True):
            return "트레이 메뉴 '업데이트 소식 보기'에서"
        return "바의 '업데이트' 패널을 누르면"

    def update_method(self):
        """'exe'(릴리스 EXE로 교체) · 'git'(개발 폴더 — git pull 안내) · 'zip'."""
        if getattr(sys, "frozen", False):
            return "exe"
        here = os.path.dirname(os.path.abspath(__file__))
        return "git" if os.path.isdir(os.path.join(here, ".git")) else "zip"

    def whats_new_view(self):
        """업데이트 창에 그릴 것 — (모드, 부제, 항목들).

        모드: 'available'(설치 대기 — [지금 설치]) · 'installed'(방금 업데이트
        됨) · 'history'(메뉴로 연 최근 변경 내용).
        """
        cur = _ver_tuple(__version__)
        info = self.update_info
        if info:
            entries = [e for e in changelog_entries(info[1]) if e["t"] > cur]
            return ("available", f"새 버전 v{info[0]} · 지금 v{__version__}",
                    entries)
        local = changelog_entries(local_changelog())
        wn = self.cfg.get("whats_new")
        if wn:
            lo, hi = self.whats_new_range()
            entries = [e for e in local if lo < e["t"] <= hi]
            sub = (f"v{wn['from']} → v{wn['to']} 업데이트 완료"
                   if wn.get("from") else f"v{wn['to']} 업데이트 완료")
            return "installed", sub, entries
        return ("history", f"최근 변경 내용 · 지금 v{__version__}",
                [e for e in local if e["t"] <= cur][:6])

    def mark_whats_new_seen(self, mode):
        """창을 열었다 = 봤다. 바의 업데이트 패널이 다음 틱에 내려간다."""
        if mode == "installed" and self.cfg.pop("whats_new", None):
            save_config(self.cfg)
        elif mode == "available" and self.update_info and \
                self.cfg.get("update_seen") != self.update_info[0]:
            self.cfg["update_seen"] = self.update_info[0]
            save_config(self.cfg)

    # ---------------- 데이터
    def _load_file(self, initial=False):
        try:
            mtime = os.path.getmtime(USAGE_FILE)
        except OSError:
            return None
        try:
            with open(USAGE_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        rows = rows_from_windows(d.get("rate_limits") or {})
        if not rows:
            return None
        ts = d.get("written_at", mtime)
        if initial:
            self.rows, self.source, self.updated_at = rows, "hook", ts
            self.status = None
        return (rows, ts)

    def _load_cache(self):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            rows = [(r[0], float(r[1]), r[2]) for r in d["rows"]]
            ts = float(d["updated_at"])
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            return
        if rows and time.time() - ts < CACHE_MAX_AGE:
            self.rows, self.source, self.updated_at = rows, "cache", ts
            self.status = None

    def _adopt_setup_token(self):
        """credentials에 진짜 장수 토큰이 보일 때만 위젯 설정에 자동 등록.

        sk-ant-oat 접두사는 8시간짜리 일반 액세스 토큰도 똑같이 쓴다
        (2026-07-24 실측 — 접두사만 보고 채택한 토큰이 8시간마다 죽었음).
        만료가 30일 이상 남은 것만 장수 토큰으로 인정한다.
        """
        try:
            with open(CRED_PATH, encoding="utf-8") as f:
                o = json.load(f).get("claudeAiOauth") or {}
        except (OSError, json.JSONDecodeError):
            return False
        tok = o.get("accessToken") or ""
        far = (time.time() + 30 * 86400) * 1000
        if tok.startswith("sk-ant-oat") and (o.get("expiresAt") or 0) > far \
                and tok != self.cfg.get("setup_token"):
            self.cfg["setup_token"] = tok
            save_config(self.cfg)
            log.info("setup token auto-registered (len %d)", len(tok))
            return True
        return False

    def _refresh_test(self):
        """리프레시 체인이 살아 있는지 지금 확인 — 토큰이 멀쩡할 때 강제 갱신.

        성공하면 위젯을 상시 실행해 체인을 이어갈 수 있다는 뜻이고,
        실패하면 이 계정에서 갱신 경로 자체가 죽은 것이라 다른 방법이 필요하다.
        """
        try:
            get_access_token(force_refresh=True)
            log.info("refresh test: OK - chain alive")
            note = "토큰 갱신 성공 — 자동 유지 가능"
        except Exception as e:
            log.info("refresh test: FAILED - %s", e)
            note = f"토큰 갱신 실패 — {e}"
        try:
            self.icon.notify(note, "Claude 사용량")
        except Exception:
            pass
        self.force_api.set()
        self.wake.set()

    def _save_cache(self):
        try:
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rows": self.rows, "updated_at": self.updated_at}, f)
            os.replace(tmp, CACHE_PATH)
        except OSError:
            pass

    # ---------------- 업데이트
    def _check_update(self):
        """새 버전 확인. EXE 배포본은 릴리스에서 받아 스스로 교체하고(기본,
        트레이 토글로 끌 수 있다), 소스 실행은 예전처럼 알림 + 메뉴 항목만."""
        cur = _ver_tuple(__version__)
        if getattr(sys, "frozen", False):
            # 시작 직후엔 구 부트로더가 제 EXE 잠금을 늦게 놓아 .old 삭제가
            # 실패할 수 있다(실측 3초+) — 30초 뒤 첫 확인 때 한 번 더 치운다
            finish_exe_update()
            rel = fetch_latest_release()
            if not rel:
                return
            ver_t, latest, body, assets, digests = rel
            if ver_t <= cur or not assets.get(WIDGET_ASSET):
                self.update_info = None
                self.update_error = None
                self.update_checked = True
                return
            log.info("update available: v%s (current v%s)",
                     latest, __version__)
            # 여러 버전을 건너뛰었으면 그 사이 패치노트를 다 모은다 —
            # 못 모으면 최신 릴리스 본문 하나로 대신한다
            try:
                notes = fetch_release_notes(cur, ver_t) or body
            except Exception as e:
                log.info("release notes unavailable (%s) - latest only", e)
                notes = body
            self.update_info = (latest, notes, assets, digests)
            head = self.range_headline(cur, ver_t, notes)
            if self.cfg.get("auto_update", True):
                if self._updating:
                    return
                self._updating = True
                self.update_error = None
                try:
                    if self.icon:
                        self.icon.notify(
                            f"새 버전 v{latest} 설치 중 — 잠시 후 재시작합니다"
                            + (f"\n{head}" if head else ""),
                            "Claude 위젯 업데이트")
                    self._install_release(latest, assets, digests)
                    return
                except Exception as e:
                    # 자동 설치 실패 — 바의 업데이트 패널에서 다시 시도하게 한다
                    log.exception("auto update failed")
                    self._updating = False
                    self.update_error = str(e)
                    self.cfg.pop("update_seen", None)   # 패널을 다시 띄운다
                    save_config(self.cfg)
                    if self.icon:
                        self.icon.notify(
                            f"v{latest} 자동 설치 실패 — {self.where_to_look()} "
                            "다시 시도할 수 있어요",
                            "Claude 위젯 업데이트")
                    return
        else:
            text = fetch_changelog()
            entries = changelog_entries(text)
            if not entries:
                return
            if entries[0]["t"] <= cur:
                self.update_info = None
                self.update_checked = True
                return
            latest = entries[0]["v"]
            # 개발 실행은 원격 CHANGELOG에서 (지금, 최신] 절만 잘라 쓴다
            keep = [m for m in re.finditer(r"(?ms)^## v.+?(?=^## v|\Z)", text)
                    if _ver_tuple(re.match(r"## v?([\d.]+)",
                                           m.group(0)).group(1)) > cur]
            notes = "\n".join(m.group(0).strip() + "\n" for m in keep)
            self.update_info = (latest, notes, None, None)
            head = self.range_headline(cur, entries[0]["t"], notes)
        log.info("update ready: v%s (current v%s)", latest, __version__)
        if self.cfg.get("notified_version") != latest:
            self.cfg["notified_version"] = latest
            save_config(self.cfg)
            if self.icon:
                self.icon.notify(
                    f"새 버전 v{latest}" + (f" — {head}" if head else "")
                    + f"\n{self.where_to_look()} 바뀐 점을 보고 설치할 수 있어요",
                    "Claude 위젯 업데이트")
                log.info("update toast shown: v%s", latest)

    def _install_release(self, ver, assets, digests=None):
        """릴리스의 새 EXE를 받아 제자리 교체 후 재시작.

        실행 중인 EXE는 덮어쓸 수 없지만 이름 바꾸기는 되므로,
        새 EXE를 .new 로 다 받아 검증한 뒤 [현재 → .old, .new → 제자리]
        순서로 바꾼다. 두 번째 rename이 실패하면 첫 번째를 되돌려
        반쯤 바뀐 채 끝나지 않게 한다. .old 는 다음 시작이 지운다
        (finish_exe_update — 새 버전이 못 떴으면 수동 복구용으로 남는다).
        받은 파일은 GitHub가 주는 SHA-256과 맞아야만 교체에 쓴다.
        """
        import subprocess
        digests = digests or {}
        exe = sys.executable
        new, old = exe + ".new", exe + ".old"
        download_file(assets[WIDGET_ASSET], new, digests.get(WIDGET_ASSET))
        check_exe(new)
        hook = os.path.join(os.path.dirname(exe), HOOK_ASSET)
        hurl = assets.get(HOOK_ASSET)
        if hurl and os.path.exists(hook):
            try:
                download_file(hurl, hook + ".new", digests.get(HOOK_ASSET))
                check_exe(hook + ".new")
                os.replace(hook + ".new", hook)  # 순간 실행이라 대개 안 잠겨 있다
            except (OSError, RuntimeError):
                pass        # 잠겼으면 .new 가 남고, 다음 시작 때 마저 바꾼다
        try:
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            pass
        rename_retry(exe, old)
        try:
            rename_retry(new, exe)
        except OSError:
            rename_retry(old, exe)      # 되돌린다
            raise
        log.info("update installed: v%s (exe swap)", ver)
        # 잠시 뒤 새 인스턴스를 띄운다. 새 인스턴스는 전임이 포트를 놓을
        # 때까지 스스로 기다리므로(acquire_singleton) 여기 지연은 짧아도 된다.
        # 예약 작업을 경유해야 다시 그 작업의 인스턴스가 된다 — EXE를 직접
        # 띄우면 작업은 Ready로 남아, 이후 Claude 세션이 시작될 때마다 훅의
        # `schtasks /run`이 새 프로세스를 진짜로 만들어 냈다(작업 인스턴스일
        # 때는 IgnoreNew 정책이 그 호출을 그냥 무시한다). 작업이 없으면
        # (수동 실행 설치) 전처럼 EXE를 직접 띄운다.
        subprocess.Popen(
            f'cmd /c ping -n 2 127.0.0.1 >nul & '
            f'(schtasks /run /tn "{STARTUP_TASK}" >nul 2>&1 || start "" "{exe}")',
            creationflags=0x08000008)   # DETACHED | CREATE_NO_WINDOW
        self.q.put(("quit",))

    def _do_update(self):
        """업데이트 창의 [지금 설치] — 교체 후 재시작한다. (별도 스레드)

        패치노트는 창이 이미 보여줬고, 누른 것이 곧 확인이다. EXE 배포본은
        릴리스의 새 EXE로, 소스 실행은 저장소 zip으로 바꾼다(개발 폴더는
        창이 git pull을 안내하고 여기까지 오지 않는다). 실패하면 사유를
        update_error에 남겨 창이 보여주고, 다시 시도할 수 있게 둔다.
        """
        import shutil
        import subprocess
        import tempfile
        info = self.update_info
        if not info or self._updating or self.update_method() == "git":
            return
        ver = info[0]
        self._updating = True
        self.update_error = None
        if getattr(sys, "frozen", False):
            try:
                self._install_release(ver, info[2] or {}, info[3] or {})
            except Exception as e:
                log.exception("update failed")
                self._updating = False
                self.update_error = str(e)
            return
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            src = download_repo(tempfile.mkdtemp(prefix="ctw-update-"))
            shutil.copytree(src, here, dirs_exist_ok=True)
            self._update_hooks(src)
            log.info("update installed: v%s", ver)
            vbs = os.path.join(here, "run-widget.vbs")
            # 구 인스턴스가 싱글턴 포트를 놓은 뒤(약 3초) 새 인스턴스를 띄운다
            subprocess.Popen(
                f'cmd /c ping -n 4 127.0.0.1 >nul & wscript "{vbs}"',
                creationflags=0x08000008)   # DETACHED | CREATE_NO_WINDOW
            self.q.put(("quit",))
        except Exception as e:
            log.exception("update failed")
            self._updating = False
            self.update_error = str(e)

    def _update_hooks(self, src):
        """설치돼 있는 ~/.claude 훅 사본도 새 버전으로 갱신 (없으면 건너뜀)."""
        import shutil
        claude_dir = os.path.join(HOME, ".claude")
        try:
            tgt = os.path.join(claude_dir, "start-usage-widget.py")
            if os.path.exists(tgt):
                with open(os.path.join(src, "hooks", "start-usage-widget.py"),
                          encoding="utf-8") as f:
                    txt = f.read().replace("__WIDGET_PATH__",
                                           os.path.abspath(__file__))
                with open(tgt, "w", encoding="utf-8") as f:
                    f.write(txt)
            tgt = os.path.join(claude_dir, "usage-hook.py")
            if os.path.exists(tgt):
                shutil.copyfile(os.path.join(src, "hooks", "usage-hook.py"),
                                tgt)
        except OSError as e:
            log.warning("hook update skipped: %s", e)

    def _poll_loop(self):
        last_mtime = None
        last_cred = None
        next_api = 0.0
        next_upd = time.time() + 30     # 시작 직후 부하를 피해 30초 뒤 첫 확인
        throttle_until = 0.0
        throttle_streak = 0
        # 서버가 허용하는 간격을 배워 둔 값. 429로 끊기면 두 배, 연달아
        # 성공하면 조금씩 줄인다 — 60초 고정이던 때는 "성공 1번 → 429 6~7번"이
        # 되풀이돼 호출의 대부분이 헛걸음이었다(2026-09-23 로그: 한 시간
        # 성공 2 / 429 13).
        api_gap = API_INTERVAL_ACTIVE
        last_ts = None
        last_attempt = 0.0
        api_denied_reason = None
        api_ok = False
        while not self.stop_evt.is_set():
            now = time.time()
            try:
                if self.force_api.is_set():
                    self.force_api.clear()
                    next_api = 0.0
                try:
                    cred = os.path.getmtime(CRED_PATH)
                except OSError:
                    cred = None
                if last_cred is None:
                    last_cred = cred
                elif cred != last_cred:
                    last_cred = cred
                    next_api = 0.0
                    log.info("credentials changed - retry api now")

                if now >= next_upd:
                    next_upd = now + UPDATE_CHECK_SEC
                    try:
                        self._check_update()
                    except Exception as e:
                        log.info("update check failed: %s", e)
                    self.q.put(("menu",))   # 메뉴의 버전 줄(최신·새 버전)을 곧바로

                # 전사 파일 갱신 = 방금 답변이 끝남 → 사용량이 변한 순간
                ts_m = latest_transcript_mtime()
                active = bool(ts_m) and (now - ts_m) < ACTIVE_WINDOW
                if last_ts is None:
                    last_ts = ts_m
                elif ts_m != last_ts:
                    last_ts = ts_m
                    # 인증이 죽은 상태의 이벤트 재시도는 헛 호출만 쌓는다 —
                    # 재로그인은 credentials 변경 감지가 즉시 잡는다
                    if now >= throttle_until and not api_denied_reason and \
                            now - last_attempt >= max(EVENT_MIN_GAP, api_gap):
                        next_api = min(next_api, now)

                if now >= next_api:
                    last_attempt = now
                    try:
                        data = fetch_usage_api(self.cfg)
                        rows = rows_from_limits(data.get("limits")) \
                            or rows_from_windows(data)
                        if rows:
                            self.q.put(("data", rows, "api", now))
                            api_denied_reason = None
                            throttle_streak = 0
                            throttle_until = 0.0
                            if self.auth_notice:
                                self.auth_notice = None
                                log.info("auth notice cleared")
                            if not api_ok:
                                api_ok = True
                                log.info("api ok: %d rows", len(rows))
                            if not self.cfg.get("setup_token"):
                                self._adopt_setup_token()
                        next_api = now + (api_gap if active else
                                          max(API_INTERVAL_IDLE, api_gap))
                        api_gap = max(API_INTERVAL_ACTIVE, int(api_gap * 0.85))
                    except ApiThrottled as e:
                        api_ok = False
                        throttle_streak += 1
                        if throttle_streak == 1:    # 한 번 끊길 때마다 한 번만
                            api_gap = min(api_gap * 2, API_INTERVAL_IDLE)
                            log.info("api pacing -> %ds", api_gap)
                        if e.retry_after:
                            # 서버가 명시한 대기는 이벤트 재시도도 존중 —
                            # 그 전에 찌르면 잠금 창만 계속 연장된다
                            wait = min(e.retry_after, 600)
                            throttle_until = now + wait
                        else:
                            # 짧은 스로틀은 30초 재시도로 즉시 회복하고,
                            # 오래 가는 스로틀은 한도를 더 갉지 않게 점점 늦춘다.
                            wait = min(30 * 2 ** ((throttle_streak - 1) // 4), 300)
                            throttle_until = now + wait if throttle_streak > 4 \
                                else 0.0
                        next_api = now + wait
                        log.info("api throttled (429) x%d - retry in %ds",
                                 throttle_streak, int(wait))
                    except ApiDenied as e:
                        api_denied_reason = str(e)
                        api_ok = False
                        next_api = now + API_INTERVAL_DENIED
                        log.info("api denied: %s", e)
                        if "설정 토큰" in api_denied_reason and \
                                self._adopt_setup_token():
                            next_api = 0.0  # 새로 발급된 장수 토큰으로 즉시 재시도
                        elif ("토큰 갱신" in api_denied_reason
                              or "리프레시 토큰" in api_denied_reason
                              or "인증" in api_denied_reason):
                            # 리프레시 체인까지 끊긴 상태 — 재로그인만이 답
                            n = "재로그인 필요 · 트레이 메뉴 클릭"
                            if n != self.auth_notice:
                                self.auth_notice = n
                                log.info("auth notice: %s", n)
                        # 조직 정책(403) 등 재로그인으로 못 푸는 경우는 문구 없음
                    except Exception as e:
                        api_ok = False
                        next_api = now + API_INTERVAL_ACTIVE
                        log.warning("api error: %s", e)

                try:
                    mtime = os.path.getmtime(USAGE_FILE)
                except OSError:
                    mtime = None
                if mtime and mtime != last_mtime:
                    first = last_mtime is None
                    last_mtime = mtime
                    got = self._load_file()
                    if got:
                        self.q.put(("data", got[0], "hook", got[1]))
                    if not first and now >= throttle_until \
                            and not api_denied_reason \
                            and (api_gap <= API_INTERVAL_ACTIVE
                                 or now - last_attempt >= api_gap):
                        # 답변 직후 = 사용량이 막 변한 시점, 즉시 재조회
                        # (서버가 간격을 요구하는 동안은 그 간격을 지킨다)
                        next_api = min(next_api, now)

                if not self.rows:
                    if api_denied_reason and "설정 토큰" in api_denied_reason:
                        st = "설정 토큰 거부(403) — 조직 설정 확인"
                    elif api_denied_reason and ("토큰 갱신" in api_denied_reason
                                                or "리프레시 토큰" in api_denied_reason
                                                or "인증" in api_denied_reason):
                        st = "재로그인 필요 — 메뉴에서 '재로그인' 클릭"
                    elif api_denied_reason:
                        st = "대기 중 — 훅 설정 확인 필요"
                    else:
                        st = "불러오는 중…"
                    self.q.put(("status", st))
            except Exception:
                log.exception("poll error")
            self.wake.wait(POLL_SEC)
            self.wake.clear()

    def _skill_loop(self):
        first = True
        ticks = 0
        while not self.stop_evt.is_set():
            try:
                self.skill_tracker.refresh(force=first)
            except Exception:
                log.exception("skill tracker refresh failed")
            report = self.skill_tracker.repair_report
            if report is not None and self.icon:
                self.skill_tracker.repair_report = None
                try:
                    self.icon.notify(
                        "스킬 기록 파일이 손상돼 자동으로 복구했습니다 — "
                        f"기록 {report.get('events', 0)}건 보존, 원본은 "
                        "로그 폴더에 .corrupt 사본으로 남겨 뒀어요",
                        "Claude 위젯")
                except Exception:
                    pass
            if first or ticks % 30 == 0:    # Codex 사용량은 60초마다
                try:
                    # 바에 패널이 뜨는 조건과 같게, 실행 중일 때만 조회한다
                    snap = None
                    if "codex" in self.skill_tracker.snapshot()[0]:
                        snap = codex_usage_api()
                    self.codex_usage = codex_merge(snap
                                                   or codex_rate_snapshot())
                except Exception:
                    log.exception("codex usage scan failed")
            first = False
            ticks += 1
            self.stop_evt.wait(2)

    # ---------------- 트레이
    def _menu_lines(self):
        import pystray
        items = []
        if self.rows:
            for label, pct, reset in self.rows:
                phrase = reset_phrase(reset)
                shown = (100 - pct if self.cfg.get("usage_remaining")
                         else pct)
                text = f"{label}   {round(shown)}%"
                if phrase:
                    text += f"   ·  {phrase}"
                items.append(pystray.MenuItem(text, None, enabled=False))
            if self.updated_at:
                age = time.time() - self.updated_at
                when = time.strftime("%H:%M", time.localtime(self.updated_at))
                src = {"api": "실시간", "cache": "지난 실행 값"}.get(
                    self.source, "마지막 대화 시점")
                stale = "" if age < 90 else f" ({int(age // 60)}분 전)"
                items.append(pystray.MenuItem(f"— {src} · {when}{stale}",
                                              None, enabled=False))
        else:
            items.append(pystray.MenuItem(self.status or "데이터 없음",
                                          None, enabled=False))
        return items

    def _build_menu(self):
        import pystray
        # 진단 항목은 평소엔 숨긴다 — CLAUDE_WIDGET_DEBUG=1 일 때만 보인다
        dbg = []
        if os.environ.get("CLAUDE_WIDGET_DEBUG"):
            dbg = [pystray.MenuItem("토큰 갱신 테스트 (진단)",
                                    lambda i, it: self.q.put(("reftest",)))]
        upd = []
        if self.update_info:
            upd = [pystray.MenuItem(f"새 버전 v{self.update_info[0]} 설치…",
                                    lambda i, it: self.q.put(("whatsnew",)))]
        # 자동 설치 토글은 EXE 배포본에서만 — 소스 실행은 자동 교체가 없다
        autoupd = []
        if getattr(sys, "frozen", False):
            autoupd = [pystray.MenuItem(
                "새 버전 자동 설치",
                lambda i, it: self.q.put(("autoupd",)),
                checked=lambda it: bool(self.cfg.get("auto_update", True)))]
        # 재로그인은 인증이 끊겼을 때만 — 평소엔 눌러도 할 일이 없는 항목이다
        auth = []
        if self.auth_notice:
            auth = [pystray.MenuItem("재로그인 (터미널 열기)",
                                     lambda i, it: self.q.put(("relogin",)))]
        # 유형별로 실선 구분: 사용량 정보 / 창 열기 / 동작 / 설정 토글 /
        # 이력·진단 / 종료 — 한 덩어리로 붙어 있어 찾기 어렵다는 신고가 있었다
        return pystray.Menu(
            *self._menu_lines(),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("스킬 사용 내역 열기",
                             lambda i, it: self.q.put(("details",))),
            pystray.MenuItem("루틴 알림 열기",
                             lambda i, it: self.q.put(("alerts",))),
            pystray.Menu.SEPARATOR,
            *auth,
            *upd,
            pystray.MenuItem("지금 새로고침", lambda i, it: self.q.put(("refresh",))),
            *dbg,
            pystray.MenuItem("장수 토큰 등록 (클립보드에서)",
                             lambda i, it: self.q.put(("token",)),
                             checked=lambda it: bool(self.cfg.get("setup_token"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("플로팅 바 표시",
                             lambda i, it: self.q.put(("bar",)),
                             checked=lambda it: self.cfg.get("bar_visible", True)),
            pystray.MenuItem("바 위치 잠금 (클릭 통과)",
                             lambda i, it: self.q.put(("lock",)),
                             checked=lambda it: bool(self.cfg.get("bar_locked"))),
            pystray.MenuItem("사용량을 남은 비율로 표시",
                             lambda i, it: self.q.put(("remaining",)),
                             checked=lambda it: bool(
                                 self.cfg.get("usage_remaining"))),
            pystray.MenuItem("Windows 시작 시 자동 실행",
                             lambda i, it: self.q.put(("startup",)),
                             checked=lambda it: bool(self.autostart[1])),
            *autoupd,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self.version_line(), None, enabled=False),
            pystray.MenuItem("업데이트 소식 보기",
                             lambda i, it: self.q.put(("whatsnew",))),
            pystray.MenuItem("로그 폴더 열기", lambda i, it: self.q.put(("log",))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda i, it: self.q.put(("quit",))),
        )

    def _open_login_terminal(self):
        """새 콘솔 창에서 claude 로그인을 띄운다 — 인증은 브라우저로 이어진다."""
        import shutil
        import subprocess
        exe = shutil.which("claude") or os.path.join(
            os.environ.get("APPDATA", HOME), "npm", "claude.cmd")
        if not os.path.exists(exe):
            exe = "claude"              # PATH에 있기를 기대하고 이름만 넘긴다
        try:
            # /k 로 창을 남긴다 — 실패해도 사용자가 오류를 읽을 수 있게.
            # 바깥 따옴표 한 겹은 cmd 의 따옴표 제거 규칙을 막는 관용구다.
            subprocess.Popen(f'cmd /k ""{exe}" auth login"',
                             creationflags=0x00000010)   # CREATE_NEW_CONSOLE
            log.info("login terminal opened: %s", exe)
        except OSError as e:
            log.error("login terminal failed: %s", e)
            if self.icon:
                self.icon.notify("터미널을 열지 못했습니다 — 직접 터미널에서 "
                                 "claude auth login 을 실행해 주세요.",
                                 "Claude 사용량")

    def _worst(self):
        p = [x[1] for x in self.rows if x[1] is not None]
        return max(p) if p else None

    def _blink(self):
        """가끔 눈 한 번 깜빡. 두 장 다 캐시라 그리는 비용은 없다."""
        if not self.icon:
            return
        try:
            pct = self._worst()
            self.icon.icon = make_icon_image(pct, blink=True)
            time.sleep(BLINK_HOLD)
            self.icon.icon = make_icon_image(pct)
            if os.environ.get("CLAUDE_WIDGET_DEBUG"):
                log.info("blink")
        except Exception as e:
            log.error("blink failed: %s", e)

    def _refresh_tray(self):
        if not self.icon:
            return
        try:
            self.icon.icon = make_icon_image(self._worst())
            tip = ["Claude 사용량"]
            for label, pct, reset in self.rows:
                phrase = reset_phrase(reset)
                tip.append(f"{label} {round(pct)}%" + (f" · {phrase}" if phrase else ""))
            if not self.rows:
                tip.append(self.status or "")
            self.icon.title = "\n".join(tip)[:127]
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
        except Exception as e:
            log.error("tray refresh failed: %s", e)

    def _startup_notice(self):
        """첫 설치면 어디를 보면 되는지, 업데이트 직후면 무엇이 바뀌었는지 —
        토스트 한 번. 창을 띄우지 않으니 전체화면 앱에서 포커스를 뺏지 않는다."""
        if not self.icon:
            return
        try:
            if self._fresh_install:
                self.icon.notify(
                    "설치 완료 — 작업표시줄 오른쪽에 Claude 사용량이 표시됩니다. "
                    "트레이의 Claude 아이콘을 누르면 메뉴가 열려요.",
                    "AI Taskbar Widget")
                return
            wn = self.cfg.get("whats_new")
            if wn and wn.get("to") == __version__ and not wn.get("toasted"):
                head = self.range_headline(*self.whats_new_range())
                self.icon.notify(
                    f"v{__version__} 업데이트 완료" + (f" — {head}" if head else "")
                    + f"\n{self.where_to_look()} 바뀐 점을 볼 수 있어요",
                    "Claude 위젯 업데이트")
                wn["toasted"] = True
                save_config(self.cfg)
        except Exception as e:
            log.info("startup notice skipped: %s", e)

    def _pump(self, icon):
        icon.visible = True
        self._refresh_tray()
        for delay in (6, 60):
            t = threading.Timer(delay, lambda: demote_tray_icon())
            t.daemon = True
            t.start()
        t = threading.Timer(4, self._startup_notice)    # 아이콘이 자리 잡은 뒤
        t.daemon = True
        t.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._skill_loop, daemon=True).start()
        threading.Thread(target=self._singleton_listener, daemon=True).start()
        threading.Thread(target=self._refresh_autostart, daemon=True).start()
        FloatingBar(self).start()

        last_tray = 0.0
        next_blink = time.time() + BLINK_EVERY
        while not self.stop_evt.is_set():
            try:
                msg = self.q.get(timeout=1.0)
            except queue.Empty:
                now = time.time()
                if self.rows and now - last_tray >= 30:
                    last_tray = now
                    self._refresh_tray()   # 남은 시간 표시 갱신 (분 단위면 충분)
                if now >= next_blink:
                    next_blink = now + BLINK_EVERY
                    self._blink()
                continue
            kind = msg[0]
            if kind == "data":
                self.rows, self.source, self.updated_at = msg[1], msg[2], msg[3]
                self.status = None
                if self.source == "api":
                    self._save_cache()
                self._refresh_tray()
            elif kind == "status":
                if not self.rows:
                    self.status = msg[1]
                    self._refresh_tray()
            elif kind == "refresh":
                self.force_api.set()
                self.wake.set()
            elif kind == "reftest":
                threading.Thread(target=self._refresh_test,
                                 daemon=True).start()
            elif kind == "relogin":
                self._open_login_terminal()
            elif kind == "token":
                tok = ""
                try:
                    tok = clipboard_text().strip()
                except Exception as e:
                    log.error("clipboard read failed: %s", e)
                if tok.startswith("sk-ant-"):
                    self.cfg["setup_token"] = tok
                    save_config(self.cfg)
                    log.info("setup token registered (len %d)", len(tok))
                    self.force_api.set()
                    self.wake.set()
                    self.icon.notify("장수 토큰 등록됨 — 사용량 조회 재시도",
                                     "Claude 사용량")
                else:
                    self.icon.notify("클립보드에 sk-ant- 로 시작하는 토큰이 없습니다. "
                                     "claude setup-token 결과를 복사한 뒤 다시 눌러주세요.",
                                     "Claude 사용량")
            elif kind == "bar":
                self.cfg["bar_visible"] = not self.cfg.get("bar_visible", True)
                save_config(self.cfg)
                self._refresh_tray()
            elif kind == "details":
                self.details_requested.set()
            elif kind == "alerts":
                self.notes_requested.set()
            elif kind == "lock":
                self.cfg["bar_locked"] = not self.cfg.get("bar_locked")
                save_config(self.cfg)
                self._refresh_tray()
            elif kind == "remaining":
                self.cfg["usage_remaining"] = not self.cfg.get(
                    "usage_remaining")
                save_config(self.cfg)
                self._refresh_tray()
            elif kind == "startup":
                threading.Thread(target=self._toggle_autostart,
                                 daemon=True).start()
            elif kind == "menu":
                self._refresh_tray()
            elif kind == "autoupd":
                self.cfg["auto_update"] = not self.cfg.get("auto_update", True)
                save_config(self.cfg)
                self._refresh_tray()
            elif kind == "update_now":
                threading.Thread(target=self._do_update, daemon=True).start()
            elif kind == "whatsnew":
                self.whats_new_requested.set()
            elif kind == "log":
                os.startfile(APPDATA_DIR)
            elif kind == "quit":
                self.stop_evt.set()
                release_singleton()     # 후임이 기다리지 않게 포트부터 놓는다
                icon.stop()
                return

    def _singleton_listener(self):
        s = _singleton_sock         # release_singleton이 전역을 비워도 안전하게
        if s is None:
            return
        s.settimeout(1.0)
        while not self.stop_evt.is_set():
            try:
                conn, _ = s.accept()
                conn.close()
                self.force_api.set()
                self.wake.set()
            except socket.timeout:
                continue
            except OSError:
                break

    def run(self):
        import pystray
        self.icon = pystray.Icon(APP_NAME, make_icon_image(self._worst()),
                                 "Claude · Codex 스킬 활동", self._build_menu())
        self.icon.run(setup=self._pump)


_singleton_sock = None


SINGLETON_WAIT_SEC = 8      # 전임이 포트를 놓기를 기다리는 최대 시간


def _try_bind_singleton():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(2)
        return s
    except OSError:
        s.close()
        return None


def acquire_singleton():
    """한 번에 하나만 — 단, 전임이 종료 중이면 포트가 풀릴 때까지 기다린다.

    자동 업데이트는 새 EXE를 띄우고 구 인스턴스를 종료하는데, 구 인스턴스가
    포트를 놓기 전에 새 인스턴스가 먼저 도착하면 '이미 떠 있다'고 보고
    그대로 죽어 위젯이 통째로 사라졌다(재시도 없음). 이제 처음 실패하면
    기존 인스턴스에 신호를 보낸 뒤 몇 초간 bind를 다시 시도하고, 그래도
    안 되면 그때 물러난다. 평소 중복 실행(세션 시작 훅)은 신호만 보내고
    잠시 뒤 조용히 끝나므로 겉보기는 전과 같다.
    """
    global _singleton_sock
    s = _try_bind_singleton()
    if s is not None:
        _singleton_sock = s
        return
    signalled = False
    try:
        c = socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=2)
        c.close()
        signalled = True
    except OSError:
        pass
    deadline = time.time() + SINGLETON_WAIT_SEC
    while time.time() < deadline:
        time.sleep(0.5)
        s = _try_bind_singleton()
        if s is not None:
            _singleton_sock = s
            log.info("previous instance released the port - taking over")
            return
    if signalled:
        log.info("already running — signalled existing instance")
        sys.exit(0)
    _singleton_sock = None      # 포트를 누가 쥐고 있는지 모른다 — 그냥 뜬다


def release_singleton():
    """종료 절차 맨 앞에서 포트를 놓는다 — 후임이 즉시 자리를 이어받게."""
    global _singleton_sock
    s, _singleton_sock = _singleton_sock, None
    if s is not None:
        try:
            s.close()
        except OSError:
            pass


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    threading.excepthook = lambda a: log.error(
        "thread crashed", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    acquire_singleton()
    log.info("---- tray v%s start (python %s) ----",
             __version__, sys.version.split()[0])
    finish_exe_update()     # 직전 자동 업데이트의 .old 삭제·훅 교체 마무리
    TrayApp().run()


if __name__ == "__main__":
    main()
