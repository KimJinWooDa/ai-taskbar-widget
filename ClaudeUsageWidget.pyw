# -*- coding: utf-8 -*-
"""
Claude 사용량 트레이 아이콘 v2 — 시계 옆에 사용률(%)을 항상 표시.

데이터 (우선순위):
 1. 사용량 API — 대화하지 않아도 60초마다 갱신 (계정 정책이 허용할 때만)
 2. ~/.claude/usage-widget.json — Stop 훅이 답변 직후 남기는 값 (API가 막혔을 때)

트레이 아이콘 = 가장 한도에 가까운 항목의 %.
아이콘 클릭 → 항목별 수치와 재설정까지 남은 시간이 메뉴에 표시된다.
"""
import ctypes
import ctypes.wintypes
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
                           ago as notify_ago)

__version__ = "3.8.0"

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
UPDATE_CHECK_SEC = 24 * 3600

APPDATA_DIR = os.path.join(os.environ.get("APPDATA", HOME), APP_NAME)
LOG_PATH = os.path.join(APPDATA_DIR, "widget.log")
CONFIG_PATH = os.path.join(APPDATA_DIR, "config.json")
CACHE_PATH = os.path.join(APPDATA_DIR, "last-usage.json")
CACHE_MAX_AGE = 24 * 3600
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_VBS = os.path.join(STARTUP_DIR, "ClaudeUsageWidget.vbs")

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


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
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
_CHANGELOG_HEAD = re.compile(r"^##\s+v?(\d+(?:\.\d+)*)\b")


def _ver_tuple(s):
    """'2.7.0' → (2, 7, 0). 자릿수가 달라도 비교되게 3자리로 맞춘다."""
    try:
        nums = [int(x) for x in str(s).split(".")]
    except ValueError:
        return (0, 0, 0)
    return tuple((nums + [0, 0, 0])[:3])


def parse_changelog(text):
    """CHANGELOG.md → [(버전튜플, '2.7.0', 본문)] 파일 순서(최신이 먼저)."""
    entries = []
    for line in text.splitlines():
        m = _CHANGELOG_HEAD.match(line.strip())
        if m:
            entries.append([_ver_tuple(m.group(1)), m.group(1), []])
        elif entries and line.strip():
            entries[-1][2].append(line.rstrip())
    return [(t, s, "\n".join(body)) for t, s, body in entries]


