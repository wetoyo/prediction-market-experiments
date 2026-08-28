#!/bin/bash
# Autonomous kill-switch: tracks account EQUITY against a funded baseline (NOT
# Kalshi's own reported P&L, which is distorted by the deposit itself) and
# kills the live runner.py process if equity drops too far below it.
#
# Usage: RUNNER_PID=<pid> BASELINE_DOLLARS=100 THRESHOLD_PCT=-10 ./pnl_killswitch.sh
# Run from expirments/resolution_alpha/live with .env already sourced.
#
# v2 (2026-08-06): the first version used raw cash balance, which produced a
# false trigger -- this strategy only enters positions in the last
# ENTRY_WINDOW_SECONDS before a market closes, so paying for a sizeable
# position and its settlement often land within seconds of each other. A
# balance sample taken in that gap (cost paid, payout not yet landed) reads
# as a big loss even when every open position goes on to win. Concretely:
# balance dipped to $84.15 (-15.85%) right after two ~$8 positions were
# opened; both won moments later and balance ended at $101.15 (+1.15%) -- the
# kill-switch fired on a real account in the middle of a real gain.
#
# Fix: track EQUITY = cash balance + the cost basis (market_exposure_dollars)
# of still-open positions, not cash alone. Kalshi's market_exposure_dollars
# is cost-basis, not a live mark-to-market of win probability -- it doesn't
# credit an about-to-win position early, but it does stop penalizing the
# balance dip from simply having paid for one. This can very slightly lag a
# real, developing loss (a position that's likely to lose still counts at
# full cost until it actually settles), which is an acceptable trade-off
# given how short this strategy's holding periods are (seconds to low
# minutes) by design. A second layer, REQUIRED_CONSECUTIVE_BREACHES, also
# requires the threshold to be crossed on multiple consecutive checks (not
# one instantaneous sample) before killing, as further protection against
# any remaining timing edge cases or a transient API hiccup.

set -u

RUNNER_PID="${RUNNER_PID:?must set RUNNER_PID}"
BASELINE_DOLLARS="${BASELINE_DOLLARS:-100.0}"
THRESHOLD_PCT="${THRESHOLD_PCT:--10.0}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-30}"
HEARTBEAT_EVERY_N_CHECKS="${HEARTBEAT_EVERY_N_CHECKS:-10}"
REQUIRED_CONSECUTIVE_BREACHES="${REQUIRED_CONSECUTIVE_BREACHES:-2}"
# Inherited from start_live.sh (repo-root .venv); falls back to PATH python.
PYTHON="${PYTHON:-python}"
# The equity probe below does `import kalshi_gateway`, which now lives one
# level up (this folder is launcher-scripts-only). start_live.sh exports this;
# set it for standalone runs too. Assumes cwd is live/ (as when start_live
# arms it) -- run this script from that directory.
export PYTHONPATH="${PYTHONPATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

echo "kill-switch armed (v2, equity-based): runner_pid=${RUNNER_PID} baseline=\$${BASELINE_DOLLARS} threshold=${THRESHOLD_PCT}% consecutive_breaches_required=${REQUIRED_CONSECUTIVE_BREACHES}"

i=0
consecutive_breaches=0
while true; do
    sleep "${CHECK_INTERVAL_SECONDS}"
    i=$((i + 1))

    if ! kill -0 "${RUNNER_PID}" 2>/dev/null; then
        echo "runner.py (PID ${RUNNER_PID}) is no longer running -- kill-switch stopping"
        break
    fi

    EQUITY=$("$PYTHON" -c "
from kalshi_gateway import KalshiTradingClient
c = KalshiTradingClient()
balance = float(c.get_balance()['balance_dollars'])
positions = c.get_positions()['market_positions']
exposure = sum(float(p['market_exposure_dollars']) for p in positions)
print(round(balance + exposure, 4))
" 2>/dev/null)

    if [ -z "${EQUITY}" ]; then
        echo "WARNING: failed to fetch equity on check #${i}, will retry (not counted as a breach)"
        continue
    fi

    PCT=$("$PYTHON" -c "print(round((${EQUITY} - ${BASELINE_DOLLARS}) / ${BASELINE_DOLLARS} * 100, 3))")
    BREACHED=$("$PYTHON" -c "print(1 if (${EQUITY} - ${BASELINE_DOLLARS}) / ${BASELINE_DOLLARS} * 100 <= ${THRESHOLD_PCT} else 0)")

    if [ "${BREACHED}" = "1" ]; then
        consecutive_breaches=$((consecutive_breaches + 1))
        echo "breach ${consecutive_breaches}/${REQUIRED_CONSECUTIVE_BREACHES}: equity=\$${EQUITY} pnl=${PCT}% <= ${THRESHOLD_PCT}% threshold"
        if [ "${consecutive_breaches}" -ge "${REQUIRED_CONSECUTIVE_BREACHES}" ]; then
            echo "AUTO-KILL TRIGGERED: equity=\$${EQUITY} pnl=${PCT}% sustained across ${consecutive_breaches} consecutive checks -- killing runner.py PID ${RUNNER_PID}"
            kill "${RUNNER_PID}"
            sleep 1
            if kill -0 "${RUNNER_PID}" 2>/dev/null; then
                echo "PID ${RUNNER_PID} still alive after kill, sending SIGKILL"
                kill -9 "${RUNNER_PID}"
            fi
            echo "AUTO-KILL COMPLETE: runner.py stopped, equity=\$${EQUITY} pnl=${PCT}%"
            break
        fi
        continue
    fi

    consecutive_breaches=0

    if [ $((i % HEARTBEAT_EVERY_N_CHECKS)) -eq 0 ]; then
        echo "heartbeat: equity=\$${EQUITY} pnl=${PCT}% (kill threshold ${THRESHOLD_PCT}%, needs ${REQUIRED_CONSECUTIVE_BREACHES} consecutive breaches)"
    fi
done
