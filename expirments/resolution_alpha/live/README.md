# Live system (outline — no code yet)

Runtime component of [resolution_alpha](../README.md). Runs synced to the market interval grid (currently targeting 15m boundaries, extensible to 30m/60m series once validated) and trades the strong side across whatever crypto markets are open at that moment — markets come from the discovery step, not a hardcoded list.

## Cycle structure

Runs on a clock aligned to Kalshi's interval boundaries (`:00/:15/:30/:45` for 15m series), not a fixed polling loop, so behavior stays synced to when markets actually open/close rather than drifting.

1. **T-15m (interval open):** run discovery — `fetch_series` / `fetch_markets` (see main README's Market discovery section) to get the full list of currently-open recurring crypto strike markets and their `close_time`. Build/refresh the active-market registry for this interval.
2. **T-15m → T-90s:** idle/monitor only. Track spot reference feed per underlying, but no trading — outside the validated entry window.
3. **T-90s → T-0 (entry window):** for each active market:
   - Compute live `z` (distance-to-strike in std devs, per main README's probability model).
   - Pull current order book for the market.
   - If `|z|` exceeds the confidence threshold AND walking the book shows enough depth at an acceptable price to fill target size AND `model_prob - fill_price > edge_threshold` (net of fees) → submit order on the favored side.
   - Re-evaluate each tick (need to define tick cadence — likely every websocket book update, or a fixed sub-second poll) since z and book depth both move fast in this window.
4. **T-0 (resolution):** no action needed — positions ride to settlement. Log resolution outcome per market for tracking.
5. **Post-resolution:** reconcile fills vs. resolution outcome, update running P&L / edge-realized tracking, roll into next cycle's discovery.

## Components (proposed, not yet built)

- `discovery.py` — wraps `fetch_series`/`fetch_markets` to build the active-market registry each cycle.
- `spot_feed.py` — subscribes to reference price feed(s) per underlying (needs a source decision — see Open questions in main README about matching Kalshi's actual settlement index).
- `orderbook.py` — wraps `live_datastream.stream_orderbook` (needs `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` configured — currently unset in this project) and maintains a local book state per subscribed market.
- `probability_model.py` — shared with backtest; takes (spot, strike, time_to_expiry, vol) → resolution probability. Should be the *same* code path as the backtest model, not a reimplementation, so live behavior matches what was validated.
- `fill_sim.py` / `execution.py` — walks the live book to decide fillable size/price before submitting; shared logic with backtest's fill modeling where possible.
- `order_manager.py` — places/tracks/cancels orders via Kalshi's authenticated REST/trade endpoints (not yet integrated anywhere in this repo — will need order-placement auth, separate from the read-only market-listing calls already in `fetch_historical.py`).
- `scheduler.py` — drives the cycle clock described above, aligned to interval boundaries.
- `logger.py` / P&L tracking — persist every decision (market, z, model prob, book state, action taken, fill price, resolution outcome) for post-hoc analysis and to keep feeding the backtest with real execution data.

## Risk controls (must exist before running with real money)

- Max concurrent positions / max exposure per cycle, across all markets trading simultaneously.
- Kill switch: if spot feed goes stale, book data goes stale, or an unexpected API error occurs mid-window, skip the trade rather than fail open into an unvalidated action.
- Sanity bounds on model probability and fill price (reject trades where inputs look corrupted, e.g. crossed book, stale timestamp).
- Dry-run / paper-trading mode as the default until backtest validates the edge net of realistic fills.

## Dependencies / blockers before this can run for real

- Kalshi auth (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`) not yet configured in this project — needed for both the orderbook websocket and (separately) order placement.
- Order-placement API integration doesn't exist yet anywhere in the scraper submodule — only read-only market listing and orderbook streaming are wired up.
- Backtest (main README) should validate the edge survives realistic fill modeling before this live system is built out beyond a paper-trading skeleton.

## Status

Outline only — sequenced after the backtest in the main strategy README. Do not build execution/order-placement logic until the backtest confirms edge net of orderbook-aware fill simulation.
