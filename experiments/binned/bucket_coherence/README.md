# bucket_coherence (binned 2026-10-24)

**Question:** Many Kalshi events are ladders of mutually exclusive,
exhaustive buckets: exactly one bucket resolves YES. Examples are the
crypto, index and FX range buckets, daily high-temperature buckets, game
winners and the Fed decision. Do these ladders ever price a riskless full
set below its payout, after taker fees?

- **YES set:** buy one YES in every bucket. It pays exactly $1, so it is an
  arb when sum(ask + fee) < $1.
- **NO set:** buy one NO in every bucket. It pays exactly n - 1, so it is an
  arb when sum((1 - bid) + fee) < n - 1, i.e. roughly when the yes bids sum
  to more than $1.

**Answer: no, not at a size or edge worth trading.** The candle screen does
find apparent arbs that persist for 3 or more minutes, on 89 events in the
recent half of the sample. The mean edge is 0.54c a set, below the
pre-registered 1c bar. Most are weather ladders at 0.3-0.5c, mostly in thin
overnight books. In the live order books there were no arbs at all: 312
snapshots of 55 open ladders, all simultaneous, none with even one set
available net of fees. No live runner was built.

## Pass criteria (fixed before the run)

(a) The candle screen shows apparent full-set arbs, net of fees, that
persist for at least 3 consecutive 1-minute candles. They must appear on at
least 30 distinct events in the recent half, with a mean net edge of at
least 1c a set. (b) The live books show at least one real simultaneous arb
of 5 or more sets, or candle evidence strong enough to justify a forward
logger. Failing (a) means the idea fails.

| criterion | result | verdict |
|---|---|---|
| (a) events with a persistent arb, recent half | 89 (bar: 30) | pass |
| (a) mean net edge on those events | **0.54c** (median 0.30c; bar: 1c) | **fail** |
| (b) live simultaneous arb, 5+ sets | 0 of 312 snapshots had even 1 set | fail |

A more generous reading of (a) still fails: the mean of each event's
*best* edge is 0.81c.

## Method

**Candle screen** (`screen.py`). The sample covers 2,346 settled events in
24 series, closing 2026-06-24 to 2026-10-24. The recent half is the 2,087
events closing on or after 2026-08-25, which is also Kalshi's historical
cutoff.

- Every event's `mutually_exclusive` flag is true.
- For each event, `screen.py` fetches every bucket's 1-minute yes bid/ask
  candles. The window runs from the moment every bucket is listed (or 48h
  before close, whichever is later) to the first bucket's close.
- In total that is 67.6k event-hours of minute grids, about 15.7k API
  requests, all cached under `data/`.
- Kalshi emits a candle only in a minute when a bucket's quote changed. Each
  bucket's last close is therefore forward-filled onto a common minute grid,
  and quotes from before the window are seeded from hourly candles.
- At every minute where all buckets are quoted, the screen computes both set
  edges after the taker fee 0.07·m·p·(1-p) per contract, where m is the
  series' `fee_multiplier`. m is 1 everywhere except KXMLBGAME, where it is
  0.5. The crypto, index, FX and weather series are `fee_type: quadratic`,
  with no maker fee. The sports series and KXFEDDECISION are
  `quadratic_with_maker_fees`. A missing ask is treated as unbuyable, and
  so is a missing bid on the NO side.
- Runs of positive edge are measured in consecutive minutes. A run is
  "static" when no bucket's quote changed during it, which means the sum
  really was on screen simultaneously.

Series and sample sizes (recent / early half). The early half comes one
bucket at a time from `/historical/`, so it is thinner.

| group | series | events |
|---|---|---|
| crypto hourly | KXBTC (188 buckets), KXETH (300), KXXRP (75) | 720 / 14 |
| index daily | KXINX, KXNASDAQ100 (30 buckets) | 86 / 20 |
| FX daily | KXEURUSD, KXUSDJPY (15) | 84 / 20 |
| weather | KXHIGHNY/CHI/LAX/MIA/AUS/DEN/PHIL/TDC, KXLOWTNYC (6) | 539 / 135 |
| sports (2-3 way) | KXNFLGAME, KXMLBGAME, KXNHLGAME, KXNBAGAME, KXNCAAFGAME, KXEPLGAME, KXATPMATCH | 657 / 69 |
| econ | KXFEDDECISION (5) | 1 / 1 |

**Live snapshot** (`snapshot.py`). Six sweeps ran 5 minutes apart, from
2026-10-24 18:42 to 19:07 UTC (a Saturday). Each sweep took the 3
soonest-closing open ladders of every series and fetched all their books
through `/markets/orderbooks` (up to 100 tickers per call). That makes one
simultaneous snapshot per ladder, or 2-3 calls for BTC and ETH. For each
ladder, `snapshot.py` walks every leg's asks together (YES asks = 1 - NO
bids). It reports the top-of-book edge and the largest whole number of sets
that still nets > 0.

```bash
python screen.py                 # fetch (cached in data/) + summary + detail
python screen.py --summary-only
python snapshot.py --sweeps 6 --interval 300
python snapshot.py --summary-only
python -m pytest tests           # 16 offline tests
```

Both scripts throttle to `BCO_MAX_RPS` (3.5/s for the screen and 1/s for
the snapshot by default; the snapshot ran at 0.5/s alongside the screen).
They back off exponentially on 429 and 5xx.

