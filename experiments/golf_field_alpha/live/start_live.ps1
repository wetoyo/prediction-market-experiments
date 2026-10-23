# Starts ../strategy.py --loop --execute as a detached Windows process
# (Start-Process, not bash nohup/disown -- see
# ../../btc_implied_prob/live/start_live.ps1's header for the Windows
# console-job-object incident this avoids) and records its PID.

Set-Location -Path $PSScriptRoot

$RunnerPidFile = ".runner.pid"

if (Test-Path $RunnerPidFile) {
    $existingPid = Get-Content $RunnerPidFile
    $existingProc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
    if ($existingProc) {
        Write-Host "strategy.py is already running (PID $existingPid) -- run stop_live first."
        Read-Host "Press Enter to close this window"
        exit 1
    }
}

$oldLogDir = "logs\old"
if (-not (Test-Path "logs")) { New-Item -ItemType Directory -Path "logs" | Out-Null }
if (-not (Test-Path $oldLogDir)) { New-Item -ItemType Directory -Path $oldLogDir | Out-Null }
foreach ($file in Get-ChildItem -Path "logs" -File) {
    Move-Item -Path $file.FullName -Destination $oldLogDir -Force
}

if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
        }
    }
}

$loopSeconds = $env:GOLF_FIELD_ALPHA_LOOP_SECONDS
if (-not $loopSeconds) { $loopSeconds = 300 }

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
Write-Host "Detached background process -- closing this window will NOT stop it. Run stop_live.bat to stop it."
Read-Host "Press Enter to close this window"
