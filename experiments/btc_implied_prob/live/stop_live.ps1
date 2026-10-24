# Stops the strategy.py loop started by start_live.ps1.
Set-Location -Path $PSScriptRoot

$RunnerPidFile = ".runner.pid"

if (Test-Path $RunnerPidFile) {
    $runnerPid = Get-Content $RunnerPidFile
    $proc = Get-Process -Id $runnerPid -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $runnerPid -Force
        Write-Host "stopped strategy.py (PID $runnerPid)"
    } else {
        Write-Host "strategy.py (PID $runnerPid) was already stopped"
    }
    Remove-Item $RunnerPidFile -ErrorAction SilentlyContinue
} else {
    Write-Host "no .runner.pid found -- nothing to stop (was it started with start_live?)"
}

Read-Host "Press Enter to close this window"
