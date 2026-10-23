# perp_martingale

**Question:** can a martingale on Kalshi's BTC perpetual (`KXBTCPERP`) make
money if each bet's direction comes from momentum indicators (MACD + RSI),
the profit is taken out after every won cycle, and the cycle is Kelly-sized
so a bust never takes the bankroll near zero?

**Backtest answer (2026-10-23): no.** No direction signal won often enough
to pay for the costs, so the Kelly sizing never funds a cycle. The classic
martingale (no Kelly) loses money at the planned settings. Details below.
There is no live runner, and none is worth building on these results.

## The strategy

- **Bet** (`backtest.py`): enter at market and exit at +/-`BRACKET` from the
  fill, whichever comes first. The take-profit is a resting limit; the stop
  is a stop-market. One bet is roughly a coin flip before costs.
- **Direction** (`indicators.py`): from the latest closed `SIGNAL_BAR_MINUTES`
  bar. `macd_rsi` goes long when the MACD(12,26,9) histogram > 0 and
  RSI(14) > 50, short when both are below, and waits when they disagree.
  Also available: `macd`, `rsi`, any signal with `_fade` (to trade against
  it), and two baselines, `always_long` and `random`.
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
python backtest.py --sweep   # 504 configs ranked on train (06-11..09-11), top 10 re-run on test (09-11..10-23) at 1/5/10bp
python -m pytest tests
```

### Results

**Breakeven.** A bracket bet breaks even at a win rate of (b + c) / 2b, where
c is the round-trip cost: 2x the fee plus about 2bp of spread and slippage.
At a 5bp fee that's 74% for a 0.25% bracket, 62% for 0.5%, 56% for 1% and
53% for 2%.

**Raw hit rates** (train, at 5bp): every indicator lands between 44% and
54%, the same range as `random`. At brackets of 1% or less, no signal clears
breakeven. The momentum signal (`macd_rsi`, 15m, 0.5%) won 47.4% of 1,172
bets, slightly worse than a coin flip and far below the 62% it needs.
Fading it doesn't help either: around 49-51%. The only positive edges are
at the 2% bracket on about 90 bets, where 1 standard error is about 5
points. The best of those is `always_long` (BTC's drift), which uses no
indicator at all.

**Kelly sizing (what you asked for):** across 288 Kelly-sized configs, 2
ever funded a bet, and both by noise (`random` direction). With no edge
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

## If you want to keep going

The martingale and Kelly parts are done and tested. What's missing is a
signal with a real edge. A candidate passes if `--sweep` shows a hit rate
clearly above breakeven, over hundreds of bets, on both train and test.
Add it to `indicators.directions` and re-run. Kelly will start funding
cycles on its own once the walk-forward win rate clears breakeven.