def fetch_changelog():
    req = urllib.request.Request(CHANGELOG_URL, headers={"User-Agent": CLI_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


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


def _msgbox(text, title, flags):
    # MB_TOPMOST | MB_SETFOREGROUND — 트레이에서 띄우는 창이 뒤로 숨지 않게
    return ctypes.windll.user32.MessageBoxW(0, text, title,
                                            flags | 0x40000 | 0x10000)


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
        try:
            for root, dirs, files in os.walk(TRANSCRIPT_DIR):
                for f in files:
                    if f.endswith(".jsonl"):
                        p = os.path.join(root, f)
                        try:
                            m = os.path.getmtime(p)
                        except OSError:
                            continue
                        if m > best_m:
                            best_m, best_path = m, p
        except OSError:
            pass
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


def _window_exe(hwnd):
    """창을 소유한 프로세스의 실행파일 이름(소문자) — 실패 시 ''."""
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        pid = ctypes.wintypes.DWORD()
        u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            n = ctypes.wintypes.DWORD(512)
            if k.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf,
                                            ctypes.byref(n)):
                return os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
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
        if cn.value in ("Progman", "WorkerW", "Shell_TrayWnd",
                        "Shell_SecondaryTrayWnd"):
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
    MAX_PANELS = 4          # Claude 스킬 + Codex 스킬 + 루틴 알림 + 사용량
    PANEL_GAP = 20
    ACCENTS = {"claude": "#d97757", "codex": "#45a79a", "notify": "#c58a1a"}
    FONT_PX = -10           # 음수 = 픽셀 지정. 8pt(11px)에서 한 단계만 줄인 값
    PAD = 8                 # 좌우 여백 — 모든 줄의 라벨이 여기서 시작한다
    TICK_MS = 500           # 전체화면 전환을 늦게 알아채지 않도록 짧게 (2초→0.5초)
    RESTORE_MS = 30         # 숨어 있는 동안에만 도는 복귀 확인 (평소엔 안 돈다)
    HIDE_MS = 100           # 훅이 놓쳤을 때를 위한 보험 (평소엔 훅이 먼저 잡는다)
    CAMO_EVERY = 20         # 10초마다 배경 확인
    ADOPT_EVERY = 120       # 60초마다 소유 관계 재확인
    CAMO_MAX_AGE = 300      # 옆 픽셀이 그대로여도 이 시간이 지나면 한 번 다시 찍는다
    SIDE = 12               # 배경을 떠올 좌우 여백 폭
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
        f = self._font = tkfont.Font(family="맑은 고딕", size=self.FONT_PX)
        # -8px는 한글이 뭉개진다 — 본문(-10)보다 한 단계만 작게
        self._font_small = tkfont.Font(family="맑은 고딕", size=-9)
        self._col_gap = max(9, f.measure(" ") * 3)  # 라벨-숫자 사이 간격
        self._panel_w = f.measure("image-prompt-craft  999회  · 자동 999") + 24
        self._usage_w = f.measure("Sonnet 100%  · 16시간 59분 후") + 24
        self._fix_w = self._usage_w
        self._fix_h = self.LINES * f.metrics("linespace") + 6
        self._shown = False
        self._covered = 0
        self._ticks = 0
        self._rgb = None
        self._pending = None
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
        self._pal = self.PAL_DARK
        self._bgimg = None
        self._probe_warned = False
        self._last = [None] * (self.LINES * self.MAX_PANELS)
        self._panel_widths = [self._usage_w]
        self._panel_kinds = ["usage"]
        self._details = None
        self._notes = None      # 루틴 알림 목록 창
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

    def _probe(self):
        """바 옆 작업표시줄 픽셀 몇 개의 평균 — 배경이 바뀌었는지 감지용.

        한 점만 보면 그 점이 우연히 안 변한 스타일 변화(테마·미카 톤 변경)를
        놓친다. 위·아래·좌우로 흩어 뽑아 평균을 내면 훨씬 잘 잡힌다.
        """
        try:
            self.root.update_idletasks()    # geometry 반영 전 winfo_x()=0 방지
            u, g = ctypes.windll.user32, ctypes.windll.gdi32
            x, y, h = self.root.winfo_x(), self.root.winfo_y(), self._fix_h
            # 전부 바 왼쪽의 빈 구간에서 — 오른쪽은 트레이 아이콘이 가까워서
            # 아이콘이 바뀔 때마다 배경을 다시 만들게 된다(60초마다 재촬영의 원인)
            pts = [(x - 6, y + h // 2), (x - 18, y + 3), (x - 6, y + h - 3),
                   (x - 30, y + h // 2)]
            dc = u.GetDC(0)
            got = []
            for px, py in pts:
                c = g.GetPixel(dc, px, py)
                if c >= 0:
                    got.append((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF))
            u.ReleaseDC(0, dc)
            if not got:
                return None
            return tuple(sum(c[i] for c in got) // len(got) for i in range(3))
        except Exception:
            return None

    def _match_background(self, force=False):
        """바 양옆 작업표시줄을 떠서 그 사이를 이어 붙여 배경으로 쓴다.

        예전에는 바를 잠깐 숨기고 그 자리를 찍었는데, 그 순간이 눈에 띄었다
        (화면 캡처 직후처럼 다시 찍을 일이 겹치면 특히). 바가 놓인 구간은
        아이콘이 없는 매끈한 자리라, 좌우 끝을 가로로 이어 붙이면 실제와 같다.
        """
        if self._snip_active and self._bgimg is not None:
            return      # 캡처 오버레이로 어두워진 화면을 배경으로 찍으면 안 됨
        if not force and time.time() - self._camo_at > self.CAMO_MAX_AGE:
            force = True    # 옆 픽셀이 그대로여도 오래되면 한 번 다시 맞춘다
        rgb = self._probe()
        if rgb is None:
            # 조용히 포기하면 바가 임시 배경(#1f1f1f) 그대로 검게 남는다 —
            # 원인 추적이 되도록 창당 한 번은 남긴다. 재시도는 틱이 한다.
            if self._bgimg is None and not self._probe_warned:
                self._probe_warned = True
                log.info("camo probe failed at %d,%d",
                         self.root.winfo_x(), self.root.winfo_y())
            return
        if self._bgimg is None:
            # 첫 촬영 전에도 검정을 보여주지 않는다 — 옆 픽셀 색으로 먼저 칠하고
            # 글자 팔레트도 그 밝기에 맞춘다(촬영은 바로 아래에서 이어진다).
            solid = "#%02x%02x%02x" % rgb
            self.root.configure(bg=solid)
            self.cv.configure(bg=solid)
            lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
            self._pal = self.PAL_LIGHT if lum >= 128 else self.PAL_DARK
        if not force:
            if self._rgb and \
                    max(abs(a - b) for a, b in zip(rgb, self._rgb)) <= 3:
                self._pending = None
                return
            # 새 색이 2회 연속(약 60초) 유지될 때만 재촬영 —
            # 아이콘 점멸·알림 토스트 같은 일시 변화로 깜빡이지 않게
            if self._pending is None or \
                    max(abs(a - b) for a, b in zip(rgb, self._pending)) > 3:
                self._pending = rgb
                return
            self._pending = None
        self._rgb = rgb
        try:
            from PIL import Image, ImageGrab, ImageTk
            x, y = self.root.winfo_x(), self.root.winfo_y()
            w, h, s = self._fix_w, self._fix_h, self.SIDE
            x0 = max(x - s, 0)
            x1 = min(x + w + s, self.root.winfo_screenwidth())
            lw, rw = x - x0, x1 - (x + w)
            if lw <= 0 and rw <= 0:
                return
            shot = ImageGrab.grab(bbox=(x0, y, x1, y + h),
                                  all_screens=True).convert("RGB")
            left = (shot.crop((0, 0, lw, h)).resize((w, h)) if lw > 0 else None)
            right = (shot.crop((shot.width - rw, 0, shot.width, h))
                     .resize((w, h)) if rw > 0 else None)
            if left is None:
                img = right
            elif right is None:
                img = left
            else:
                ramp = Image.new("L", (w, 1))
                ramp.putdata([255 * i // max(w - 1, 1) for i in range(w)])
                img = Image.composite(right, left, ramp.resize((w, h)))
            self._bgimg = ImageTk.PhotoImage(img)
            self.cv.itemconfigure(self._img_item, image=self._bgimg)
            r, gr, b = img.resize((1, 1)).getpixel((0, 0))[:3]
            lum = 0.299 * r + 0.587 * gr + 0.114 * b
            self._pal = self.PAL_LIGHT if lum >= 128 else self.PAL_DARK
            self._last = [None] * (self.LINES * self.MAX_PANELS)
            self._camo_at = time.time()
            log.info("bar camo #%02x%02x%02x", r, gr, b)
        except Exception:
            log.exception("bg capture failed")

    def _value_color(self, pct):
        if pct >= 90:
            return "#da3633"
        if pct >= 70:
            return "#bb8009"
        return self._pal["value"]

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
            right = sw - 330                       # 트레이 아이콘 왼쪽
        # 줄이 늘어 바가 높아지면 저장된 y로는 화면 아래로 넘친다 — 다시 맞춘다
        if y is None or int(y) + h > sh:
            if sh > r.bottom:                      # 작업표시줄이 아래쪽
                y = r.bottom + max((sh - r.bottom - h) // 2, 0)
            else:
                y = sh - h - 8
        self.root.geometry(f"{w}x{h}+{int(right) - w}+{int(y)}")

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
            # 알림 패널을 눌렀으면 알림 목록, 그 밖은 기존대로 스킬 내역
            if self._panel_kind_at(e.x_root - self.root.winfo_x()) == "notify":
                self._toggle_notifications()
            else:
                self._toggle_details()
            return
        self.app.cfg["bar_right"] = self.root.winfo_x() + self._fix_w
        self.app.cfg["bar_y"] = self.root.winfo_y()
        save_config(self.app.cfg)
        self._match_background(force=True)  # 옮긴 자리의 배경으로 다시 위장

    def _hide_click(self, e):
        self.app.cfg["bar_visible"] = False
        save_config(self.app.cfg)
        self._show(False)

    def _skill_panels(self):
        """실행 중인 앱별 스킬 요약 패널. 줄 = (라벨, 값, 시간, 라벨색, 값색)."""
        clients, summaries, _ = self.app.skill_tracker.snapshot()
        panels = []
        for client in clients:
            data = summaries.get(client) or {}
            title = "Claude" if client == "claude" else "Codex"
            accent = self.ACCENTS.get(client)
            # 작은 "스킬" 꼬리표 — 사용량 패널의 "Claude 22%"와 안 헷갈리게
            lines = [(title, f"{data.get('installed', 0)}개", "",
                      accent, accent, "스킬")]
            for row in data.get("top", [])[:2]:
                name = row["name"]
                if len(name) > 22:
                    name = name[:20] + "…"
                lines.append((name, f"{row['total_count']}회", "",
                              None, None, ""))
            while len(lines) < self.LINES:
                lines.append(("실행 기록 없음" if len(lines) == 1 else "",
                              "", "", None, None, ""))
            panels.append({"width": self._panel_width(lines), "lines": lines})
        return panels

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
                lines.append((title, f"{round(pct)}%",
                              f" · {t}" if t else "",
                              self.ACCENTS["claude"] if idx == 0 else None,
                              self._value_color(pct), ""))
            else:
                lines.append(("", "", "", None, None, ""))
        return {"width": self._panel_width(lines), "lines": lines}

    def _panels(self):
        """맨 왼쪽이 루틴 알림, 그다음 스킬 패널들, 맨 오른쪽이 사용량 패널.

        알림을 가장 왼쪽에 두는 이유: 바는 오른쪽 끝이 앵커라 왼쪽으로 자란다.
        알림이 맨 왼쪽이면 알림이 생기거나 사라져도 스킬·사용량 패널은 제자리에
        그대로 있고, 바가 바깥쪽으로만 늘었다 줄었다 한다. 중간에 끼우면 알림이
        뜰 때마다 왼쪽 패널들이 통째로 밀려서 자리가 흔들린다.
        """
        panels, kinds = [], []
        notify = self._notify_panel()
        if notify:
            panels.append(notify)
            kinds.append("notify")
        skills = self._skill_panels()
        panels.extend(skills)
        kinds.extend(["skill"] * len(skills))
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
        """라벨 + 작은 꼬리표("스킬")가 차지하는 폭."""
        w = self._font.measure(row[0])
        if len(row) > 5 and row[5]:
            w += self._font_small.measure(row[5]) + 4
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
        y = self.root.winfo_y()
        # 오른쪽 끝을 고정해 패널이 늘 때 트레이 쪽이 아니라 왼쪽으로 자란다.
        #
        # 앵커는 설정에 저장된 bar_right가 유일한 진실이다. 예전처럼 창의 현재
        # x에서 되계산하면(x + 폭), 기동 직후 _place_initial의 geometry가 아직
        # 반영되지 않은 x를 읽어 앵커가 패널 폭만큼 오염된다 — 재시작 한 번에
        # +137px씩 오른쪽으로 밀려 결국 화면 밖으로 나갔다(실측 2548→2685→2822).
        # 여기서는 bar_right를 읽기만 하고, 쓰는 것은 사용자가 드래그로 자리를
        # 정하는 _save_pos 한 곳뿐이다.
        right = self.app.cfg.get("bar_right")
        if right is None:                       # 첫 실행 — 지금 자리를 앵커로
            right = self.root.winfo_x() + self._fix_w
            self.app.cfg["bar_right"] = int(right)
        x = int(right) - self._fix_w
        self.cv.configure(width=self._fix_w)
        self.root.geometry(f"{self._fix_w}x{self._fix_h}+{x}+{y}")
        self.app.cfg["bar_y"] = y
        save_config(self.app.cfg)
        self._bgimg = None
        self._last = [None] * (self.LINES * self.MAX_PANELS)
        self.root.update_idletasks()    # 새 geometry가 잡힌 뒤에 배경을 뜬다
        self._match_background(force=True)

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
        """루틴이 남긴 결과 목록. 여는 순간 전부 읽음 처리 → 패널이 사라진다."""
        import tkinter as tk

        all_rows, unread = self.app.notifications.snapshot()
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

        width, height = 560, 400
        x = max(8, min(self.root.winfo_x() + self._fix_w - width,
                       self.root.winfo_screenwidth() - width - 8))
        y = max(8, self.root.winfo_y() - height - 10)
        win.geometry(f"{width}x{height}+{x}+{y}")

        head = tk.Frame(win, bg="#ffffff", height=54)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="Routine Alerts", bg="#ffffff", fg="#202124",
                 font=("Segoe UI Semibold", 14)).pack(side="left", padx=(18, 8))
        tk.Label(head, text=f"새 알림 {len(unread)}건 · 전체 {len(all_rows)}건",
                 bg="#ffffff", fg="#6b7280", font=("맑은 고딕", 9)
                 ).pack(side="left", pady=(3, 0))
        tk.Button(head, text="×", command=self._toggle_notifications,
                  relief="flat", borderwidth=0, bg="#ffffff",
                  activebackground="#eeeeee", fg="#6b7280",
                  font=("Segoe UI", 15), cursor="hand2"
                  ).pack(side="right", padx=14)

        wrap = tk.Frame(win, bg="#f4f5f7")
        wrap.pack(fill="both", expand=True, padx=18, pady=(10, 6))
        bar = tk.Scrollbar(wrap, orient="vertical")
        bar.pack(side="right", fill="y")
        body = tk.Text(wrap, wrap="word", relief="flat", bg="#ffffff",
                       fg="#30343b", font=("맑은 고딕", 9), padx=14, pady=10,
                       cursor="arrow", yscrollcommand=bar.set,
                       highlightbackground="#e3e6ea", highlightthickness=1)
        bar.configure(command=body.yview)
        body.pack(side="left", fill="both", expand=True)
        body.tag_configure("fresh", font=("맑은 고딕", 9, "bold"),
                           foreground="#202124")
        body.tag_configure("title", foreground="#30343b")
        body.tag_configure("meta", foreground="#8a9099")

        if not all_rows:
            body.insert("end", "아직 받은 루틴 알림이 없습니다.\n\n", "meta")
            body.insert("end", "예약 작업이 결과를 남기면 여기에 쌓이고, "
                               "안 읽은 게 있을 때만 작업표시줄에 표시됩니다.\n",
                        "meta")
        for row in reversed(all_rows[-100:]):       # 최신부터, 최근 100건
            fresh = row["index"] >= unread_from
            title = row["title"] or "(제목 없음)"
            body.insert("end", f"{'●  ' if fresh else '　  '}{title}\n",
                        "fresh" if fresh else "title")
            body.insert("end", f"      {row['body']}\n", "title")
            rel = notify_ago(row["ts"])
            stamp = row["when"] or ""
            body.insert("end",
                        f"      {stamp}{'  ·  ' + rel if rel else ''}\n\n",
                        "meta")
        body.configure(state="disabled")

        tk.Label(win, text=f"기록: {NOTIFY_LOG}", bg="#f4f5f7", fg="#8a9099",
                 font=("맑은 고딕", 8)).pack(anchor="w", padx=18, pady=(0, 10))

        win.bind("<Escape>", lambda e: self._toggle_notifications())
        # 창에 실제로 띄운 것까지만 읽음 — 그 사이 도착한 새 알림은 남겨 둔다.
        # 다음 틱에 바에서 패널이 사라진다(안 읽은 게 0이면 _notify_panel이 None).
        self.app.notifications.mark_all_read(len(all_rows))

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

        width, height = 640, 520
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
        if self.app.stop_evt.is_set():
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
            self._match_background(force=True)
            self._adopt_by_taskbar()
            self._sync_topmost(raise_now=True)
        if self._recapture and not snip:
            self._recapture = False
            self._match_background(force=True)
        elif self._ticks % self.CAMO_EVERY == 0:
            self._match_background()    # 재촬영은 2회 연속 변했을 때만
        elif self._bgimg is None:
            self._match_background(force=True)  # 첫 배경을 얻기까지 매 틱 재시도
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
        self.cv.coords(tg, base + self.PAD + self._font.measure(label) + 4, y)
        self.cv.coords(v, cols[0], y)      # 값은 오른쪽 정렬 — 끝이 맞는다
        self.cv.coords(w, cols[1], y)

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

    def _show(self, on):
        """상태가 바뀔 때만 표시/숨김 — 매 틱 재표시로 인한 깜빡임 방지."""
        if on and not self._shown:
            if not self._mapped:
                self.root.deiconify()   # Tk 상태를 normal로 만드는 건 한 번만
                self._mapped = True
            self._win_show(True)        # 먼저 띄운다 — 배경 촬영이 복귀를 늦추면 안 된다
            self._adopt_by_taskbar()    # 표시 후에 걸어야 Tk가 안 지운다
            self._sync_topmost(raise_now=True)
            # 숨어 있는 동안 아래가 바뀌었을 수 있으니(테마·아이콘) 곧바로 맞춘다.
            # 바 양옆을 뜨는 방식이라 바가 보이는 채로 찍어도 된다(v2.16).
            self._match_background(force=True)
        elif not on and self._shown:
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
        self.update_info = None     # (새 버전 문자열, 패치노트) — 있으면 메뉴에 표시
        self._updating = False
        self.icon = None
        self.cfg = load_config()
        self.skill_tracker = TrackerService()
        self.notifications = NotificationService()
        self.notes_requested = threading.Event()
        self.details_requested = threading.Event()
        if os.environ.get("SKILL_WIDGET_SHOW_DETAILS") == "1":
            self.details_requested.set()
        if os.environ.get("SKILL_WIDGET_SHOW_ALERTS") == "1":
            self.notes_requested.set()

        self._load_file(initial=True)   # 켜자마자 마지막 값 표시
        if not self.rows:
            self._load_cache()          # 훅 데이터가 없으면 지난 실행의 API 값

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
        """CHANGELOG의 최신 버전이 내 버전보다 높으면 알림 + 메뉴 항목 준비."""
        if getattr(sys, "frozen", False):
            return  # exe 배포본은 설치 프로그램으로만 안전하게 갱신한다
        entries = parse_changelog(fetch_changelog())
        if not entries:
            return
        cur = _ver_tuple(__version__)
        if entries[0][0] <= cur:
            self.update_info = None
            return
        latest = entries[0][1]
        notes = "\n\n".join(f"v{s}\n{body}" for t, s, body in entries
                            if t > cur)
        self.update_info = (latest, notes)
        log.info("update available: v%s (current v%s)", latest, __version__)
        if self.cfg.get("notified_version") != latest:
            self.cfg["notified_version"] = latest
            save_config(self.cfg)
            if self.icon:
                self.icon.notify(f"새 버전 v{latest}가 나왔습니다 — "
                                 "트레이 메뉴에서 설치할 수 있습니다",
                                 "Claude 위젯 업데이트")
                log.info("update toast shown: v%s", latest)

    def _do_update(self):
        """패치노트를 보여주고 확인하면 zip으로 교체 후 재시작. (별도 스레드)"""
        import shutil
        import subprocess
        import tempfile
        info = self.update_info
        if not info or self._updating:
            return
        ver, notes = info
        here = os.path.dirname(os.path.abspath(__file__))
        if os.path.isdir(os.path.join(here, ".git")):
            _msgbox(f"v{ver} 패치노트:\n\n{notes[:1500]}\n\n"
                    "개발 폴더(git 저장소)에서 실행 중이라 자동 설치는 하지 "
                    "않습니다. git pull 로 업데이트하세요.",
                    "Claude 위젯 업데이트", 0x40)      # MB_ICONINFORMATION
            return
        ok = _msgbox(f"v{ver} 패치노트:\n\n{notes[:1500]}\n\n"
                     "지금 설치하고 재시작할까요?",
                     f"Claude 위젯 업데이트 v{ver}", 0x41)  # OKCANCEL | INFO
        if ok != 1:                                         # IDOK
            return
        self._updating = True
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
            _msgbox(f"업데이트 실패: {e}\n\n"
                    "README의 설치 명령으로 다시 설치하면 해결됩니다.",
                    "Claude 위젯 업데이트", 0x10)           # MB_ICONERROR

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
                            now - last_attempt >= EVENT_MIN_GAP:
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
                        next_api = now + (API_INTERVAL_ACTIVE if active
                                          else API_INTERVAL_IDLE)
                    except ApiThrottled as e:
                        api_ok = False
                        throttle_streak += 1
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
                            n = "재로그인 필요 · claude /login"
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
                            and not api_denied_reason:
                        # 답변 직후 = 사용량이 막 변한 시점, 즉시 재조회
                        next_api = min(next_api, now)

                if not self.rows:
                    if api_denied_reason and "설정 토큰" in api_denied_reason:
                        st = "설정 토큰 거부(403) — 조직 설정 확인"
                    elif api_denied_reason and ("토큰 갱신" in api_denied_reason
                                                or "리프레시 토큰" in api_denied_reason
                                                or "인증" in api_denied_reason):
                        st = "재로그인 필요 — 터미널에서 claude /login"
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
        while not self.stop_evt.is_set():
            try:
                self.skill_tracker.refresh(force=first)
                first = False
            except Exception:
                log.exception("skill tracker refresh failed")
            self.stop_evt.wait(2)

    # ---------------- 트레이
    def _menu_lines(self):
        import pystray
        items = []
        if self.rows:
            for label, pct, reset in self.rows:
                phrase = reset_phrase(reset)
                text = f"{label}   {round(pct)}%"
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
                                    lambda i, it: self.q.put(("update",)))]
        return pystray.Menu(
            *self._menu_lines(),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("스킬 사용 내역 열기",
                             lambda i, it: self.q.put(("details",))),
            pystray.MenuItem("루틴 알림 열기",
                             lambda i, it: self.q.put(("alerts",))),
            *upd,
            pystray.MenuItem("지금 새로고침", lambda i, it: self.q.put(("refresh",))),
            *dbg,
            pystray.MenuItem("장수 토큰 등록 (클립보드에서)",
                             lambda i, it: self.q.put(("token",)),
                             checked=lambda it: bool(self.cfg.get("setup_token"))),
            pystray.MenuItem("플로팅 바 표시",
                             lambda i, it: self.q.put(("bar",)),
                             checked=lambda it: self.cfg.get("bar_visible", True)),
            pystray.MenuItem("바 위치 잠금 (클릭 통과)",
                             lambda i, it: self.q.put(("lock",)),
                             checked=lambda it: bool(self.cfg.get("bar_locked"))),
            pystray.MenuItem("Windows 시작 시 자동 실행",
                             lambda i, it: self.q.put(("startup",)),
                             checked=lambda it: startup_installed()),
            pystray.MenuItem("패치 이력 보기", lambda i, it: self.q.put(("notes",))),
            pystray.MenuItem("로그 폴더 열기", lambda i, it: self.q.put(("log",))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda i, it: self.q.put(("quit",))),
        )

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

    def _pump(self, icon):
        icon.visible = True
        self._refresh_tray()
        for delay in (6, 60):
            t = threading.Timer(delay, lambda: demote_tray_icon())
            t.daemon = True
            t.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._skill_loop, daemon=True).start()
        threading.Thread(target=self._singleton_listener, daemon=True).start()
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
            elif kind == "startup":
                uninstall_startup() if startup_installed() else install_startup()
                self._refresh_tray()
            elif kind == "update":
                threading.Thread(target=self._do_update, daemon=True).start()
            elif kind == "notes":
                import webbrowser
                webbrowser.open(CHANGELOG_PAGE)
            elif kind == "log":
                os.startfile(APPDATA_DIR)
            elif kind == "quit":
                self.stop_evt.set()
                icon.stop()
                return

    def _singleton_listener(self):
        if _singleton_sock is None:
            return
        _singleton_sock.settimeout(1.0)
        while not self.stop_evt.is_set():
            try:
                conn, _ = _singleton_sock.accept()
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


def acquire_singleton():
    global _singleton_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(2)
        _singleton_sock = s
    except OSError:
        s.close()
        try:
            c = socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=2)
            c.close()
            log.info("already running — signalled existing instance")
            sys.exit(0)
        except OSError:
            _singleton_sock = None


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
    TrayApp().run()


if __name__ == "__main__":
    main()
