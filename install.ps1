# AI Skill Widget installer for Windows 10/11.
# Installs the self-contained app, startup entry, and token-free Claude hooks.
# 가장 쉬운 길: 저장소 폴더의 install.cmd 더블클릭 (Python·빌드 불필요).
#   dist\ 에 직접 빌드한 EXE가 있으면 그걸, 없으면 GitHub 최신 릴리스 EXE를
#   받아 SHA-256을 확인한 뒤 설치한다. -FromRelease 는 dist\ 가 있어도 릴리스를 쓴다.
param(
    [switch]$FromRelease
)
$ErrorActionPreference = "Stop"
# PowerShell 5.1은 진행 막대를 그리느라 내려받기가 몇 배 느려진다
$ProgressPreference = "SilentlyContinue"

trap {
    Write-Host ""
    Write-Host "설치를 끝내지 못했습니다: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "같은 방법으로 다시 실행하면 이어서 설치됩니다 — 설정과 기록은 그대로 남습니다."
    exit 1
}

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $repo "dist"
$widgetSource = Join-Path $dist "AI-Skill-Widget.exe"
$hookSource = Join-Path $dist "SkillEventHook.exe"
$gitHubRepo = "KimJinWooDa/ai-taskbar-widget"

# 받은 EXE 검증 — 잘린 다운로드(크기·MZ)와 바뀐 파일(GitHub가 자산마다
# 주는 SHA-256 digest)을 모두 거른다. 위젯의 자동 업데이트와 같은 기준이다.
function Test-ReleaseExe {
    param([string]$Path, [string]$Digest)
    if ((Get-Item $Path).Length -lt 5000000) {
        throw "내려받은 파일이 너무 작습니다(잘린 다운로드): $Path"
    }
    $fs = [IO.File]::OpenRead($Path)
    try {
        $head = New-Object byte[] 2
        [void]$fs.Read($head, 0, 2)
    }
    finally { $fs.Dispose() }
    if ($head[0] -ne 0x4D -or $head[1] -ne 0x5A) {
        throw "내려받은 파일이 실행 파일이 아닙니다: $Path"
    }
    if ($Digest) {
        $hash = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
        if ("sha256:$hash" -ne $Digest.ToLowerInvariant()) {
            throw "SHA-256이 릴리스 정보와 다릅니다 — 손상됐거나 바뀐 파일이라 설치하지 않습니다"
        }
    }
}

