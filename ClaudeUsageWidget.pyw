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

__version__ = "2.17.0"

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

REPO = "KimJinWooDa/claude-taskbar-widget"
CHANGELOG_URL = f"https://raw.githubusercontent.com/{REPO}/main/CHANGELOG.md"
CHANGELOG_PAGE = f"https://github.com/{REPO}/blob/main/CHANGELOG.md"
REPO_ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/main.zip"
REPO_ZIP_TOPDIR = "claude-taskbar-widget-main"
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
    content = ('CreateObject("Wscript.Shell").Run '
               f'"""{pythonw_exe()}"" ""{os.path.abspath(__file__)}""", 0, False')
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


def _snip_foreground():
    """전면에 화면 캡처 도구의 오버레이가 떠 있는가.

    캡처 중에는 바를 숨기면 안 된다 — 찍힌 사진에서 바만 빠진다.
    """
    try:
        fg, top = _foreground_pair()
        if not fg:
            return False
        return bool(({_window_exe(fg), _window_exe(top)} - {""}) & SNIP_EXES)
    except Exception:
        return False


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
    return not _snip_foreground()


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
    """작업표시줄에 얹히는 투명 세 줄 바 (시안 B) — 세션 / 주간 / 모델별(Fable).

    평상시 무채색, 70%↑ 주황·90%↑ 빨강만 색 표시. 드래그로 이동(위치 저장),
    우클릭 숨김, 트레이 메뉴에서 표시·잠금 토글. 잠금 시 클릭이 통과한다.
    z순서는 작업표시줄에 맞춘다 — 소유자로 지정해 바로 위에 두고, topmost
    여부까지 따라가서 전체화면 앱이 뜨면 작업표시줄과 함께 아래로 내려간다.
    """

    LINES = 3               # 작업표시줄 48px에 12px 줄 3개 + 여백 6px
    FONT_PX = -10           # 음수 = 픽셀 지정. 8pt(11px)에서 한 단계만 줄인 값
    PAD = 8                 # 좌우 여백 — 모든 줄의 라벨이 여기서 시작한다
    TICK_MS = 500           # 전체화면 전환을 늦게 알아채지 않도록 짧게 (2초→0.5초)
    FAST_MS = 100           # 훅이 놓쳤을 때를 위한 보험 확인 (평소엔 훅이 먼저)
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
        self._fix_w = f.measure("주간 (모든 모델) 100%  · 16시간 59분 후") + 24
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
        self._camo_at = 0.0
        self._pal = self.PAL_DARK
        self._bgimg = None
        self._last = [None] * self.LINES
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
                                      fill=self._pal["time"]))
                      for y in self._ys]
        for w in (root, cv):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._save_pos)
            w.bind("<Button-3>", self._hide_click)
        self._place_initial()
        self._adopt_by_taskbar()
        root.update_idletasks()  # 이걸 해야 최상위 창이 생긴다(그전엔 GetParent=0)
        self._apply_lock()       # 첫 deiconify 전에 걸어야 그때부터 안 뺏는다
        self._hook_events()      # 전체화면 진입을 즉시 받는다 (폴링은 보험)
        self._tick()
        self._fast()
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
            if raise_now:
                u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(0),
                               0, 0, 0, 0, 0x0013)  # HWND_TOP — 작업표시줄 위로
        except Exception:
            log.exception("topmost sync failed")

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
            return
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
            self._last = [None] * self.LINES  # 새 팔레트로 텍스트 다시 그리기
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
        w, h = self._fix_w, self._fix_h
        x, y = self.app.cfg.get("bar_x"), self.app.cfg.get("bar_y")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                   ctypes.byref(r), 0)
        if x is None:
            x = sw - w - 330                       # 트레이 아이콘 왼쪽
        # 줄이 늘어 바가 높아지면 저장된 y로는 화면 아래로 넘친다 — 다시 맞춘다
        if y is None or int(y) + h > sh:
            if sh > r.bottom:                      # 작업표시줄이 아래쪽
                y = r.bottom + max((sh - r.bottom - h) // 2, 0)
            else:
                y = sh - h - 8
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    def _press(self, e):
        if self.app.cfg.get("bar_locked"):
            return
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag(self, e):
        if self.app.cfg.get("bar_locked") or not hasattr(self, "_dx"):
            return
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _save_pos(self, e):
        if self.app.cfg.get("bar_locked") or not hasattr(self, "_dx"):
            return
        self.app.cfg["bar_x"] = self.root.winfo_x()
        self.app.cfg["bar_y"] = self.root.winfo_y()
        save_config(self.app.cfg)
        self._match_background(force=True)  # 옮긴 자리의 배경으로 다시 위장

    def _hide_click(self, e):
        self.app.cfg["bar_visible"] = False
        save_config(self.app.cfg)
        self._show(False)

    def _pick(self):
        """rows에서 [(줄이름, 값)] 세 줄 — 세션 / 주간(모든 모델) / 모델별."""
        sess = week = model = None
        mname = "모델"
        for label, pct, reset in self.app.rows:
            if label == "현재 세션" and sess is None:
                sess = (pct, reset)
            elif label.startswith("주간 (") and week is None:
                week = (pct, reset)
            elif label.startswith("주간 ") and model is None:
                model = (pct, reset)
                mname = label[3:]           # "주간 Fable" → "Fable"
        return [("세션", sess), ("주간", week), (mname, model)]

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

    def _on_win_event(self, hook, event, hwnd, idobj, idchild, thread, ms):
        """창이 전면이 되거나 크기가 바뀐 순간 — 전체화면이면 그 자리에서 숨는다.

        이 콜백은 Tk의 메시지 펌프 한가운데서 불린다. 여기서 Tk/Tcl을 건드리면
        재진입이라 프로세스가 그대로 죽는다(실측: `winfo_id()`를 부르는 경로를
        넣었더니 pythonw가 0xc0000409로 크래시). 그래서 Win32만 쓰고, 창 핸들도
        미리 받아 둔 것을 쓴다 — 갱신은 틱이 한다.
        """
        if idobj != OBJID_WINDOW or not self._shown or not self._hwnd:
            return
        try:
            u = ctypes.windll.user32
            u.GetForegroundWindow.restype = ctypes.c_void_p
            if hwnd and hwnd != u.GetForegroundWindow():
                return              # 뒤쪽 창이 움직인 것 — 대부분 여기서 끝난다
            if _fullscreen_now():
                u.ShowWindow(ctypes.c_void_p(self._hwnd), 0)    # SW_HIDE
                self._shown = False
        except Exception:
            pass                    # 콜백에서 로그를 쏟지 않는다

    def _fast(self):
        """훅이 못 받은 경우를 위한 보험 — 숨기는 쪽만, 조용히.

        다시 보이는 것은 틱이 한다. 두 군데서 보이기를 다투면 깜빡인다.
        """
        if self.app.stop_evt.is_set():
            return
        try:
            if self._shown and _fullscreen_now():
                self._show(False)
        except Exception:
            log.exception("fast hide failed")
        self.root.after(self.FAST_MS, self._fast)

    def _update(self):
        self._ticks += 1
        # 콜백에서 쓸 창 핸들은 여기(Tk 스레드)서만 구한다
        u0 = ctypes.windll.user32
        self._hwnd = u0.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        if not self.app.cfg.get("bar_visible", True):
            self._show(False)
            return
        covered, snip = _taskbar_covered()
        if self._snip_active and not snip:
            self._recapture = True  # 캡처가 끝났으면 그동안의 변화를 다시 입는다
        self._snip_active = snip
        self._sync_topmost()
        # 픽셀 검사만으로는 작업표시줄이 아직 영상 위에 그려진 순간을 못 잡는다
        fullscreen = not snip and _fullscreen_now()
        hide = covered or fullscreen
        self._covered = self._covered + 1 if hide else 0
        self._clear = 0 if hide else self._clear + 1
        if fullscreen or self._covered >= 2:    # 픽셀 판정만 2회 연속을 요구한다
            self._show(False)
            return
        if not self._shown and self._clear < 2:
            return          # 가림이 풀린 직후 한 틱은 더 본다 — 되보이기 깜빡임 방지
        if self._recapture and not snip:
            self._recapture = False
            self._match_background(force=True)
        elif self._ticks % self.CAMO_EVERY == 0:
            self._match_background()    # 재촬영은 2회 연속 변했을 때만
        lines = self._pick()
        notice = self.app.auth_notice
        cols = self._columns(lines)
        for idx, (title, row) in enumerate(lines):
            if idx == len(lines) - 1 and notice:    # 마지막 줄을 재발급 안내로
                self._set_line(idx, notice, "", "", "#da3633", cols,
                               lcolor="#da3633")
            elif row:
                pct, reset = row
                t = short_reset(reset)
                self._set_line(idx, title, f"{round(pct)}%",
                               f" · {t}" if t else "", self._value_color(pct),
                               cols)
            else:
                self._set_line(idx, "", "", "", self._pal["value"], cols)
        self._show(True)
        self._apply_lock()

    def _columns(self, lines):
        """세 줄이 같은 열에 서도록 (값 오른쪽끝 x, 시간 시작 x)를 구한다."""
        f = self._font
        gap = f.measure(" ")
        label_w = max([f.measure(t) for t, _ in lines] or [0])
        value_w = max([f.measure(f"{round(r[0])}%") for _, r in lines if r]
                      or [f.measure("100%")])
        end = self.PAD + label_w + gap + value_w
        return (end, end)

    def _set_line(self, idx, label, value, when, vcolor, cols, lcolor=None):
        """실제로 달라졌을 때만 캔버스 텍스트를 다시 그린다."""
        key = (label, value, when, vcolor, lcolor, cols, id(self._pal))
        if self._last[idx] == key:
            return
        self._last[idx] = key
        l, v, w = self.items[idx]
        self.cv.itemconfigure(l, text=label, fill=lcolor or self._pal["label"])
        self.cv.itemconfigure(v, text=value, fill=vcolor)
        self.cv.itemconfigure(w, text=when, fill=self._pal["time"])
        y = self._ys[idx]
        self.cv.coords(l, self.PAD, y)
        self.cv.coords(v, cols[0], y)      # 값은 오른쪽 정렬 — 끝이 맞는다
        self.cv.coords(w, cols[1], y)

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
            # 숨어 있는 동안 아래가 바뀌었을 수 있다(전체화면 종료·테마 변경).
            # 창이 아직 안 보이는 지금 찍으면 깜빡임 없이 새 배경을 입는다.
            self._match_background(force=True)
            if not self._mapped:
                self.root.deiconify()   # Tk 상태를 normal로 만드는 건 한 번만
                self._mapped = True
            self._win_show(True)
            self._adopt_by_taskbar()    # 표시 후에 걸어야 Tk가 안 지운다
            self._sync_topmost(raise_now=True)
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
        claude_gone_at = None
        while not self.stop_evt.is_set():
            now = time.time()
            try:
                if claude_running():
                    claude_gone_at = None
                elif claude_gone_at is None:
                    claude_gone_at = now
                elif now - claude_gone_at >= 10:
                    log.info("claude not running - exiting with it")
                    self.q.put(("quit",))
                    break
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
                                 "Claude 사용량", self._build_menu())
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
