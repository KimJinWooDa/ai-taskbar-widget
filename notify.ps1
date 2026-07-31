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
#   제목에는 파이프(|)를 쓰지 않는다. 본문에는 써도 된다.
#   기본 위치: %USERPROFILE%\.claude\scheduled-tasks\notifications.log
#   (CLAUDE_NOTIFY_LOG 환경변수로 옮길 수 있다. 위젯도 같은 변수를 본다.)
#
# 이 파일은 UTF-8 BOM으로 저장해야 한다. BOM이 없으면 Windows PowerShell 5.1이
# 한글 주석을 CP949로 오독해 엉뚱한 위치에서 파스 에러가 난다.

param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Message
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

# 1) 기록 먼저 — 알림을 놓치거나 못 띄워도 이건 남는다.
try {
    $dir = Split-Path -Parent $logPath
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force $dir | Out-Null
    }
    Add-Content -Path $logPath -Value "[$stamp] $Title | $Message" -Encoding UTF8
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
