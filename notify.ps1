# Routine notification writer for AI Taskbar Widget.
#
# 왜 있나: 예약 작업(루틴)은 사람이 안 볼 때 돌고, 그 결과는 놓치기 쉽다.
#          Claude Code의 기본 푸시 알림은 사용자가 앱 앞에 있으면 건너뛰고,
#          토스트는 사라지면 끝이다. 그래서 루틴이 결과를 이 스크립트로 넘기면
#          ① 로그에 한 줄 남기고 ② 윈도우 알림을 띄운다.
#          위젯은 그 로그를 읽어 안 읽은 개수를 작업표시줄에 표시한다.
#
# 쓰는 법 (루틴 지시문 끝에 이 한 줄을 넣는다):
#   powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\scheduled-tasks\notify.ps1" -Title "제목" -Message "한 줄 결과"
#
# 로그 형식 — 위젯이 읽는 계약이다:
#   [yyyy-MM-dd HH:mm:ss] 제목 | 본문
#   [yyyy-MM-dd HH:mm:ss] 제목 | 본문 |run:C:\경로\할것.bat   (-Run 을 준 경우)
#   제목에는 파이프(|)를 쓰지 않는다. 본문에는 써도 된다.
#   기본 위치: %USERPROFILE%\.claude\scheduled-tasks\notifications.log
#   (CLAUDE_NOTIFY_LOG 환경변수로 옮길 수 있다. 위젯도 같은 변수를 본다.)
#
# 이 파일은 UTF-8 BOM으로 저장해야 한다. BOM이 없으면 Windows PowerShell 5.1이
# 한글 주석을 CP949로 오독해 엉뚱한 위치에서 파스 에러가 난다.

param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Message,
    # 사람이 뭔가 실행해야 끝나는 알림이면 그 대상의 전체 경로를 넘긴다.
    # 그러면 위젯의 알림 목록에 [실행] 버튼이 붙는다. 위젯은 '있는 절대경로'만
    # 인정하고 확인을 받은 뒤 탐색기처럼 열 뿐이라, 명령줄은 넘길 수 없다.
    [string]$Run
)

$ErrorActionPreference = 'Continue'

if ($env:CLAUDE_NOTIFY_LOG) {
    $logPath = $env:CLAUDE_NOTIFY_LOG
} elseif ($env:CLAUDE_NOTIFY_DIR) {
    $logPath = Join-Path $env:CLAUDE_NOTIFY_DIR 'notifications.log'
} else {
    $logPath = Join-Path $PSScriptRoot 'notifications.log'
}
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

# 한 줄 = 한 알림이 계약이다. 줄바꿈이 섞이면 가짜 항목이 만들어지고, 제목의
# 파이프는 본문 분리를 어긋나게 한다 — 둘 다 여기서 접는다.
$Title = ($Title -replace '[\r\n]+', ' ').Trim() -replace '\|', '/'
$Message = ($Message -replace '[\r\n]+', ' ').Trim()

# 실행 대상은 맨 끝에 "|run:<경로>"로 붙인다. 경로에는 파이프를 못 쓰므로
# 본문에 파이프가 섞여도 경계가 안 흔들린다.
$runSuffix = ''
if ($Run) {
    $runPath = ($Run -replace '[\r\n|]+', ' ').Trim().Trim('"')
    if ($runPath) {
        if (-not (Test-Path -LiteralPath $runPath)) {
            # 막지는 않는다 — 알림이 온 뒤에 만들어지는 파일도 있다. 다만
            # 위젯은 실행 시점에 없으면 버튼을 안 보여주므로 알려는 준다.
            Write-Output "RUN_TARGET_MISSING: $runPath"
        }
        $runSuffix = " |run:$runPath"
    }
}

# 로그가 무한정 자라지 않게 한다. 위젯은 파일이 바뀔 때마다 전체를 다시 읽으므로
# 길이가 곧 읽기 비용이다. 평상시 알림 빈도로는 몇 년을 써도 안 걸리는 한도지만,
# 고빈도 루틴을 붙인 사용자에게도 상한이 있어야 한다.
$MaxLines = 2000
$KeepLines = 1000

# 1) 기록 먼저 — 알림을 놓치거나 못 띄워도 이건 남는다.
try {
    $dir = Split-Path -Parent $logPath
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force $dir | Out-Null
    }
    Add-Content -Path $logPath -Value "[$stamp] $Title | $Message$runSuffix" -Encoding UTF8

    $lines = @(Get-Content -LiteralPath $logPath -Encoding UTF8)
    if ($lines.Count -gt $MaxLines) {
        $tail = $lines[($lines.Count - $KeepLines)..($lines.Count - 1)]
        Set-Content -LiteralPath $logPath -Value $tail -Encoding UTF8
    }
} catch {
    Write-Output "LOG_FAILED: $($_.Exception.Message)"
}

# 2) 윈도우 알림.
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

    # PowerShell에 이미 등록된 AppUserModelID를 빌려 쓴다 — 별도 앱 등록이 필요 없다.
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

    # 제목·본문에 & < > 가 섞여도 XML이 깨지지 않게.
    $safeTitle = [System.Security.SecurityElement]::Escape($Title)
    $safeMessage = [System.Security.SecurityElement]::Escape($Message)

    # scenario="reminder" — 배너가 저절로 사라지지 않고 사용자가 닫을 때까지 남는다.
    $xml = @"
<toast scenario="reminder">
  <visual>
    <binding template="ToastGeneric">
      <text>$safeTitle</text>
      <text>$safeMessage</text>
    </binding>
  </visual>
  <actions>
    <action content="확인" arguments="dismiss" activationType="background"/>
  </actions>
</toast>
"@

    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    Write-Output 'TOAST_SENT_OK'
} catch {
    Write-Output "TOAST_FAILED: $($_.Exception.Message)"
}
