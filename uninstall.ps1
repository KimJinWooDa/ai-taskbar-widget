$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:LOCALAPPDATA "AI-Skill-Widget"
$startupVbs = Join-Path $env:APPDATA `
    "Microsoft\Windows\Start Menu\Programs\Startup\AI-Skill-Widget.vbs"
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"

Get-Process -Name "AI-Skill-Widget" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item $startupVbs -Force -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "AI Taskbar Widget" `
    -Confirm:$false -ErrorAction SilentlyContinue

if (Test-Path $settingsPath) {
    $settings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.PSObject.Properties["hooks"]) {
        foreach ($eventName in @("PreToolUse", "UserPromptExpansion",
                                 "SessionStart")) {
            $prop = $settings.hooks.PSObject.Properties[$eventName]
            if (-not $prop) { continue }
            $kept = @()
            foreach ($group in @($prop.Value)) {
                $handlers = @($group.hooks | Where-Object {
                    $_.command -notlike "*SkillEventHook*--client claude*" -and
                    $_.command -notlike "*AI-Skill-Widget.exe*"
                })
                if ($handlers.Count) {
                    $group.hooks = $handlers
                    $kept += $group
                }
            }
            $settings.hooks | Add-Member -MemberType NoteProperty `
                -Name $eventName -Value @($kept) -Force
        }
        $json = $settings | ConvertTo-Json -Depth 30
        [IO.File]::WriteAllText(
            $settingsPath, $json, (New-Object Text.UTF8Encoding $false)
        )
    }
}

if (([IO.Path]::GetFullPath($installDir)) -ne
        ([IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "AI-Skill-Widget")))) {
    throw "제거 경로 검증 실패: $installDir"
}
if (Test-Path $installDir) {
    Remove-Item $installDir -Recurse -Force
}

Write-Host "앱, 시작 프로그램, Claude 훅을 제거했습니다."
Write-Host "사용 기록 DB는 %APPDATA%\ClaudeUsageWidget\skill-usage.db 에 남겨뒀습니다."
