# Starts runner.py plus the pnl_killswitch.sh safety watcher as genuinely
# detached Windows processes (Start-Process, not bash's nohup/disown --
# see the 2026-08-07 fix note below for why that mattered) and records both
# PIDs so stop_live can find them later.
#
# Bug this replaces: the original start_live.sh used `nohup ... & disown`
# from a bash console spawned by start_live.bat. On Windows, closing that
# console window can terminate the whole process tree via the console's own
# job-object mechanism, REGARDLESS of nohup/disown -- those only stop the
# shell from delivering SIGHUP, they don't detach the child from Windows'
# own process/job tracking. Symptom seen live: the log file would show
# genuine startup activity (a few ticks) then just stop, with no error --
# consistent with the process being killed externally, not crashing.
# Start-Process (no console window at all) avoids this because the child is
# never part of a console's job object in the first place.

Set-Location -Path $PSScriptRoot

# runner.py and its modules live one level up now (this folder is
# launcher-scripts-only, matching ../../btc_implied_prob/live and
# ../../golf_field_alpha/live). Run "..\runner.py" and put the experiment
# root on PYTHONPATH so the inline equity snippet below (and any child)
# can `import kalshi_gateway` regardless of cwd.
$ExperimentRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$env:PYTHONPATH = $ExperimentRoot

$RunnerPidFile = ".runner.pid"
$KillswitchPidFile = ".killswitch.pid"

if (Test-Path $RunnerPidFile) {
    $existingPid = Get-Content $RunnerPidFile
    $existingProc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
    if ($existingProc) {
        Write-Host "runner.py is already running (PID $existingPid) -- run stop_live first if you want to restart it."
        Read-Host "Press Enter to close this window"
        exit 1
    }
}

# Archive every existing per-run log into logs\old\ before starting a new
# run (2026-08-07, user: "this is getting cluttered"). Safe to do here,
# unconditionally: the guard above already confirmed nothing is currently
# running, so nothing could still be writing to these files. samples.db is
# deliberately excluded -- that's a persistent dataset that accumulates
# across restarts, not a per-run log.
$oldLogDir = "logs\old"
if (-not (Test-Path $oldLogDir)) {
    New-Item -ItemType Directory -Path $oldLogDir | Out-Null
}
# Materialize the file list *before* moving anything -- piping
# Get-ChildItem straight into a Move-Item that empties the same directory
# it's still enumerating is unreliable (verified live: it moved samples.db
# too, despite the exclusion filter, when done as one combined pipeline).
$logsToArchive = Get-ChildItem -Path "logs" -File | Where-Object { $_.Name -ne "samples.db" }
foreach ($file in $logsToArchive) {
    Move-Item -Path $file.FullName -Destination $oldLogDir -Force
}

# Load .env into this process's environment (child processes inherit it).
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
    }
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = "logs\live_$timestamp.log"
# Repo-root virtualenv (created once at the repo root: `python -m venv .venv`
# then `pip install -r requirements.txt`). Three levels up from live\.
$pythonExe = Join-Path $PSScriptRoot "..\..\..\.venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Host "venv python not found at $pythonExe"
    Write-Host "Create it from the repo root:  python -m venv .venv  then  .venv\Scripts\python -m pip install -r expirments\resolution_alpha\requirements.txt"
    Read-Host "Press Enter to close this window"
    exit 1
}
$pythonExe = (Resolve-Path $pythonExe).Path

$runnerProc = Start-Process -FilePath $pythonExe -ArgumentList "..\runner.py" `
    -WindowStyle Hidden -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.stderr" -PassThru
Set-Content -Path $RunnerPidFile -Value $runnerProc.Id -NoNewline

Start-Sleep -Seconds 3
$runnerProc.Refresh()
if ($runnerProc.HasExited) {
    Write-Host "runner.py exited immediately -- check $logFile"
    Remove-Item $RunnerPidFile -ErrorAction SilentlyContinue
    Read-Host "Press Enter to close this window"
    exit 1
}
Write-Host "runner.py started, PID $($runnerProc.Id), logging to $logFile"

# Kill-switch: -10% threshold -- tightened from -15% on 2026-08-10 after a
# real breach (single losing trade, KXNEAR15M-26AUG092045-45, cost ~18.6% of
# a single position and alone pushed equity past -15%; user wanted a
# tighter margin going forward).
#
# Baseline is fetched fresh (current equity), NOT hardcoded to the original
# $100 funding amount -- real incident, 2026-08-10: restarting right after
# the account had fallen to $82.70 with a hardcoded 100 baseline made the
# kill-switch breach and self-kill on its very first check (equity was
# already -17% vs the ORIGINAL baseline before any new activity), which
# would keep the runner permanently unable to start again until the account
# recovered on its own -- exactly backwards from "protect against further
# loss from here." Each restart's baseline is simply wherever the account
# stands at that moment.
#
# PowerShell script (pnl_killswitch.ps1), not the bash one -- switched
# 2026-08-08 after a real incident: runner.py above gets a real Windows PID
# from Start-Process, which bash's own kill/kill-0 (MSYS/Cygwin) cannot see
# or signal at all for a process outside its own fork tree. A bash-armed
# kill-switch against this PID would silently monitor nothing -- confirmed
# live: `kill -0 <that-pid>` returned "No such process" for a runner.py that
# was demonstrably alive and trading for hours, completely unsupervised. See
# pnl_killswitch.ps1's header for the full incident writeup.
$killLogFile = "logs\killswitch_$timestamp.log"

$currentEquity = & $pythonExe -c @"
from kalshi_gateway import KalshiTradingClient
c = KalshiTradingClient()
balance = float(c.get_balance()['balance_dollars'])
positions = c.get_positions()['market_positions']
exposure = sum(float(p['market_exposure_dollars']) for p in positions)
print(round(balance + exposure, 4))
"@ 2>$null
if (-not $currentEquity) {
    Write-Host "failed to fetch current equity for kill-switch baseline -- aborting, runner.py PID $($runnerProc.Id) is still running, stop it manually if needed"
    Read-Host "Press Enter to close this window"
    exit 1
}
Write-Host "kill-switch baseline: current equity `$$currentEquity"

$ksProc = Start-Process -FilePath "powershell.exe" -ArgumentList `
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "pnl_killswitch.ps1", `
    "-RunnerPid", $runnerProc.Id, "-BaselineDollars", $currentEquity, "-ThresholdPct", "-10", `
    "-PythonExe", $pythonExe `
    -WindowStyle Hidden -RedirectStandardOutput $killLogFile -RedirectStandardError "$killLogFile.stderr" -PassThru
Set-Content -Path $KillswitchPidFile -Value $ksProc.Id -NoNewline
Write-Host "kill-switch armed, PID $($ksProc.Id) (baseline `$$currentEquity, threshold -10%)"

Write-Host ""
Write-Host "Both running as detached background processes -- closing this window will NOT stop them."
Write-Host "Run stop_live.bat to stop everything."
Read-Host "Press Enter to close this window"
