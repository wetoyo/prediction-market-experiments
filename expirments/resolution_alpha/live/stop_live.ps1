# Stops runner.py and the pnl_killswitch.sh watcher started by start_live.ps1.
Set-Location -Path $PSScriptRoot

$RunnerPidFile = ".runner.pid"
$KillswitchPidFile = ".killswitch.pid"
$stoppedAnything = $false

if (Test-Path $RunnerPidFile) {
    $runnerPid = Get-Content $RunnerPidFile
    $proc = Get-Process -Id $runnerPid -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $runnerPid -Force
        Write-Host "stopped runner.py (PID $runnerPid)"
        $stoppedAnything = $true
    } else {
        Write-Host "runner.py (PID $runnerPid) was already stopped"
    }
    Remove-Item $RunnerPidFile -ErrorAction SilentlyContinue
} else {
    Write-Host "no runner.pid found -- nothing to stop (was it started with start_live?)"
}

if (Test-Path $KillswitchPidFile) {
    $ksPid = Get-Content $KillswitchPidFile
    $proc = Get-Process -Id $ksPid -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $ksPid -Force
        Write-Host "stopped kill-switch watcher (PID $ksPid)"
        $stoppedAnything = $true
    } else {
        Write-Host "kill-switch watcher (PID $ksPid) was already stopped"
    }
    Remove-Item $KillswitchPidFile -ErrorAction SilentlyContinue
}

if (-not $stoppedAnything) {
    Write-Host "Nothing was running."
}

Read-Host "Press Enter to close this window"
