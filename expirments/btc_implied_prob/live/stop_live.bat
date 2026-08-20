@echo off
REM Double-click launcher for stop_live.ps1 (switched from stop_live.sh
REM alongside start_live.bat's switch to start_live.ps1 on 2026-08-13 --
REM see start_live.ps1's header for why).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_live.ps1"
