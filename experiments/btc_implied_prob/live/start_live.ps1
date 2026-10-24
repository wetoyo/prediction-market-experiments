# Starts ../strategy.py --loop as a genuinely detached Windows process
# (Start-Process, not bash's nohup/disown) and records its PID so
# stop_live.ps1 can find it later.
#
# Bug this replaces: start_live.sh used `nohup ... & disown` from a bash
# console spawned by start_live.bat. On Windows, closing that console window
# can terminate the whole process tree via the console's own job-object
# mechanism, REGARDLESS of nohup/disown -- those only stop the shell from
# delivering SIGHUP, they don't detach the child from Windows' own
# process/job tracking. Symptom seen live, 2026-08-13: the log file showed
# one line of startup activity then just stopped, with no error -- consistent
# with the process being killed externally, not crashing. Same root cause as
# resolution_alpha/live's original start_live.sh bug (see that folder's
# start_live.ps1 header for the full incident writeup) -- this strategy just
# hadn't hit it yet at the time start_live.bat's comment was written.
# Start-Process (no console window at all) avoids this because the child is
# never part of a console's job object in the first place.

Set-Location -Path $PSScriptRoot

$RunnerPidFile = ".runner.pid"

if (Test-Path $RunnerPidFile) {
    $existingPid = Get-Content $RunnerPidFile
    $existingProc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
    if ($existingProc) {
        Write-Host "strategy.py is already running (PID $existingPid) -- run stop_live first if you want to restart it."
        Read-Host "Press Enter to close this window"
        exit 1
    }
}

# Archive every existing per-run log into logs\old\ before starting a new
# run, same as resolution_alpha/live/start_live.ps1 -- safe here
# unconditionally since the guard above already confirmed nothing is
# currently running, so nothing could still be writing to these files.
$oldLogDir = "logs\old"
if (-not (Test-Path "logs")) {
    New-Item -ItemType Directory -Path "logs" | Out-Null
}
if (-not (Test-Path $oldLogDir)) {
    New-Item -ItemType Directory -Path $oldLogDir | Out-Null
}
$logsToArchive = Get-ChildItem -Path "logs" -File
foreach ($file in $logsToArchive) {
    Move-Item -Path $file.FullName -Destination $oldLogDir -Force
}

# Load .env into this process's environment (child processes inherit it).
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
    }
}

$loopSeconds = $env:BTC_IMPLIED_PROB_LOOP_SECONDS
if (-not $loopSeconds) {
    $loopSeconds = 60
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = "logs\live_$timestamp.log"
$pythonExe = "C:\Users\wesle\AppData\Local\Programs\Python\Python310\python.exe"

$runnerProc = Start-Process -FilePath $pythonExe -ArgumentList "..\strategy.py", "--loop", $loopSeconds, "--execute" `
    -WindowStyle Hidden -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.stderr" -PassThru
Set-Content -Path $RunnerPidFile -Value $runnerProc.Id -NoNewline

Start-Sleep -Seconds 3
$runnerProc.Refresh()
if ($runnerProc.HasExited) {
    Write-Host "strategy.py exited immediately -- check $logFile"
    Remove-Item $RunnerPidFile -ErrorAction SilentlyContinue
    Read-Host "Press Enter to close this window"
    exit 1
}
Write-Host "strategy.py started, PID $($runnerProc.Id), looping every ${loopSeconds}s, logging to $logFile"
Write-Host ""
Write-Host "Running as a detached background process -- closing this window will NOT stop it."
Write-Host "Run stop_live.bat to stop it."
Read-Host "Press Enter to close this window"
