@echo off
REM Double-click launcher for start_live.ps1. Switched from wrapping
REM start_live.sh via Git Bash on 2026-08-13 after hitting live the same
REM failure resolution_alpha/live's .bat header already warned about: bash's
REM nohup/disown does not survive this console window closing on Windows
REM (Start-Process does) -- see start_live.ps1's header for the full
REM incident writeup.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_live.ps1"
