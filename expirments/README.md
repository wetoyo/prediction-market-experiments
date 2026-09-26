# expirments

Live/paper Kalshi trading strategies. Each subdirectory is a standalone
experiment (own `config.py`, `strategy.py`/`runner.py`, `README.md`):

- `resolution_alpha/` -- crypto interval-market resolution-window strategy.
- `btc_implied_prob/` -- BTC/ETH interval markets priced off Deribit implied vol.
- `golf_field_alpha/` -- golf tournament outright-winner field de-vig.

All three authenticate against the **same Kalshi account**
(`KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`). Each sizes off the whole
account balance today, so don't run more than one live at once yet. The
plan for sharing the account is a per-runner ledger: each runner's
`OrderManager` gets its own slice of the balance and an account-level
reconciler checks the slices sum to reality. resolution_alpha has it
(`resolution_alpha/sim_bankroll.py`, `resolution_alpha/account_reconciler.py`);
see `resolution_alpha/live/SIM_BANKROLL_PLAN.md` for the design and the
steps to port it to the other two.
