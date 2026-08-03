# Resolution Alpha

**Thesis:** On Kalshi's recurring crypto strike markets (BTC/ETH/etc. "above/below $X at :00/:15/:30/:45", in 15m/30m/60m flavors), the resolution side becomes statistically near-certain in the final minute of the window, but the market frequently misprices that certainty — the "strong" side often trades meaningfully below fair value (e.g. 90-97c instead of 99c+) because liquidity is thin and takers/makers aren't actively arbing the last 60 seconds. Buying the strong side late captures the gap between market price and true resolution probability.

This is a convergence/theta-style trade, not a directional crypto bet. The edge is in *microstructure near expiry*, not in predicting BTC price.

## Market mechanics

- Kalshi lists recurring "Bitcoin price range" / "above $X" markets on fixed cadences: 15m, 30m, 60m (ticker families like `KXBTC-*`, `KXBTCD-*` — confirm exact series per asset).
- Each market has a strike (or range) and resolves YES/NO based on a reference index price (Kalshi uses its own crypto index, sourced from multiple exchanges — not raw Coinbase/Binance spot) at the expiry timestamp.
- Because the underlying is a live, continuously-quoted asset (BTC/ETH), the "true" probability of resolution can be computed at any point in the window from spot price vs. strike, time-to-expiry, and realized/implied vol (Black-Scholes-style binary option pricing, or empirically via distance-to-strike in stdevs).
- In the closing seconds, if spot is meaningfully away from strike, P(resolve in that direction) → ~99%+. Market price on Kalshi often lags this and sits at 90-96c, leaving 3-9c of edge on a contract that pays $1.

## Where the edge comes from (hypotheses to validate)

1. **Thin last-minute liquidity** — market makers may pull or widen quotes near expiry to avoid pin risk, leaving stale/wide resting orders that don't reflect true resolution probability.
2. **Retail/whale flow noise** — late directional bets or unwinds can push price away from fair value without being arbed back in time.
3. **Fee/spread friction deters natural arbers** — Kalshi fees + bid/ask spread may be just large enough that "easy" edge sits unharvested by casual participants, especially in less liquid intervals (15m).
4. **No cross-venue arb pressure** — unlike sports/election markets, there's no equivalent "sharp" side importing outside odds, so mispricing can persist longer.

Each of these needs to be checked against real order book data before assuming the edge is durable — see Open Questions.

## Strategy outline

**Entry:**
- Monitor active interval markets for a given asset (start with BTC 15m/30m/60m — highest volume).
- Compute live "distance to strike" in standardized units: `z = (spot - strike) / (sigma * sqrt(time_to_expiry))` using short-horizon realized vol.
- Define an entry window near expiry (e.g. last 60-90 seconds) where `|z|` exceeds a threshold implying model P(resolve) > some floor (e.g. 97%+).
- Compare model probability to best ask on the favored side. If `model_prob - ask_price > edge_threshold` (covering fees + slippage + margin of safety), buy the favored side at or near the ask.

**Exit:**
- Hold to resolution (theta play — no need to exit early; the position either resolves to $1 or $0).
- Optional: if price spikes back toward the strike before expiry (spot reversal), consider an early-exit stop if `z` collapses below a re-entry threshold, to cap tail risk from a fast reversal.

**Position sizing:**
- Kelly-fraction or fixed-fraction sizing based on edge size and confidence in the probability model — start conservative (this is a fat-tail/pin-risk trade: rare fast reversals in the last 60s can wipe out many small wins).
- Cap size per underlying/interval to avoid correlated exposure across simultaneous overlapping windows (e.g. a 15m and 60m BTC market resolving near the same time move together).

## Market discovery (auto, not hardcoded)

Don't hand-pick BTC/ETH — scan Kalshi for every recurring crypto strike series and trade whichever are live, so the strategy scales across the full crypto board (BTC, ETH, SOL, XRP, DOGE, etc.) and across all interval lengths automatically.

- `prediction_market_scraper/Clients/Kalshi/fetch_historical.py` already exposes `fetch_series(category=...)` and `fetch_markets(series_ticker=..., status="open")` against the public (unauthenticated) `trade-api/v2` REST endpoints — this is the discovery mechanism, no new API integration needed.
- Discovery job (run on a schedule, e.g. every few minutes): `fetch_series(category="Crypto")` (confirm the actual category label Kalshi uses) → filter tickers matching the recurring-interval families (e.g. `KXBTCD`, `KXETHD`, ...) → for each series, `fetch_markets(series_ticker=..., status="open")` to get the currently-open instances and their `close_time`.
- Build a live registry: `{ticker, underlying, strike, interval_minutes, open_time, close_time}` refreshed each cycle, since new market instances spin up every interval.
- This registry is the input to both the backtest (which series/intervals actually exist and have history) and the live system (what to subscribe to and trade each cycle).

## Liquidity & fill modeling (critical — thin books near expiry)

Thin end-of-window liquidity is the main risk to the theoretical edge, so the backtest must not assume fills at the quoted best ask. Modeling this properly is a prerequisite, not a nice-to-have.