if (-not $FromRelease -and (Test-Path $widgetSource) -and (Test-Path $hookSource)) {
    Write-Host "[0/5] 직접 빌드한 실행 파일 사용 -> $dist"
}
else {
    Write-Host "[0/5] 최신 릴리스 내려받기 (github.com/$gitHubRepo)"
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $headers = @{ "User-Agent" = "ai-taskbar-widget-installer" }
    try {
        $release = Invoke-RestMethod -UseBasicParsing -Headers $headers `
            -Uri "https://api.github.com/repos/$gitHubRepo/releases/latest"
    }
    catch {
        throw "릴리스 정보를 못 읽었습니다($($_.Exception.Message)). 인터넷 연결을 확인하거나, Python 3.10+이 있으면 build.ps1 로 직접 빌드한 뒤 다시 실행하세요."
    }
    $downloadDir = Join-Path $env:TEMP "ai-taskbar-widget-install"
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    $prefix = "https://github.com/$gitHubRepo/releases/download/"
    foreach ($name in @("AI-Skill-Widget.exe", "SkillEventHook.exe")) {
        $asset = @($release.assets | Where-Object { $_.name -eq $name })[0]
        if (-not $asset) { throw "릴리스 $($release.tag_name)에 $name 이 없습니다." }
        $url = [string]$asset.browser_download_url
        if (-not $url.StartsWith($prefix)) { throw "예상 밖의 다운로드 주소라 거부합니다: $url" }
        $out = Join-Path $downloadDir $name
        Write-Host "  $name ($([math]::Round($asset.size / 1MB, 1)) MB)"
        Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $url -OutFile $out
        Test-ReleaseExe -Path $out -Digest ([string]$asset.digest)
    }
    $widgetSource = Join-Path $downloadDir "AI-Skill-Widget.exe"
    $hookSource = Join-Path $downloadDir "SkillEventHook.exe"
    Write-Host "  $($release.tag_name) 확인 완료 (SHA-256 일치)"
}

$installDir = Join-Path $env:LOCALAPPDATA "AI-Skill-Widget"
$widget = Join-Path $installDir "AI-Skill-Widget.exe"
$hook = Join-Path $installDir "SkillEventHook.exe"
$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$startupVbs = Join-Path $startupDir "AI-Skill-Widget.vbs"

# 실행 중이던 EXE는 프로세스가 끝난 뒤에도 파일 핸들이 잠깐 남아, 첫 복사가
# "다른 프로세스가 사용 중" 으로 실패한다(실측). 짧게 재시도해서 넘긴다.
function Copy-FileWithRetry {
    param(
        [string]$Source,
        [string]$Destination
    )
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Copy-Item $Source $Destination -Force -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Milliseconds 300
        }
    }
}

Write-Host "[1/5] 앱 설치 -> $installDir"
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Get-Process -Name "AI-Skill-Widget" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
# 인스턴스가 여러 개면 전부 빠질 때까지 기다린다. 타임아웃은 조용히 넘기고
# 남은 잠금은 아래 재시도 복사에 맡긴다.
Wait-Process -Name "AI-Skill-Widget" -Timeout 10 -ErrorAction SilentlyContinue
# 훅 EXE는 실행 중인 훅이 잡고 있을 수 있다. 카운터 기록이 깨지지 않게
# 죽이지 않고 스스로 끝나기를 기다린다(훅 타임아웃 3초).
Wait-Process -Name "SkillEventHook" -Timeout 10 -ErrorAction SilentlyContinue
Copy-FileWithRetry $widgetSource $widget
Copy-FileWithRetry $hookSource $hook

Write-Host "[2/5] Windows 시작 프로그램 등록 (로그온 예약 작업)"
# 시작프로그램 폴더는 Windows가 수십 초 늦게 실행한다(실측 44초) —
# 로그온 트리거 예약 작업은 로그온 직후 바로 뜬다. 구버전 vbs는 정리한다.
Remove-Item $startupVbs -Force -ErrorAction SilentlyContinue
$taskAction = New-ScheduledTaskAction -Execute $widget
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName "AI Taskbar Widget" -Action $taskAction `
    -Trigger $taskTrigger -Settings $taskSettings -Force | Out-Null

Write-Host "[3/5] Claude Code 스킬 카운터 훅 병합"
$claudeDir = Join-Path $env:USERPROFILE ".claude"
$settingsPath = Join-Path $claudeDir "settings.json"
New-Item -ItemType Directory -Path $claudeDir -Force | Out-Null

if (Test-Path $settingsPath) {
    Copy-Item $settingsPath "$settingsPath.skill-widget.bak" -Force
    try {
        $settings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Claude settings.json이 올바른 JSON이 아닙니다. 수정하지 않았습니다: $settingsPath"
    }
}
else {
    $settings = [PSCustomObject]@{}
}

if (-not $settings.PSObject.Properties["hooks"]) {
    $settings | Add-Member -MemberType NoteProperty -Name "hooks" `
        -Value ([PSCustomObject]@{})
}

$hookCommand = '"' + ($hook -replace '\\', '/') + '" --client claude'

function Add-SkillHook {
    param(
        [string]$EventName,
        [string]$Matcher
    )
    $groups = @()
    $prop = $settings.hooks.PSObject.Properties[$EventName]
    if ($prop) {
        $groups = @($prop.Value)
    }
    $exists = $false
    foreach ($group in $groups) {
        foreach ($handler in @($group.hooks)) {
            if ($handler.command -like "*SkillEventHook*--client claude*") {
                $exists = $true
            }
        }
    }
    if (-not $exists) {
        $handler = [PSCustomObject]@{
            type = "command"
            command = $hookCommand
            timeout = 3
        }
        $group = [PSCustomObject]@{
            matcher = $Matcher
            hooks = @($handler)
        }
        $groups += $group
    }
    $settings.hooks | Add-Member -MemberType NoteProperty -Name $EventName `
        -Value @($groups) -Force
}

Add-SkillHook -EventName "PreToolUse" -Matcher "^Skill$"
Add-SkillHook -EventName "UserPromptExpansion" -Matcher ""

# Claude 세션이 시작되면 위젯도 켠다 — 이미 떠 있으면 예약 작업이 무시되고
# 조용히 끝난다(exit 0). 훅에서 EXE를 직접 실행하면 Claude 데스크톱(MSIX)
# 컨테이너 신원을 상속받아 %APPDATA% 쓰기가 패키지 그림자로 격리되므로
# (알림 읽음 상태 분열 사고), 위에서 등록한 예약 작업을 경유해 띄운다.
$startCommand = 'schtasks /run /tn "AI Taskbar Widget"'
$startGroups = @()
$startProp = $settings.hooks.PSObject.Properties["SessionStart"]
if ($startProp) { $startGroups = @($startProp.Value) }
$startExists = $false
foreach ($group in $startGroups) {
    foreach ($handler in @($group.hooks)) {
        if ($handler.command -like "*AI Taskbar Widget*") { $startExists = $true }
        elseif ($handler.command -like "*AI-Skill-Widget.exe*") {
            # 구버전 훅(EXE 직접 실행)은 새 명령으로 교체한다 — 중복 방지
            $handler.command = $startCommand
            $startExists = $true
        }
    }
}
if (-not $startExists) {
    $startGroups += [PSCustomObject]@{
        matcher = ""
        hooks = @([PSCustomObject]@{
            type = "command"; command = $startCommand; timeout = 5
        })
    }
}
$settings.hooks | Add-Member -MemberType NoteProperty -Name "SessionStart" `
    -Value @($startGroups) -Force

$json = $settings | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText(
    $settingsPath, $json, (New-Object Text.UTF8Encoding $false)
)

Write-Host "[4/5] 루틴 알림 발신 스크립트 설치"
# 예약 작업(루틴)이 결과 한 줄을 남길 때 쓰는 스크립트. 위젯은 그 로그를 읽어
# 안 읽은 알림 개수를 작업표시줄에 띄운다. 루틴 쪽 배선은 사용자가 하며,
# 규약은 README의 "루틴 알림"을 참조한다. 없으면 알림 기능만 조용히 쉰다.
$tasksDir = Join-Path $claudeDir "scheduled-tasks"
$notifySource = Join-Path $repo "notify.ps1"
if (Test-Path $notifySource) {
    New-Item -ItemType Directory -Path $tasksDir -Force | Out-Null
    Copy-Item $notifySource (Join-Path $tasksDir "notify.ps1") -Force
}
else {
    Write-Host "  notify.ps1 이 없어 건너뜁니다 (알림 기능만 비활성)." -ForegroundColor Yellow
}

Write-Host "[5/5] 실행"
# 직접 실행 대신 예약 작업 경유 — 훅과 같은 이유(사용자 신원 보장).
Start-ScheduledTask -TaskName "AI Taskbar Widget"

Write-Host ""
Write-Host "설치 완료. 이후 별도 명령 없이 자동 추적됩니다." -ForegroundColor Green
Write-Host "- 작업표시줄 오른쪽(트레이 옆)에 사용량 바가 나타납니다. 트레이의 Claude 아이콘을 누르면 메뉴가 열립니다."
Write-Host "- 새 버전은 위젯이 알아서 받아 설치하고, 무엇이 바뀌었는지 바의 '업데이트' 패널로 알려 줍니다."
Write-Host "Claude 자동 호출/수동 호출은 정확히 집계하고, Codex 자동 호출은 ~추정으로 표시합니다."
if (-not (Test-Path (Join-Path $claudeDir ".credentials.json"))) {
    Write-Host ""
    Write-Host "참고: Claude Code 로그인 정보가 아직 없습니다 — 터미널에서 claude 를 실행해 로그인하면 사용량이 바로 표시됩니다." -ForegroundColor Yellow
}
