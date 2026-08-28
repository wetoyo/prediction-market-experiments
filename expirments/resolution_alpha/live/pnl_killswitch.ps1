# PowerShell port of pnl_killswitch.sh, added 2026-08-08.
#
# Why this exists alongside the bash version: start_live.ps1 launches
# runner.py via Start-Process, which gives it a real Windows PID. Bash's own
# `kill`/`kill -0` (MSYS/Cygwin) operate on a different, emulated PID
# namespace for anything outside their own fork tree and simply cannot see
# or signal a Start-Process-launched process -- confirmed live, 2026-08-08:
# `kill -0 <real-pid>` returned "No such process" for a runner.py that was
# demonstrably alive and trading. A kill-switch armed via
# `bash pnl_killswitch.sh` against such a PID would silently monitor
# nothing: its liveness check would immediately report the runner dead (or
# never resolve at all), and its kill action could never actually reach the
# real process. Real incident: exactly this happened -- a live run traded
# for hours with no working kill-switch, undetected until an unrelated
# investigation stumbled onto it. PowerShell's own Get-Process/Stop-Process
# operate on real Windows PIDs directly and don't have this gap, so this
# script -- not the bash one -- is what start_live.ps1 now arms.
#
# Same logic as pnl_killswitch.sh otherwise: track account EQUITY (balance +
# open-position cost basis, not Kalshi's own P&L figure -- see that script's
# header comment for why raw balance alone false-triggers), kill runner.py
# if equity drops too far below a funded baseline, sustained across
# REQUIRED_CONSECUTIVE_BREACHES consecutive checks.
#
# Usage: .\pnl_killswitch.ps1 -RunnerPid <pid> [-BaselineDollars 100] [-ThresholdPct -10]
# Run from expirments/resolution_alpha/live with .env already loaded into
# this process's environment (start_live.ps1 does this before launching).

param(
    [Parameter(Mandatory = $true)][int]$RunnerPid,
    [double]$BaselineDollars = 100.0,
    [double]$ThresholdPct = -10.0,
    [int]$CheckIntervalSeconds = 30,
    [int]$HeartbeatEveryNChecks = 10,
    [int]$RequiredConsecutiveBreaches = 2,
    [string]$PythonExe = (Join-Path $PSScriptRoot "..\..\..\.venv\Scripts\python.exe")
)

Set-Location -Path $PSScriptRoot

# The equity-probe snippet below does `import kalshi_gateway`, which now
# lives one level up (this folder is launcher-scripts-only). start_live.ps1
# exports PYTHONPATH before arming this; set it here too for standalone runs.
if (-not $env:PYTHONPATH) {
    $env:PYTHONPATH = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

Write-Host "kill-switch armed (PowerShell, equity-based): runner_pid=$RunnerPid baseline=`$$BaselineDollars threshold=$ThresholdPct% consecutive_breaches_required=$RequiredConsecutiveBreaches"

$i = 0
$consecutiveBreaches = 0

while ($true) {
    Start-Sleep -Seconds $CheckIntervalSeconds
    $i++

    $proc = Get-Process -Id $RunnerPid -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "runner.py (PID $RunnerPid) is no longer running -- kill-switch stopping"
        break
    }

    $equityRaw = & $PythonExe -c @"
from kalshi_gateway import KalshiTradingClient
c = KalshiTradingClient()
balance = float(c.get_balance()['balance_dollars'])
positions = c.get_positions()['market_positions']
exposure = sum(float(p['market_exposure_dollars']) for p in positions)
print(round(balance + exposure, 4))
"@ 2>$null

    $equity = 0.0
    $parsedOk = [double]::TryParse($equityRaw, [ref]$equity)
    if (-not $parsedOk) {
        Write-Host "WARNING: failed to fetch equity on check #$i, will retry (not counted as a breach)"
        continue
    }

    $pct = [Math]::Round((($equity - $BaselineDollars) / $BaselineDollars) * 100, 3)
    $breached = (($equity - $BaselineDollars) / $BaselineDollars) * 100 -le $ThresholdPct

    if ($breached) {
        $consecutiveBreaches++
        Write-Host "breach ${consecutiveBreaches}/${RequiredConsecutiveBreaches}: equity=`$$equity pnl=$pct% <= $ThresholdPct% threshold"
        if ($consecutiveBreaches -ge $RequiredConsecutiveBreaches) {
            Write-Host "AUTO-KILL TRIGGERED: equity=`$$equity pnl=$pct% sustained across $consecutiveBreaches consecutive checks -- killing runner.py PID $RunnerPid"
            Stop-Process -Id $RunnerPid -Force -ErrorAction SilentlyContinue
            Write-Host "AUTO-KILL COMPLETE: runner.py stopped, equity=`$$equity pnl=$pct%"
            break
        }
        continue
    }

    $consecutiveBreaches = 0

    if ($i % $HeartbeatEveryNChecks -eq 0) {
        Write-Host "heartbeat: equity=`$$equity pnl=$pct% (kill threshold $ThresholdPct%, needs $RequiredConsecutiveBreaches consecutive breaches)"
    }
}
