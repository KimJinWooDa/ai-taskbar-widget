<div align="center">

# AI Taskbar Widget
<img width="314" height="47" alt="image" src="https://github.com/user-attachments/assets/8a472c96-a679-48aa-b1d1-8af1dc2f9bd8" />

**Claude 사용량(세션·주간 잔량)과 Claude Code·Codex 스킬 활동을 한 줄로 보여주는
Windows 작업표시줄 위젯**

[![Platform](https://img.shields.io/badge/Windows_10%2F11-0078d4?style=flat-square)](#)
[![Runtime](https://img.shields.io/badge/Standalone_EXE-no_Python-2ea043?style=flat-square)](#)
[![License](https://img.shields.io/badge/MIT-2ea043?style=flat-square)](LICENSE)

</div>

## 화면 구성

- 바 맨 오른쪽(트레이 쪽)에는 Claude 사용량 패널(세션·주간·모델별 잔량)이
  항상 표시됩니다.
- 그 왼쪽에 실행 중인 앱의 스킬 패널이 붙습니다 — Claude만, Codex만, 또는 둘 다.
- 패널 폭은 내용에 맞춰 계산돼 간격이 균일하고, 앱이 꺼지면 빈자리 없이
  당겨집니다.
- 앱이 모두 꺼지면 바만 숨고 백그라운드에서 다음 실행을 기다립니다.
- 얇은 바에는 앱별 설치 수·상위 스킬 2개만 표시합니다.
- 바를 클릭하거나 트레이 메뉴의 `스킬 사용 내역 열기`를 누르면 전체 목록이
  작업표시줄 위 팝업으로 열립니다.
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

## 토큰과 개인정보

- 훅은 모델을 호출하지 않으며 stdout과 `additionalContext`를 출력하지 않습니다.
- 따라서 추적 때문에 추가되는 모델 입력 토큰은 0입니다.
- Codex는 훅 없이 기존 로컬 세션 JSONL을 증분 읽기합니다.
- 원문 프롬프트와 응답은 저장하지 않습니다.
- 로컬 SQLite에는 스킬명, 앱, 시각, 자동/수동/추정 구분만 기록합니다.
- DB: `%APPDATA%\ClaudeUsageWidget\skill-usage.db`

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
