<div align="center">

# AI Taskbar Widget
<img width="314" height="47" alt="image" src="https://github.com/user-attachments/assets/8a472c96-a679-48aa-b1d1-8af1dc2f9bd8" />

**Claude 사용량(세션·주간 잔량)과 Codex 주간 사용량을 한 줄로 보여주고,
Claude Code·Codex 스킬 활동은 팝업으로 확인하는 Windows 작업표시줄 위젯**

[![Platform](https://img.shields.io/badge/Windows_10%2F11-0078d4?style=flat-square)](#)
[![Runtime](https://img.shields.io/badge/Standalone_EXE-no_Python-2ea043?style=flat-square)](#)
[![License](https://img.shields.io/badge/MIT-2ea043?style=flat-square)](LICENSE)

</div>

## 화면 구성

- 바 맨 오른쪽(트레이 쪽)에는 Claude 사용량 패널(세션·주간·모델별 잔량)이
  항상 표시됩니다.
- **Codex가 실행 중일 때만** 그 왼쪽에 Codex 사용량 패널이 붙습니다(일간·
  주간 창이 계정에 있는 만큼만, 보통 주간 한 줄). 값은 Codex 로그인
  (auth.json)으로 **공식 사용량을 직접 조회**해 Codex 앱의 "남은 사용량"과
  같은 원천입니다 — 조회가 안 되면(오프라인·토큰 만료) 세션 로그의 마지막
  기록으로 폴백하며, 이 값은 과거 기록이라 실제와 다를 수 있습니다.
- 표시는 Claude와 같은 **"쓴 비율"**입니다 — Codex 앱의 "남은 100%"는
  위젯에서 0%로 보입니다.
- 안 읽은 **루틴 알림**이 있을 때만 **맨 왼쪽**에 알림 패널이 나타납니다. 바는
  오른쪽 끝이 앵커라, 알림이 뜨고 사라져도 사용량 패널은 제자리에 있고
  바가 바깥쪽으로만 늘었다 줄어듭니다. → [루틴 알림](#루틴-알림)
- 패널 폭은 내용에 맞춰 계산돼 간격이 균일합니다.
- 스킬 활동은 바에 표시하지 않습니다. 바를 클릭하거나 트레이 메뉴의
  `스킬 사용 내역 열기`를 누르면 전체 목록이 작업표시줄 위 팝업으로 열립니다.
- `바 위치 잠금`이 켜져 클릭이 통과하는 상태에서는 트레이 메뉴로 엽니다.

Windows의 기본 `숨겨진 아이콘(^)` 창 안에는 임의의 목록 UI를 안전하게 삽입할
수 없어서, 트레이 아이콘이 여는 전용 팝업으로 같은 사용 흐름을 구현했습니다.

## 집계 기준

| 앱 | 수동 호출 | 자동 호출 |
|---|---|---|
| Claude Code | `/skill` 직접 실행을 정확히 집계 | `Skill` 도구 호출을 정확히 집계 |
| Codex | `$skill` 입력을 집계 | 로컬 세션에서 `SKILL.md` 로드를 감지해 `~추정` 표시 |

Codex에는 아직 전용 `PreSkillUse` 훅이 없으므로 자동/수동을 모두 정확하게
구분한다고 가장하지 않습니다. 전용 이벤트가 추가되면 추정 어댑터만 교체하면 됩니다.

## 루틴 알림

예약 작업(루틴)은 사람이 안 볼 때 돌고, 그 결과는 놓치기 쉽습니다. Claude Code의
기본 푸시 알림은 **사용자가 앱 앞에 있으면 건너뛰고**, 토스트는 사라지면 끝입니다.
그래서 위젯은 "안 읽은 결과가 몇 건 있는지"를 작업표시줄에 띄웁니다.

- 안 읽은 알림이 **0건이면 패널을 아예 그리지 않습니다.** 빈 아이콘이 자리를
  차지하지 않고, 나머지 패널이 빈틈없이 당겨집니다.
- 패널을 클릭하면 최근 100건이 최신순으로 열리고, **여는 순간 읽음 처리**되어
  패널이 사라집니다. 바가 잠겨 있으면 트레이 메뉴의 `루틴 알림 열기`를 씁니다.
- 목록을 띄운 뒤 창이 열리는 사이에 도착한 알림은 **읽음 처리하지 않습니다.**
- 목록의 `모두 지우기`는 **위젯의 표시 상태만 지웁니다** — 루틴이 쓰는 기록
  파일은 그대로 남고, 이후에 오는 알림은 평소처럼 쌓입니다.

### 규약 (한 줄)

루틴은 결과 한 줄을 로그에 덧붙이기만 하면 됩니다. 위젯은 그 파일만 읽습니다.

```
[yyyy-MM-dd HH:mm:ss] 제목 | 본문
[yyyy-MM-dd HH:mm:ss] 제목 | 본문 |run:C:\경로\할것.bat
```

둘째 줄처럼 맨 끝에 `|run:<전체 경로>`를 붙이면 그 알림에만 **[실행] 버튼**이
생깁니다 — 사람이 뭔가 눌러야 끝나는 루틴에 씁니다. 위젯은 **디스크에 실제로
있는 절대경로 하나만** 인정하고, 누르면 전체 경로를 보여주며 확인을 받은 뒤
탐색기에서 더블클릭한 것과 같은 방식으로 엽니다. **명령줄은 받지 않습니다** —
인자·파이프·리다이렉션이 낄 자리가 없고, 없는 파일이면 버튼이 아예 안 생깁니다.

| | 기본값 | 바꾸는 법 |
|---|---|---|
| 로그 | `%USERPROFILE%\.claude\scheduled-tasks\notifications.log` | `CLAUDE_NOTIFY_LOG` |
| 폴더 | `%USERPROFILE%\.claude\scheduled-tasks` | `CLAUDE_NOTIFY_DIR` |
| 읽음 상태 | `%APPDATA%\ClaudeUsageWidget\notifications-read.json` | `CLAUDE_NOTIFY_STATE` |

제목에는 파이프(`|`)를 쓰지 않습니다. 본문에는 써도 됩니다. 형식이 안 맞는 줄도
버리지 않고 본문으로 표시합니다. 로그가 없으면 알림 기능만 조용히 쉽니다.

**알림 본문에 비밀값(토큰·API 키·비밀번호)을 넣지 마세요.** 로그는 평문이고
지울 때까지 남습니다. 위젯은 이 파일을 **읽기만** 하고 쓰지도 지우지도 않으며,
어디로도 보내지 않습니다 — 알림 기능이 추가하는 네트워크 호출은 0건입니다.

`notify.ps1`은 제목·본문의 줄바꿈을 공백으로 접고 제목의 파이프를 `/`로 바꿉니다.
"한 줄 = 한 알림" 계약을 지켜, 본문에 로그 한 줄을 흉내 낸 문자열이 들어와도
가짜 항목이 만들어지지 않습니다. 로그가 2000줄을 넘으면 최근 1000줄만 남깁니다
(파일이 바뀔 때마다 위젯이 전체를 다시 읽으므로 길이가 곧 읽기 비용입니다).

### 발신 쪽 — `notify.ps1`

설치 프로그램이 `%USERPROFILE%\.claude\scheduled-tasks\notify.ps1`에 넣어 둡니다.
로그에 한 줄 남기고 윈도우 알림까지 띄웁니다. 루틴 지시문 끝에 이 한 줄을
넣으면 됩니다:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\scheduled-tasks\notify.ps1" -Title "제목" -Message "한 줄 결과"
```

배너는 `scenario="reminder"`라 사용자가 닫을 때까지 남습니다. 알림을 놓쳐도
로그와 위젯 배지에는 남으므로 결과가 사라지지 않습니다. 직접 파일에 덧붙여도
되고, 다른 언어로 써도 됩니다 — 위젯이 보는 것은 로그 한 줄뿐입니다.

## 토큰과 개인정보

- 훅은 모델을 호출하지 않으며 stdout과 `additionalContext`를 출력하지 않습니다.
- 따라서 추적 때문에 추가되는 모델 입력 토큰은 0입니다.
- Codex는 훅 없이 기존 로컬 세션 JSONL을 증분 읽기합니다.
- 원문 프롬프트와 응답은 저장하지 않습니다.
- 로컬 SQLite에는 스킬명, 앱, 시각, 자동/수동/추정 구분만 기록합니다.
- DB: `%APPDATA%\ClaudeUsageWidget\skill-usage.db`
- 루틴 알림은 로컬 로그 파일만 읽습니다. 위젯이 그 파일을 쓰거나 지우지 않고,
  내용을 어디로도 보내지 않습니다.
- Codex 사용량은 Codex 로그인 토큰으로 `chatgpt.com`의 공식 사용량 API만
  읽기 조회합니다 — 다른 데이터를 보내지 않고, 토큰을 저장하거나 다른 곳으로
  전송하지 않습니다.

## 설치

이 저장소에서 이미 빌드한 `dist` 폴더가 있다면:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

설치 프로그램이 EXE를 `%LOCALAPPDATA%\AI-Skill-Widget`에 복사하고, Windows
자동 시작과 Claude 훅을 기존 설정에 병합합니다. Python은 필요 없습니다.
기존 `~/.claude/settings.json`은 `settings.json.skill-widget.bak`으로 백업합니다.

제거:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\uninstall.ps1
```

제거 시 호출 기록 DB는 보존합니다.

## 개발 빌드

Python 3.10+ 환경에서:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build.ps1
```

생성물:

- `dist\AI-Skill-Widget.exe`
- `dist\SkillEventHook.exe`

테스트:

```powershell
python -m unittest discover -s tests -v
```

## 참고한 구현

- [CodexBar](https://github.com/steipete/CodexBar): Codex 사용량 조회 경로
  (auth.json 토큰 → `wham/usage`)
- [SkillsBar](https://github.com/amandeepmittal/skillsbar): Claude/Codex 스킬
  인벤토리와 호출 통계 구조
- [claude-skills-management](https://github.com/hardness1020/claude-skills-management):
  Claude `PreToolUse` 기반 로컬 집계
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Codex hooks](https://learn.chatgpt.com/docs/hooks)

기존 `claude-taskbar-widget`의 작업표시줄 결합·배경 위장·전체화면 처리 코드를
그대로 보존해 확장했습니다.

---

Anthropic 및 OpenAI와 무관한 비공식 도구입니다. [MIT License](LICENSE)