## Results

**Exhaustiveness.** 2,337 of 2,346 events had exactly one YES. The other 9
are game ties, cancellations and an ATP retirement. They settled "scalar"
(50/50, or 0.79/0.21 at the last fair price), but the bucket payouts still
summed to exactly $1, so a full set would have been made whole. No ladder
listed a bucket after the others had opened. Exhaustiveness is not the
problem.

**Typical full-set cost** (median of each event's best minute, net edge in
cents; negative means no arb):

| group | best YES-set edge | best NO-set edge | why |
|---|---|---|---|
| crypto hourly | -128c to -1023c | never buyable | 75-300 buckets, the far ones ask the 1c minimum, so the YES asks sum to $2-11. The far buckets have no bid, so a NO set can't be bought. |
| index daily | -37c to -42c | never buyable | same, 30 buckets |
| FX daily | -11c to -14c | -19c to -24c | 15 buckets |
| weather | -0.4c to -3.1c | -0.2c to -9c | 6 buckets, asks sum to about 1.03-1.10 in the daytime |
| sports | -0.3c to -1.2c | -0.5c to -2.1c | 2 legs near 50c pay about 3.5c in fees per set (1.75c on KXMLBGAME) |

**Persistent apparent arbs (3+ minutes), recent half:**

| group | runs | events | hours to close (median) | mean edge | mean best edge | static runs |
|---|---|---|---|---|---|---|
| weather | 110 | 74 | 31.1 | 0.37c | 0.45c | 60 |
| sports | 10 | 8 | 32.9 | 0.40c | 0.46c | 9 |
| FX | 11 | 6 | 7.2 | 2.93c | 4.34c | 1 |
| index | 2 | 1 | 17.3 | 1.29c | 1.48c | 0 |

- Of the 384 positive-edge runs in the recent half, 181 last one minute and
  70 last two. Most apparent arbs are asynchronous candle closes, as
  expected.
- The weather runs cluster around 20-38h before close. That is the evening
  and overnight after a ladder lists, when its 6 books are thin. Many runs
  are static, so the sum really was below $1, but only by 0.3-0.5c.
- The FX and index outliers sit at listing (books still filling in) or in
  the final hours. An early-half KXEURUSD event showed a 29c "edge" for 53
  minutes right after listing, but the size there is unknown and almost
  certainly tiny.
- Before fees, sum(asks) < $1 for 3+ minutes happens on most weather events
  (e.g. 55 of 60 KXHIGHCHI). The 1c tick on the tails plus the quadratic
  fee consume it.

**Live books:** 0 of 312 ladder snapshots had an arb of even one set net of
fees. Closest per group:

| group | best YES edge | best NO edge |
|---|---|---|
| sports | -1.75c (MLB) to -5.5c | -1.7c to -5.0c |
| weather | -3.9c (KXHIGHTDC) to -9.6c | -6.3c to -13.5c |
| FX | -101c | not buyable |
| index | -290c | not buyable |
| crypto | -95c (the 50-bucket weekly BTC) | not buyable |

## Why it failed

The ladders are coherent to within the fee. Where the bucket count is small
enough for a full set to be cheap (sports, weather), market makers keep the
sum of asks within about a cent of $1. The taker fee costs about 3.5c a set
for two legs near 50c, and 1-3c across a weather ladder, so it covers the
whole gap. Where the gap is big (crypto, index and FX ladders with 15-300
buckets), it runs the wrong way: every far bucket asks at least 1c, so a
YES set costs $2-11. Those far buckets also have no bid, so a NO set can't
be completed at any price.

The apparent arbs that survive fees are a few tenths of a cent. They sit in
thin overnight weather books or at listing, and the candles can't show
their size. The live books showed none.

## What might revive it

- **Lower fees.** On the `quadratic` series (weather, FX, index, crypto)
  makers pay no fee, and the sports series charge makers about a quarter of
  the taker rate. In the live books the yes bids summed to about
  0.98-0.99 on sports and weather ladders. Resting a bid on every leg would
  therefore buy a set for 1-2c under its payout if every leg filled. But
  resting legs fill one at a time, if at all, so this is a legged
  market-making strategy with inventory risk, not an arb. It needs its own
  test.
- **Forward logging of thin windows.** If someone wants to measure size on
  the overnight weather runs (about 0.4c, 20-38h before close), log full
  ladder books from 22:00 to 06:00 UTC. At 0.4c a set it would need
  thousands of sets a night to matter, which these books don't have.
- **New ladder types** with few buckets and two-sided quotes on every
  bucket, e.g. award or election winner ladders. Kalshi's 1c minimum tick
  on the tails makes wide ladders structurally overpriced, so any revival
  is on narrow ones.

## Not modeled

- Queue position and partial fills. The snapshot walks visible depth only.
- Within-minute timing. The candle closes of different buckets within one
  minute are not simultaneous, which is the reason for the persistence and
  "static" filters.
- Fee rounding. Kalshi rounds the per-order fee up to $0.0001 (see
  `fees.py`), which adds at most $0.0001 per order. The task brief's
  cent-rounding version would make every small arb worse.
- The live snapshot ran on a Saturday, when index and FX books are quiet.
  The weather and sports books were active.
