# AI Skill Widget installer for Windows 10/11.
# Installs the self-contained app, startup entry, and token-free Claude hooks.
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $repo "dist"
$widgetSource = Join-Path $dist "AI-Skill-Widget.exe"
$hookSource = Join-Path $dist "SkillEventHook.exe"

if (-not (Test-Path $widgetSource) -or -not (Test-Path $hookSource)) {
    throw "dist 실행 파일이 없습니다. 먼저 .\build.ps1 을 실행하세요."
}

$installDir = Join-Path $env:LOCALAPPDATA "AI-Skill-Widget"
$widget = Join-Path $installDir "AI-Skill-Widget.exe"
$hook = Join-Path $installDir "SkillEventHook.exe"
$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$startupVbs = Join-Path $startupDir "AI-Skill-Widget.vbs"

Write-Host "[1/4] 앱 설치 -> $installDir"
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Get-Process -Name "AI-Skill-Widget" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Copy-Item $widgetSource $widget -Force
Copy-Item $hookSource $hook -Force

Write-Host "[2/4] Windows 시작 프로그램 등록 (로그온 예약 작업)"
# 시작프로그램 폴더는 Windows가 수십 초 늦게 실행한다(실측 44초) —
# 로그온 트리거 예약 작업은 로그온 직후 바로 뜬다. 구버전 vbs는 정리한다.
Remove-Item $startupVbs -Force -ErrorAction SilentlyContinue
$taskAction = New-ScheduledTaskAction -Execute $widget
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName "AI Taskbar Widget" -Action $taskAction `
    -Trigger $taskTrigger -Settings $taskSettings -Force | Out-Null

Write-Host "[3/4] Claude Code 스킬 카운터 훅 병합"
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

# Claude 세션이 시작되면 위젯도 켠다 — 이미 떠 있으면 기존 인스턴스에
# 신호만 보내고 끝난다(단일 인스턴스). cmd start로 분리 실행해 훅은 즉시 끝난다.
$startCommand = 'cmd /c start "" "' + ($widget -replace '\\', '/') + '"'
$startGroups = @()
$startProp = $settings.hooks.PSObject.Properties["SessionStart"]
if ($startProp) { $startGroups = @($startProp.Value) }
$startExists = $false
foreach ($group in $startGroups) {
    foreach ($handler in @($group.hooks)) {
        if ($handler.command -like "*AI-Skill-Widget.exe*") { $startExists = $true }
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

Write-Host "[4/4] 실행"
Start-Process -FilePath $widget

Write-Host ""
Write-Host "설치 완료. 이후 별도 명령 없이 자동 추적됩니다." -ForegroundColor Green
Write-Host "Claude 자동 호출/수동 호출은 정확히 집계하고, Codex 자동 호출은 ~추정으로 표시합니다."
