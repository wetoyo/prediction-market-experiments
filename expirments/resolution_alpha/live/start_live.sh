#!/usr/bin/env bash
# Starts runner.py in the background plus the pnl_killswitch.sh safety
# watcher, and records both PIDs so stop_live.sh can find them later.
# Double-click start_live.bat to run this without opening a terminal.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# runner.py and its modules live one level up now (this folder is
# launcher-scripts-only, matching ../../btc_implied_prob/live). Put the
# experiment root on PYTHONPATH so pnl_killswitch.sh's inline equity probe
# can `import kalshi_gateway` from here.
export PYTHONPATH="$(cd .. && pwd)"

# Repo-root virtualenv (created once: `python3 -m venv .venv` at the repo
# root, then `.venv/bin/pip install -r expirments/resolution_alpha/requirements.txt`).
# Three levels up from live/. Falls back to PATH python if it's not there.
PYTHON="../../../.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "warning: repo-root .venv not found at $PYTHON -- falling back to PATH python" >&2
    PYTHON="$(command -v python || command -v python3)"
fi
export PYTHON

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
nohup "$PYTHON" ../runner.py > "$LOG_FILE" 2>&1 &
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
#
# RESOLUTION_ALPHA_DISABLE_KILLSWITCH (from .env): when set truthy
# (1/true/yes/on, case-insensitive) the equity-loss watcher is NOT armed and
# runner.py runs with no automatic stop. Off by default -- opt out
# deliberately. See live/.env.example.
_ks_disabled=$(printf '%s' "${RESOLUTION_ALPHA_DISABLE_KILLSWITCH:-}" | tr '[:upper:]' '[:lower:]')
case "$_ks_disabled" in
    1 | true | yes | on)
        rm -f "$KILLSWITCH_PID_FILE"
        echo "WARNING: kill-switch DISABLED via RESOLUTION_ALPHA_DISABLE_KILLSWITCH -- runner.py (PID $RUNNER_PID) is running UNSUPERVISED with no automatic equity-loss stop."
        ;;
    *)
        RUNNER_PID="$RUNNER_PID" BASELINE_DOLLARS=100 THRESHOLD_PCT=-15 \
            nohup bash pnl_killswitch.sh > "logs/killswitch_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
        KILLSWITCH_PID=$!
        echo "$KILLSWITCH_PID" > "$KILLSWITCH_PID_FILE"
        disown
        echo "kill-switch armed, PID $KILLSWITCH_PID (baseline \$100, threshold -15%)"
        ;;
esac

echo ""
echo "Both running. Close this window freely -- they keep running in the background."
echo "Run stop_live (or stop_live.bat) to stop everything."
read -p "Press Enter to close this window..."
