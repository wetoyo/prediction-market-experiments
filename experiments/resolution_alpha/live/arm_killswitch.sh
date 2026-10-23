#!/usr/bin/env bash
# Wrapper that resolution-alpha-killswitch.service execs: resolves the runner's
# PID from systemd, computes a FRESH equity baseline (cash + open-position cost
# basis) the same way start_live.sh does for the manual path, then hands off to
# pnl_killswitch.sh. Keeping the baseline out of the unit file is deliberate --
# a hardcoded one either fires on the first check (if the account has since
# fallen) or, as the unit used to with BASELINE_DOLLARS=100, sits far below the
# real balance and never protects anything.
#
# pnl_killswitch.sh still self-exits when RESOLUTION_ALPHA_DISABLE_KILLSWITCH is
# set, so this wrapper runs but arms nothing in that case.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

RUNNER_PID="$(systemctl show -p MainPID --value resolution-alpha.service 2>/dev/null || true)"
if [ -z "${RUNNER_PID:-}" ] || [ "${RUNNER_PID:-0}" -le 0 ]; then
    echo "arm_killswitch: resolution-alpha.service has no MainPID -- not arming"
    exit 1
fi

PYTHON="${PYTHON:-../../../.venv/bin/python}"
export PYTHONPATH="${PYTHONPATH:-$(cd .. && pwd)}"

BASELINE_DOLLARS="$("$PYTHON" -c '
from kalshi_gateway import KalshiTradingClient
c = KalshiTradingClient()
balance = float(c.get_balance()["balance_dollars"])
positions = c.get_positions()["market_positions"]
exposure = sum(float(p["market_exposure_dollars"]) for p in positions)
print(round(balance + exposure, 4))
' 2>/dev/null || true)"
if [ -z "$BASELINE_DOLLARS" ]; then
    echo "arm_killswitch: could not fetch equity baseline -- not arming"
    exit 1
fi
echo "arm_killswitch: runner_pid=$RUNNER_PID baseline=\$$BASELINE_DOLLARS"

export RUNNER_PID BASELINE_DOLLARS
export THRESHOLD_PCT="${THRESHOLD_PCT:--10}"
exec bash pnl_killswitch.sh
