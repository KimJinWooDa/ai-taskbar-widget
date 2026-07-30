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

Write-Host "[2/4] Windows 시작 프로그램 등록"
New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
$vbs = 'CreateObject("Wscript.Shell").Run """' + $widget + '""", 0, False'
[IO.File]::WriteAllText($startupVbs, $vbs, [Text.Encoding]::Unicode)

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

$json = $settings | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText(
    $settingsPath, $json, (New-Object Text.UTF8Encoding $false)
)

Write-Host "[4/4] 실행"
Start-Process -FilePath $widget

Write-Host ""
Write-Host "설치 완료. 이후 별도 명령 없이 자동 추적됩니다." -ForegroundColor Green
Write-Host "Claude 자동 호출/수동 호출은 정확히 집계하고, Codex 자동 호출은 ~추정으로 표시합니다."
