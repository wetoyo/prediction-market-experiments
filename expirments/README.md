# expirments

Live/paper Kalshi trading strategies. Each subdirectory is a standalone
experiment (own `config.py`, `strategy.py`/`runner.py`, `README.md`):

- `resolution_alpha/` -- crypto interval-market resolution-window strategy.
- `btc_implied_prob/` -- BTC/ETH interval markets priced off Deribit implied vol.
- `golf_field_alpha/` -- golf tournament outright-winner field de-vig.

All three authenticate against the **same Kalshi account**
(`KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`). They share it through a
per-runner ledger: each runner's `OrderManager` keeps its own slice of the
balance (cash, positions, fills, settlements), tags its orders
(`client_order_id` = `<tag>-<y|n>-<hex>`: `ra`, `bip`, `gfa`), and an
account-level reconciler checks the slices sum to the real balance.

- `shared/sim_bankroll.py` -- the ledger itself (pure, no network), used by all three.
- `shared/tagged_ledger.py` -- its wiring for runners whose GTC orders can
  rest and fill later (btc_implied_prob, golf_field_alpha). resolution_alpha
  (IOC only) books fills from the POST response in its own `order_manager.py`.
- `resolution_alpha/account_reconciler.py` -- the account-level check, `--ledger` per runner.

Every runner still defaults to sizing off the **whole** balance with the
ledger in shadow, so don't run more than one live at once until each is set
up for a shared account. How: `resolution_alpha/live/SIM_BANKROLL_PLAN.md`,
"Running the other two experiments beside resolution_alpha"; current status and next steps:
`MULTI_RUNNER_HANDOFF.md`. Tests:
`python -m pytest shared/tests resolution_alpha/tests`, plus
`python test_ledger_wiring.py` in each of the other two.
