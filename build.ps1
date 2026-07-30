param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$deps = Join-Path $repo ".build-deps"
$dist = Join-Path $repo "dist"

Write-Host "[1/3] Build dependencies"
if (-not (Test-Path (Join-Path $deps "PyInstaller")) -or
        -not (Test-Path (Join-Path $deps "pystray"))) {
    & $Python -m pip install --target $deps pystray pyinstaller --quiet
    if ($LASTEXITCODE -ne 0) { throw "빌드 의존성 설치 실패 ($LASTEXITCODE)" }
}

$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $deps
try {
    Write-Host "[2/3] AI-Skill-Widget.exe"
    & $Python -m PyInstaller `
        --noconfirm --clean --onefile --noconsole `
        --name "AI-Skill-Widget" `
        --distpath $dist `
        --workpath (Join-Path $repo "build\widget") `
        --specpath (Join-Path $repo "build") `
        --hidden-import "pystray._win32" `
        --hidden-import "PIL._tkinter_finder" `
        (Join-Path $repo "ClaudeUsageWidget.pyw")
    if ($LASTEXITCODE -ne 0) { throw "위젯 EXE 빌드 실패 ($LASTEXITCODE)" }

    Write-Host "[3/3] SkillEventHook.exe"
    & $Python -m PyInstaller `
        --noconfirm --clean --onefile --console `
        --name "SkillEventHook" `
        --distpath $dist `
        --workpath (Join-Path $repo "build\hook") `
        --specpath (Join-Path $repo "build") `
        (Join-Path $repo "hooks\skill-event-hook.py")
    if ($LASTEXITCODE -ne 0) { throw "훅 EXE 빌드 실패 ($LASTEXITCODE)" }
}
finally {
    $env:PYTHONPATH = $oldPythonPath
}

Write-Host "Built:" -ForegroundColor Green
Write-Host "  $(Join-Path $dist 'AI-Skill-Widget.exe')"
Write-Host "  $(Join-Path $dist 'SkillEventHook.exe')"
