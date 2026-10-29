@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-local.ps1" %*
set "stopExitCode=%errorlevel%"
echo.
pause
exit /b %stopExitCode%
