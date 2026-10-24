# binned

Trades that were built and backtested, then shelved because the backtest
said no. Each folder keeps its code, its tests and a README with the
findings, so the idea doesn't get re-tried from scratch and can be revived
if something changes (a new signal, cheaper fees, more history).

| trade | idea | why it was binned | binned |
|---|---|---|---|
| `perp_martingale/` | Martingale with Kelly-sized cycles and profit skimmed after every win. Bet direction from indicators on KXBTCPERP (MACD/RSI and 10 others, plus each reversed). Traded as perp brackets and as KXBTC15M up/down contracts. | No signal beat costs out of sample on either venue. Kelly almost never bets. The classic martingale's result depends on when the rare bust lands, not on the signal. | 2026-10-23 |
| `weather_mos_alpha/` | Kalshi daily high-temperature buckets (23 US cities) priced off NWS MOS (NBM/GFS/NAM) with a seasonal per-city error model; buy a bucket when model prob beats the ask + fee. | The market is better calibrated than MOS at both decision times (test Brier 0.113 vs 0.126). Test ROI -11.4% (CI -15.5% to -7.3%) over 2,700 trades; all 10 top train configs lost on test. | 2026-10-24 |
| `bucket_coherence/` | Buy every bucket of a mutually exclusive Kalshi ladder (YES set < $1 or NO set < n-1 after fees): crypto/index/FX ranges, weather highs, game winners, Fed decision. | Weather and sports ladders stay within the taker fee of $1. Crypto, index and FX ladders cost $2-11 because every far bucket asks at least 1c. Persistent arbs in candles average 0.54c/set (bar 1c); 312 live book snapshots had none. | 2026-10-24 |

Run a binned trade's backtest from its own folder, as before, e.g.
`cd perp_martingale && python backtest_15m.py --sweep`. Cached data
(`data/`) is gitignored and refetched on first run.
