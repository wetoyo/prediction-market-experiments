@echo off
REM Double-click launcher for start_live.ps1. See
REM ../../btc_implied_prob/live/start_live.ps1's header for why this wraps
REM the PowerShell script rather than a bash one on Windows.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_live.ps1"
