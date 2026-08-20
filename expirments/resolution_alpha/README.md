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
- **Index/oracle mechanics — confirmed:** Kalshi settles on a 60-second trailing average of CF Benchmarks' Real Time Index (e.g. BRTI for BTC) ending at `close_time`, compared against a strike that's itself a 60-second average captured at `open_time` (pulled directly from a live market's `rules_primary` contract text on 2026-08-04). Not an instantaneous last-tick price — see [live/README.md](live/README.md) for how the probability model accounts for this. CF Benchmarks' index isn't freely available over REST, but Kalshi's authenticated websocket exposes it directly via a `cfbenchmarks_value` channel (confirmed against Kalshi's own docs on 2026-08-05) — `live/ws_feed.py` subscribes to it for BTC (`BRTI`) and ETH (`ETHUSD_RTI`), the only two index ids confirmed so far, eliminating Coinbase-proxy basis risk for those two assets. Every other underlying still uses Coinbase spot as a stand-in and still carries that basis risk.
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

**Done, partially — see `backtest.py` and its results below.** Steps 1 and 3 (discovery, probability model) are implemented and run against real settled-market history. Steps 2 and 4 (order-book reconstruction, realistic fill simulation) are **not** implemented, because they're not implementable from data Kalshi's REST API exposes: `/markets/{ticker}/orderbook` only ever returns the *current* book, never a historical one, so there is no way to know what price or depth was actually available 90 seconds before close for a market that already settled. That remains the real blocker flagged below — closing it requires recording our own order-book history going forward (via `live/ws_feed.py`'s `orderbook_delta` subscription, now running) before a fill-realistic backtest is possible.

1. ~~Auto-discover all recurring crypto strike series (15m/30m/60m) via `fetch_series`/`fetch_markets`~~ done — `backtest.py` reuses `live/discovery.py`'s exact series filter against `status="settled"` markets instead of `"open"` ones.
2. Reconstruct, for each market, the full book state at T-90s, T-60s, T-30s through resolution (see Liquidity & fill modeling) — **not done, no historical book data available** (see above).
3. ~~Build a fair-value probability model~~ done — `live/probability.py`'s z-score/settlement-window model, run unmodified (imported, not reimplemented) against historical Coinbase spot as the same proxy `live/spot_feed.py` uses live.
4. Simulate the entry rule by walking the reconstructed book for realistic fill price and fill probability — **not done**, same blocker as step 2. `backtest.py` reports probability-model calibration only, never P&L.
5. Evaluate: win rate, avg edge captured, avg slippage vs. naive best-ask assumption, max drawdown, Sharpe/Kelly sizing, edge decay — **partially done**: win rate/calibration is reported (see Results below); everything requiring fill price (edge captured, slippage, drawdown, sizing) needs step 2/4's data and isn't available yet.
6. Segment results by asset, interval length, and time-to-expiry bucket — done for time-to-expiry (`--decision-seconds`); not yet broken out by asset/interval.

### Results (2026-08-05, 24h lookback, `python backtest.py --hours 24`)

12,262 settled crypto interval markets across 22 qualifying series (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, NEAR, ZEC at 15m/60m cadences). At the entry threshold this strategy actually uses (`MIN_FAVORED_PROBABILITY = 0.97`, `ENTRY_WINDOW_SECONDS = 90`):

| decision point | signals (model prob ≥ 0.97) | actual win rate | avg model prob |
|---|---|---|---|
| T-90s | 11,791 | **100.0%** | 0.9998 |
| T-60s | 12,018 | **100.0%** | 0.9999 |
| T-30s | 12,179 | 99.8% | 1.0000 |

This is a strong result for the model's core claim (once the model is >=97% confident with the entry window's worth of time left, the favored side has historically always resolved that way in this sample) but it comes with real caveats, not just the fill-modeling gap above:
- ~96% of *all* settled markets qualified as a "signal" at T-90s. That's expected given the thesis (crypto typically drifts well away from an at-the-money strike over a 15-60 minute window), but it means this sample isn't testing edge cases — it's testing the common case, which is also the case with the least interesting probability-model behavior (spot already far from strike, z-score trivially large).
- Calibration is visibly worse in the 90-97% model-probability band, especially close to expiry (e.g. at T-30s, the [0.900,0.950) bucket showed a 58.8% actual win rate on n=17 vs. an average model probability of 93.6% — a real overconfidence gap, just in a band the live threshold doesn't trade on). Don't lower `MIN_FAVORED_PROBABILITY` without more data in that band.
- Spot proxy is 1-minute Coinbase candles here vs. live's ~2-second polling (or, for BTC/ETH now, the real CF Benchmarks feed — see `live/ws_feed.py`), so the last-60-second variance-shrinkage regime in `probability.py` runs on coarser data in this backtest than it does live.

## Open questions

- Are 15m markets liquid enough to fill meaningful size, or is edge only theoretically there? (`live/discovery.py` found ~1,660 open crypto interval markets live across BTC/ETH/SOL/XRP/DOGE/BNB/HYPE/NEAR/ZEC on 2026-08-04 — plenty of candidates exist; depth per market near expiry is still unmeasured, since Kalshi's REST API has no historical order book to check this against past markets. `live/ws_feed.py` now records live book state going forward, which is the only way to eventually close this.)
- Does edge differ between BTC (most efficient) vs. smaller-cap crypto series? Not yet segmented in `backtest.py`.
- Is there a cleaner probability model than empirical z-score buckets (e.g. using realized vol term structure specific to short horizons)? `live/probability.py`'s calibration against 24h of settled markets is strong at the >=97% threshold this strategy actually trades on (see Backtest plan results above), but measurably worse in the 90-97% band — don't lower the threshold without more data there.
- What's Kalshi's exact fee schedule for these series, and does it scale with price (higher fee on cheap/expensive contracts)? (`live/fees.py` uses the commonly-cited `0.07 * contracts * price * (1-price)` formula, consistent with the "quadratic" `fee_type` these series report, but unconfirmed against real fills.)
- Does Coinbase spot track CF Benchmarks' actual settlement index closely enough near expiry to trust, or does basis risk eat the edge? **Resolved for BTC/ETH** — `live/ws_feed.py` now feeds the real CF Benchmarks index directly via websocket instead of a Coinbase proxy for those two. Still open for every other underlying.

## Status

A live execution loop exists — see [live/README.md](live/README.md) — and now runs on Kalshi's authenticated websocket (order book + BTC/ETH settlement index) instead of pure REST polling, with REST/Coinbase fallback wherever the socket has no data. Still **DRY_RUN by default**. Probability-model calibration has been checked against 24h of real settled-market outcomes (`backtest.py`, results above) and looks strong at the threshold the strategy trades on. What's still unvalidated: fill/liquidity economics (Kalshi's REST API has no historical order book, so this needs live data collection going forward — see Backtest plan) and real order placement (the endpoint path is unverified against the live API, see live/README.md). Don't flip `RESOLUTION_ALPHA_DRY_RUN=false` until both of those are addressed.
