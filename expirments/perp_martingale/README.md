# perp_martingale

**Question:** can a martingale make money if each bet's direction comes
from technical indicators on Kalshi's BTC perpetual (`KXBTCPERP`), the
profit is taken out after every won cycle, and the cycle is Kelly-sized so a
bust never takes the bankroll near zero? Tested two ways:

1. bracket bets on the perp itself (`backtest.py`);
2. Kalshi's 15-minute BTC up/down markets (`KXBTC15M`), with the direction
   taken from the perp (`backtest_15m.py`).

**Backtest answer (2026-10-23): no, on either.** Across 12 indicators,
each also reversed, at several bar sizes, no signal kept an edge over
costs out of sample. Kelly sizing therefore (correctly) almost never bets.
The classic martingale loses money, or wins by luck, depending on when the
rare bust lands. Details below. There is no live runner, and none is worth
building on these results.

## The strategy

- **Bet** (`backtest.py`): enter at market and exit at +/-`BRACKET` from the
  fill, whichever comes first. The take-profit is a resting limit; the stop
  is a stop-market. One bet is roughly a coin flip before costs.
- **Direction** (`indicators.py`): from the latest closed `SIGNAL_BAR_MINUTES`
  bar. `macd_rsi` goes long when the MACD(12,26,9) histogram > 0 and
  RSI(14) > 50, short when both are below, and waits when they disagree.
  The others are listed in `indicators.directions`:
  - momentum: `macd`, `rsi`, `ema_cross`, `tsmom`, `donchian`, `trend_vote`;
  - mean reversion: `bb_revert`, `rsi_revert`, `stoch_revert`,
    `streak_revert`, `funding_revert`;
  - baselines: `always_long`, `random`.
  Adding `_fade` to any signal trades against it.
- **Ladder** (`martingale.py`): after each loss, the next stake is sized so
  that winning it recovers every loss so far plus the cycle's base profit
  G. Costs push the growth per step above 2x: it is 2b/(b - c). The cycle
  ends at the first win or after `DEPTH` + 1 losses (the bust).
  `DEPTH = 7` survives 7 losses, so a bust has probability 1/256 at
  p = 0.5. 255 won cycles in a row (enough to double the bankroll) then has
  probability (255/256)^255 ≈ 1/e ≈ 36.8%.
- **Kelly** (`martingale.py`): treat the whole cycle as one bet. It wins G
  with probability 1 - q^(DEPTH+1) and loses the whole ladder R otherwise,
  so the fraction of the bankroll to risk is f = p_c - q_c · R/G. The cycle
  then risks `KELLY_FRACTION` · f of the bankroll, capped at
  `MAX_CYCLE_RISK`. p is the per-bet win rate, estimated walk-forward from
  bets already resolved. When f ≤ 0 the cycle isn't funded and paper bets
  keep the estimate learning.
  At p = 0.5, f is exactly 0 with no costs and negative once there are any:
  the martingale adds no edge. It only reshapes the outcomes into many small
  wins and a rare large loss. Any edge has to come from the direction
  signal.
- **Profit skim:** `SKIM_FRACTION` (default all) of each won cycle's profit
  moves to a reserve, so the working bankroll never compounds.
- **Margin:** the deepest bet's initial margin (notional / `LEVERAGE`) plus the
  ladder's earlier losses must fit in the bankroll. This caps the classic
  martingale ("full" sizing) well below the whole bankroll: about 6% risked
  per cycle at depth 7 with a 0.5% bracket, because the last stake is about
  870x the first.

## Backtest

Data: Kalshi's real `KXBTCPERP` 1-minute bid/ask candles and 8-hourly funding
(`data.py`, public API, cached in `data/`). The window is 2026-06-08 to
2026-10-23. The perp launched 2026-06-03, and its first days had a thin book
and empty-book placeholders in the candles: a bid low of 0 and an ask high
of about 9e14. Those would have triggered fake stops, so `data.clean`
removes them and the replay starts on 06-08. Fills use the real bid/ask
plus `FEE_RATE` taker on both legs and 1bp extra slippage on stops. A
minute that touches both the stop and the target counts as the stop.

```bash
python backtest.py           # config.py defaults: train / test / all
python backtest.py --sweep   # 1,638 configs ranked on train (06-11..09-11), best re-run on test (09-11..10-23)
python -m pytest tests
```

### Results (perp)

**Breakeven.** A bracket bet breaks even at a win rate of (b + c) / 2b, where
c is the round-trip cost: 2x the fee plus about 2bp of spread and slippage.
At a 5bp fee that's 74% for a 0.25% bracket, 62% for 0.5%, 56% for 1% and
53% for 2%.

**Raw hit rates** (train, at 5bp): all 26 signals land between 44% and
54%, the same range as `random`. At brackets of 1% or less, no signal clears
breakeven:

| bracket | breakeven | best signal | its win rate |
|---|---|---|---|
| 0.5% | 62% | `streak_revert` 60m | 52.1% (512 bets) |
| 1% | 56% | `rsi_revert` 15m | 53.6% (181 bets) |

The momentum signal (`macd_rsi`, 15m, 0.5%) won 47.4% of 1,172 bets,
slightly worse than a coin flip. The only edges above breakeven are at the
2% bracket on 50-95 bets, where 1 standard error is about 5 points. Out of
sample they scatter randomly: `rsi_revert` 60m went from 60% on train to
52% on test, and `streak_revert` 15m from 56% to 36%.