- **Use full order book snapshots/deltas, not top-of-book.** Reconstruct the book at each decision point (T-90s, T-60s, ... through expiry) from Kalshi's `orderbook_delta` websocket feed (`live_datastream.py` already implements the signed subscription — needs `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` set, not yet configured in this project) or from historical order book snapshots if Kalshi's historical API exposes them (needs confirming — may need to build our own historical book archive by recording the live feed going forward, since REST historical endpoints likely only give trade prints).
- **Simulate walking the book**, not hitting the best ask: for a target size, sum liquidity through successive price levels to get a realistic average fill price, and treat unfilled remainder as unfilled (not magically filled at the last level's price).
- **Model book depth as a function of time-to-expiry** — expect depth to shrink approaching close; the strategy should size down (or skip) when available depth at acceptable prices is below the intended position size, rather than force a fill into a thin book and eat bad slippage.
- **Track realized vs. assumed fill price** as a backtest metric — this is the number most likely to erode the theoretical edge from the naive best-ask model, so report it explicitly rather than only reporting P&L.
- Consider whether resting limit orders (posting inside the spread and waiting) beat crossing the spread with a market/marketable-limit order, given the short time window — may be a false tradeoff if book is too thin/fast-moving to safely rest an order in the last minute, but worth backtesting both.

## Key risks

- **Pin risk / last-second reversal:** BTC can move fast; a "sure thing" at z=3 with 45s left can still flip on a sharp wick, especially on illiquid alts. Fat tails > normal model assumes.
- **Index/oracle mechanics:** Kalshi resolves on its own index price (possibly a TWAP or median-of-exchanges over a short window near expiry, not last-tick spot) — must reverse-engineer or find documented resolution methodology, since trading against raw spot could be wrong if the settlement mechanism smooths differently.
- **Latency:** edge window is ~60-90 seconds; need low-latency spot feed + order placement, or the edge is gone by the time you act.
- **Fees:** Kalshi trading fees eat into small per-contract edges — must model fees explicitly, especially since payoff per contract is capped at $1.
- **Liquidity/fill risk:** thin books near expiry mean the displayed ask may not be fully fillable at size; slippage modeling required.
- **Adverse selection:** if you can compute this, so can others — edge may already be competed away in the most liquid series (BTC 60m) and only survive in longer-tail/less-watched series.

## Data requirements

- Kalshi: full order book (not just trades) for target markets, ideally websocket-level (`prediction_market_scraper/Clients/Kalshi/live_datastream.py` is the existing hook point, needs auth keys configured) plus historical resolved markets for backtesting (`fetch_historical.py`, `clean_historical.py`). If Kalshi's REST history doesn't retain book-depth snapshots, we likely need to run our own recorder against the live websocket for a period before a depth-aware backtest is possible — flag this as a probable blocker to resolve early.
- Series/market metadata via `fetch_series` / `fetch_markets` for auto-discovery of all crypto series (see Market discovery above), not just BTC/ETH.
- Spot reference: high-frequency price feed per asset matching (as closely as possible) whatever index Kalshi uses for resolution — need to confirm Kalshi's documented settlement source per series.
- Market metadata: strike, open time, close time, resolution time, resolution source, fee schedule — per series.

## Backtest plan

1. Auto-discover all recurring crypto strike series (15m/30m/60m) via `fetch_series`/`fetch_markets`, then pull historical resolved markets with full order book history into expiry for each.
2. Reconstruct, for each market, the full book state at T-90s, T-60s, T-30s through resolution (see Liquidity & fill modeling).
3. Build a fair-value probability model (start simple: empirical frequency of resolution given z-score and time-to-expiry, bucketed from historical data — more robust than assuming Black-Scholes normality given crypto's fat tails).
4. Simulate the entry rule by walking the reconstructed book for realistic fill price and fill probability at intended size (not the quoted best ask); include fees.
5. Evaluate: win rate, avg edge captured, avg slippage vs. naive best-ask assumption, max drawdown from tail reversals, Sharpe/Kelly-optimal sizing, and edge decay over the sample period (is edge shrinking over time = getting arbed away?).
6. Segment results by asset, interval length (15m vs 60m), and time-to-expiry bucket to find where edge concentrates and where liquidity actually supports meaningful size.

## Open questions

- What exactly is Kalshi's settlement index/methodology for each crypto series? (Confirm via Kalshi API docs / contract rules before relying on any spot proxy.)
- Are 15m markets liquid enough to fill meaningful size, or is edge only theoretically there?
- Does edge differ between BTC (most efficient) vs. smaller-cap crypto series?
- Is there a cleaner probability model than empirical z-score buckets (e.g. using realized vol term structure specific to short horizons)?
- What's Kalshi's fee schedule for these series currently, and does it scale with price (higher fee on cheap/expensive contracts)?

## Status

Idea stage — no code yet. Next step is pulling historical resolved-market data via the existing Kalshi scraper to validate whether the mispricing (favored-side ask meaningfully below empirical resolution frequency) actually shows up in the data before building any execution logic.

The runtime component that will trade this live, once backtested, is outlined separately in [live/README.md](live/README.md) — synced to interval boundaries, trading across all auto-discovered crypto markets rather than a hardcoded list.
