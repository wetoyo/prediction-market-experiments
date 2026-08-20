#!/usr/bin/env bash
# Starts runner.py in the background plus the pnl_killswitch.sh safety
# watcher, and records both PIDs so stop_live.sh can find them later.
# Double-click start_live.bat to run this without opening a terminal.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

RUNNER_PID_FILE=".runner.pid"
KILLSWITCH_PID_FILE=".killswitch.pid"

if [ -f "$RUNNER_PID_FILE" ] && kill -0 "$(cat "$RUNNER_PID_FILE")" 2>/dev/null; then
    echo "runner.py is already running (PID $(cat "$RUNNER_PID_FILE")) -- run stop_live first if you want to restart it."
    exit 1
fi

set -a
source .env
set +a

LOG_FILE="logs/live_$(date +%Y%m%d_%H%M%S).log"
nohup "$(command -v python || command -v python3)" runner.py > "$LOG_FILE" 2>&1 &
RUNNER_PID=$!
echo "$RUNNER_PID" > "$RUNNER_PID_FILE"
disown

sleep 3
if ! kill -0 "$RUNNER_PID" 2>/dev/null; then
    echo "runner.py exited immediately -- check $LOG_FILE"
    rm -f "$RUNNER_PID_FILE"
    exit 1
fi
echo "runner.py started, PID $RUNNER_PID, logging to $LOG_FILE"

# Kill-switch: same $100 baseline / -15% threshold used throughout this
# session -- adjust here if the funded balance ever changes.
RUNNER_PID="$RUNNER_PID" BASELINE_DOLLARS=100 THRESHOLD_PCT=-15 \
    nohup bash pnl_killswitch.sh > "logs/killswitch_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
KILLSWITCH_PID=$!
echo "$KILLSWITCH_PID" > "$KILLSWITCH_PID_FILE"
disown
echo "kill-switch armed, PID $KILLSWITCH_PID (baseline \$100, threshold -15%)"

echo ""
echo "Both running. Close this window freely -- they keep running in the background."
echo "Run stop_live (or stop_live.bat) to stop everything."
read -p "Press Enter to close this window..."