**Kelly sizing (what you asked for):** across 936 Kelly-sized configs, 4
ever funded a bet, a handful of bets each, all noise. With no edge
over costs, the Kelly fraction stays at or below 0, so the strategy doesn't
trade. That is the intended behaviour: it never approaches a zero balance
because it never bets. Even with fees set to zero, funded runs make +0.2%
to +1.4% over 4.5 months.

**Classic martingale at your settings** (`macd_rsi` 15m, 0.5% bracket,
depth 7, all profit skimmed, $100k bankroll so the 870x ladder fits
whole contracts, full window):

| fee | return | cycles | busts | bet win rate |
|---|---|---|---|---|
| 1bp | -9.0% | 773 | 4 | 48.0% |
| 5bp | -16.4% | 773 | 4 | 48.0% |

`random` direction did better (-2.9% / -11.0%), and `always_long` worse
(-18.0% / -25.6%). The bust rate (4 in 773, 0.52%) matches the 1/256
(0.39%) theory. Each bust cost about 5% of the bankroll, and the skimmed
profit from the 769 won cycles didn't cover the busts plus fees.

**The configs that look profitable** (1-2% brackets, depth 3-7, +3% to +25%
on train) had too few bets to tell from luck. They also didn't hold up: the
train winner (`rsi` 15m 2% depth 3, +24%) lost 7% to 12% on the test
window. The ones that stayed positive out of sample are `always_long`, i.e.
BTC's drift, and `random`. Their 0-bust runs are survivorship. At a 1/256
bust rate, 56-226 cycles is fewer than one expected bust, and the next bust
gives back 11-21% of the bankroll.

### Not modeled

- Queue position on the take-profit: a touch counts as a fill, which is
  optimistic.
- Liquidation: the brackets sit well inside the ~15% liquidation distance.
- Fee tiers and maker rebates. The account's real rate is at
  `/margin/fee_tiers` and needs auth.
- The margin-instance cash transfers a live runner would need. See
  `../btc_implied_prob/hedger.py`, which already implements them.

## KXBTC15M with the perp signal

`backtest_15m.py`. Each 15-minute market ("BTC price up in next 15 mins?")
is one bet, so bets never overlap.

- **Entry:** at minute 1 or 5 after the market opens, read the perp signal
  from the last perp bar closed by then. Long buys YES at that minute's real
  ask, short buys NO; the price must be between 20c and 80c.
- **Costs and payout:** Kalshi's quadratic fee, then hold to settlement.
- **Sizing:** the same ladder, with the win per dollar risked at the price
  actually paid: w = (1 - a - f) / (a + f).
- **Win-rate estimate:** shrinks the *excess* win rate (won - cost) toward
  0, because a binary's no-edge win rate is its price, not 50%
  (`martingale.EdgeEstimator`).
- **Data:** all 12,975 settled markets from 2026-06-08 to 10-23, with
  1-minute yes bid/ask from Kalshi's live and historical candlestick
  endpoints (`data.load_15m`; the first fetch takes about 20 minutes).
  At minute 1 the median spread is 1c and the median ask 50c, so breakeven
  is a win rate of about 51-52%.

```bash
python backtest_15m.py           # defaults: macd_rsi 15m, entry at minute 1
python backtest_15m.py --sweep   # 26 signals x bars 1/5/15m x entry 1/5 x sizing/depth: 1,092 configs
```

### Results (15m)

**Per-bet edge** (cents per contract after the fee). The default
`macd_rsi` 15m signal won 48.0% against a 51.2% breakeven (-3.2c over 6,146
train bets) and 49.5% on test (-1.7c). Of 156 signal/bar/entry combinations,
the best 10 on train were +0.1c to +2.6c. On test, 6 of those 10 turned
negative (-2c to -6c). The 4 that stayed positive:

| signal | train edge | test edge |
|---|---|---|
| `donchian` 1m @ 5m | +1.6c | +0.3c (458 bets) |
| `bb_revert_fade` 5m @ 5m | +1.5c | +0.5c (197 bets) |
| `rsi_revert_fade` 1m @ 5m | +1.6c | +2.3c (102 bets) |
| `bb_revert_fade` 1m @ 1m | -0.7c | +2.9c (379 bets) |

All 4 buy the side the market already favours (70-75c) in the direction
of the last minute's move. Their test edges are within 1 standard error of
zero (2-4c), which is about what the best of 156 noise draws looks like.
They are not evidence of an edge.

**Kelly** funded bets in 288 of 624 train configs, during stretches where
the walk-forward excess win rate was briefly positive. It funded nothing on
the test window in any of the train top 10: the estimate stayed at or below
0 throughout.

**Classic martingale** ("full": each cycle risks the whole working
bankroll, all profit skimmed). Each bust wipes out the working bankroll, so
a run is a one-shot lottery. 362 of 468 train runs ended with less than $5
working. The median return was -15%, and the train top 10 (+119% to +270%)
averaged -42% on test. At your settings (`macd_rsi` 15m, depth 7), it lost
55% on train and 20% on test. `random` direction with the same settings
lost 5% on train and *made* 70% on test (1 bust in 747 cycles, landing
late). The outcome depends on when the bust lands, not on the signal.

## If you want to keep going

The martingale and Kelly parts are done and tested. What's missing is a
signal with a real edge. A candidate passes if either `--sweep` shows it
clearly above breakeven, over hundreds of bets, on both train and test.
Add it to `indicators.directions` and re-run. Kelly will start funding
cycles on its own once the walk-forward estimate clears breakeven. The perp
history is only 4.5 months, so a candidate like the `*_fade @ 5m` rows
above is better tested by logging it forward (`../loggers`) than by
mining this window further.
