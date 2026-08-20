#!/usr/bin/env bash
# Starts ../strategy.py --loop in the background (dry-run unless
# BTC_IMPLIED_PROB_DRY_RUN=false and Kalshi creds are set -- see ../config.py),
# logging to logs/. Records its PID so stop_live.sh can find it later.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

RUNNER_PID_FILE=".runner.pid"
LOOP_SECONDS="${BTC_IMPLIED_PROB_LOOP_SECONDS:-60}"

if [ -f "$RUNNER_PID_FILE" ] && kill -0 "$(cat "$RUNNER_PID_FILE")" 2>/dev/null; then
    echo "strategy.py is already running (PID $(cat "$RUNNER_PID_FILE")) -- run stop_live.sh first if you want to restart it."
    exit 1
fi

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Archive every existing per-run log into logs/old/ before starting a new
# run, same as resolution_alpha/live/start_live.ps1 -- safe here
# unconditionally since the guard above already confirmed nothing is
# currently running, so nothing could still be writing to these files.
mkdir -p logs logs/old
shopt -s nullglob
for f in logs/*; do
    [ -f "$f" ] && mv -f "$f" logs/old/
done
shopt -u nullglob

LOG_FILE="logs/live_$(date +%Y%m%d_%H%M%S).log"
nohup "$(command -v python || command -v python3)" ../strategy.py --loop "$LOOP_SECONDS" --execute > "$LOG_FILE" 2>&1 &
RUNNER_PID=$!
echo "$RUNNER_PID" > "$RUNNER_PID_FILE"
disown

sleep 3
if ! kill -0 "$RUNNER_PID" 2>/dev/null; then
    echo "strategy.py exited immediately -- check $LOG_FILE"
    rm -f "$RUNNER_PID_FILE"
    exit 1
fi
echo "strategy.py started, PID $RUNNER_PID, looping every ${LOOP_SECONDS}s, logging to $LOG_FILE"
