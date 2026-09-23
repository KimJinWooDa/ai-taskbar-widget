@echo off
rem One-click installer - double-click this file after cloning (or unzipping) the repo.
rem No Python needed: install.ps1 downloads the latest release EXE and checks its SHA-256.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "code=%errorlevel%"
echo.
pause
exit /b %code%
