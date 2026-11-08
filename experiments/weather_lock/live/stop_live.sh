#!/usr/bin/env bash
# Stops the strategy.py loop started by start_live.sh.
cd "$(dirname "${BASH_SOURCE[0]}")"

RUNNER_PID_FILE=".runner.pid"

if [ -f "$RUNNER_PID_FILE" ]; then
    PID=$(cat "$RUNNER_PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "stopped strategy.py (PID $PID)"
    else
        echo "strategy.py (PID $PID) was already stopped"
    fi
    rm -f "$RUNNER_PID_FILE"
else
    echo "no .runner.pid found -- nothing to stop (was it started with start_live.sh?)"
fi
