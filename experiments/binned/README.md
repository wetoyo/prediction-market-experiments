# binned

Trades that were built and backtested, then shelved because the backtest
said no. Each folder keeps its code, its tests and a README with the
findings, so the idea doesn't get re-tried from scratch and can be revived
if something changes (a new signal, cheaper fees, more history).

| trade | idea | why it was binned | binned |
|---|---|---|---|
| `perp_martingale/` | Martingale with Kelly-sized cycles and profit skimmed after every win. Bet direction from indicators on KXBTCPERP (MACD/RSI and 10 others, plus each reversed). Traded as perp brackets and as KXBTC15M up/down contracts. | No signal beat costs out of sample on either venue. Kelly almost never bets. The classic martingale's result depends on when the rare bust lands, not on the signal. | 2026-10-23 |

Run a binned trade's backtest from its own folder, as before, e.g.
`cd perp_martingale && python backtest_15m.py --sweep`. Cached data
(`data/`) is gitignored and refetched on first run.
