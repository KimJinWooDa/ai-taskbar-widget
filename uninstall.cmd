@echo off
rem One-click uninstaller - removes the app, startup task and Claude hooks (keeps the usage DB).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
set "code=%errorlevel%"
echo.
pause
exit /b %code%
