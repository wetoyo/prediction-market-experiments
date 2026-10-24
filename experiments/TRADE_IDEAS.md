# Trade ideas, 2026-10-24

Five candidates, ranked by expected edge after fees, how well the backtest
data covers them, and how far they are from what's already been tried
(`resolution_alpha`, `btc_implied_prob`, `golf_field_alpha`,
`binned/perp_martingale`). Each is worked in order by its own agent: backtest
it, build it if it passes, bin it if not, then move to the next.

| rank | idea | edge source | data | status |
|---|---|---|---|---|
| 1 | **weather_mos_alpha**: Kalshi daily high-temperature buckets (`KXHIGHNY`, `KXHIGHCHI`, ...) priced off NWS MOS forecasts | Retail-heavy markets, while a calibrated forecast-error distribution around the GFS/NAM MOS max temp is a strong, free model at a 1-day horizon | Kalshi settled markets + candlesticks; IEM MOS archive (verified working); settlement from Kalshi's own results | pending |
| 2 | **longshot_fade**: buy NO on cheap contracts (≤10c) across all Kalshi categories some hours before close | Favorite-longshot bias, the most documented pricing bias in prediction markets (takers on cheap contracts lose most) | Kalshi settled markets + candlesticks, every category | pending |
| 3 | **index_range_vix**: S&P/Nasdaq daily range and above/below markets (`KXINX`, `KXINXU`, `KXNASDAQ100*`) priced off VIX/VXN-implied vol | Same model as `btc_implied_prob`, moved to equities, where the option market is deeper and the Kalshi side thinner | Kalshi settled markets + candlesticks; daily VIX + index closes | pending |
| 4 | **shock_reversion**: fade large, fast price moves on Kalshi markets that have no hard news driver | Overreaction to flow in thin books; prices partly revert | Kalshi candlesticks across categories | pending |
| 5 | **bucket_coherence**: buy the whole set of a mutually exclusive bucket ladder when the YES asks sum to < $1 after fees (or the NOs to < n-1) | Pure arbitrage across buckets of one event | Needs order-book snapshots, which Kalshi doesn't keep: backtest only off candlestick asks, or forward-log it | pending |

## Ranking rationale

1. **weather** ranks first: a real external model that is better than the
   crowd at a 1-day horizon, a large sample (several cities, a market every
   day, several buckets each), and no overlap with anything already built.
2. **longshot_fade** has the strongest prior from the literature, but its
   edge per contract is small at 90c+ and fees plus the spread eat much of
   it. golf_field_alpha already tests a narrow version.
3. **index_range_vix** reuses a proven pricing model, but daily vol data is
   coarse for intraday markets, and equity markets are likely better arbed.
4. **shock_reversion** is plausible but has no model behind it, so it's the
   most exposed to overfitting.
5. **bucket_coherence** is close to riskless when it fires, but the backtest
   can't see real asks, so it may not be testable before forward logging.
