@echo off
rem Double-click to start the P2P showcase (sets itself up on first run).
rem Add "reset" to restore the demo story:  START-SHOWCASE.cmd reset
cd /d "%~dp0"
set ARGS=
if /i "%1"=="reset" set ARGS=-Reset
if /i "%1"=="stop" set ARGS=-Stop
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-showcase.ps1" %ARGS%
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-showcase.ps1" %ARGS%
)
if errorlevel 1 pause
