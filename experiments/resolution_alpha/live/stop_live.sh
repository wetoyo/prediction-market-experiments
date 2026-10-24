#!/usr/bin/env bash
# Stops runner.py and the pnl_killswitch.sh watcher started by start_live.sh.
# Double-click stop_live.bat to run this without opening a terminal.
cd "$(dirname "${BASH_SOURCE[0]}")"

RUNNER_PID_FILE=".runner.pid"
KILLSWITCH_PID_FILE=".killswitch.pid"

stopped_anything=false

if [ -f "$RUNNER_PID_FILE" ]; then
    PID=$(cat "$RUNNER_PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "stopped runner.py (PID $PID)"
        stopped_anything=true
    else
        echo "runner.py (PID $PID) was already stopped"
    fi
    rm -f "$RUNNER_PID_FILE"
else
    echo "no runner.pid found -- nothing to stop (was it started with start_live.sh?)"
fi

if [ -f "$KILLSWITCH_PID_FILE" ]; then
    PID=$(cat "$KILLSWITCH_PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "stopped kill-switch watcher (PID $PID)"
        stopped_anything=true
    else
        echo "kill-switch watcher (PID $PID) was already stopped"
    fi
    rm -f "$KILLSWITCH_PID_FILE"
fi

if [ "$stopped_anything" = false ]; then
    echo "Nothing was running."
fi

read -p "Press Enter to close this window..."
