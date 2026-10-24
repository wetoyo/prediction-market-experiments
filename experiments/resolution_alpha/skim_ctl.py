"""Owner controls for resolution_alpha's profit skimming (../shared/profit_skim.py).

Run on the Pi from this directory with the runner venv:

    ../../.venv/bin/python skim_ctl.py status          # reserve, rules, accrued profit, recent skims
    ../../.venv/bin/python skim_ctl.py check-rules     # validate live/profit_skim_rules.json after editing it
    ../../.venv/bin/python skim_ctl.py withdraw 5.00   # BEFORE withdrawing $5.00 on kalshi.com
    ../../.venv/bin/python skim_ctl.py release 2.00    # hand $2.00 of the reserve back to trading

Read-only apart from appending to the inbox file the runner picks up on its
next sync. Places no orders and needs no credentials.
"""

import sys
from pathlib import Path

import config

sys.path.append(str(Path(__file__).resolve().parents[1] / "shared"))
from profit_skim import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli(
        sys.argv[1:],
        rules_path=config.PROFIT_SKIM_RULES_PATH,
        state_path=config.PROFIT_SKIM_STATE_PATH,
        log_path=config.PROFIT_SKIM_LOG_PATH,
        inbox_path=config.PROFIT_SKIM_INBOX_PATH,
        status_path=config.SIM_BANKROLL_STATUS_PATH,
        prog="skim_ctl.py",
    ))
