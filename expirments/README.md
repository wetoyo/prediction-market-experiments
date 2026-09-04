# expirments

Live/paper Kalshi trading strategies. Each subdirectory is a standalone
experiment (own `config.py`, `strategy.py`/`runner.py`, `README.md`):

- `resolution_alpha/` -- crypto interval-market resolution-window strategy.
- `btc_implied_prob/` -- BTC/ETH interval markets priced off Deribit implied vol.
- `golf_field_alpha/` -- golf tournament outright-winner field de-vig.

All three authenticate against the **same Kalshi account**
(`KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`). If you're running more
than one of them live at the same time, read
`../prediction_market_scraper/Clients/Kalshi/README.md` first -- it's the
shared coordination layer (capital allocation + cross-process API pacing,
`kalshi_state.py`) every experiment's `order_manager.py` routes through, and
it's also the reference for wiring a new experiment into the same account
safely.
