# golf_field_alpha

**Thesis:** A Kalshi golf *outright-winner* market is one tournament
published as one event, with a separate binary YES/NO market for each
entrant and **exactly one** resolving YES. The entrants' prices don't sum
to 1 — they sum to 1 + overround, and the overround is large and uneven
(observed 1.03 on a tight PGA field, 1.3–1.6 on regular ones, 2.1+ on
thin LPGA/celebrity fields on 2026-08-27). Buying YES on a *basket* of
players — one contract each, so exactly one contract pays $1 — is a bet
that the market has mispriced part of that field: either specific names
are cheap relative to the field's own de-vigged consensus, or the
consensus itself is wrong (favorite-longshot bias) and a basket of the
shorter-priced players wins more often than its de-vigged probability
says.

This is **not** a golf-handicapping bet — there's no model of who plays
well. The only inputs are the field's own Kalshi prices and the fee
schedule. The edge, if any, is in the *cross-sectional pricing* of one
field at one moment.

Same house shape as `../resolution_alpha` and `../btc_implied_prob` (price
a Kalshi market, trade a threshold disagreement, size fractional-Kelly,
dry-run by default, calibrate against settled history). What's different:
there's no external probability model at all here, and a "position" is a
20–40-leg basket across one event rather than a single contract.

## Market mechanics (confirmed against the live + settled API, 2026-08-27)

- Series in `config.WINNER_SERIES` — `KXPGATOUR`, `KXCHAMPTOUR`,
  `KXKFTOUR`, `KXLPGATOUR`, `KXDPWORLDTOUR`, `KXLIVTOUR`, `KXGOLFTOURN` —
  are the outright-winner families. Each **event** (`KXPGATOUR-<TOURNEY>`)
  is one tournament; each **market** in it (`...-SSCH`) is one entrant,
  `strike_type: "custom"`, `custom_strike: {"Golfer": "Scottie Scheffler"}`,
  `yes_sub_title: "Scottie Scheffler"`. Settled history: 9/11 `KXPGATOUR`
  events had exactly one YES (the other two were voided/uncreated),
  `KXCHAMPTOUR` 7/7, `KXKFTOUR` 7/7, `KXDPWORLDTOUR` 4/4, `KXLIVTOUR` 3/3.
- The whitelist is hardcoded-but-`GOLF_FIELD_ALPHA_WINNER_SERIES`-overridable
  because no single API field separates "outright winner" from the ~40
  other golf series (top-N finisher, make-cut, head-to-head, round leader,
  3-ball, …), which have a different YES-count structure this basket math
  doesn't model. Scope is deliberately **outright winner only**.
- Fees: `fee_type: "quadratic"`, `fee_multiplier: 1` on every winner
  series checked — the same `ceil(0.07 · n · p · (1−p) · 1e4)/1e4` formula
  as the crypto experiments, so `fees.py` is a verbatim copy. Fees
  **dominate** this strategy: gross basket edge is a few cents and a
  basket has 20–40 legs, so `selection.py` checks the edge *after* the
  per-contract fee at the size actually bought, per leg.
- No historical order book (same limitation as the other two). The
  backtest replays against `/markets/trades` — real executed fills, which
  *do* have history.

## The two selection methods (`selection.py`)

Both first **de-vig** the field: take each player's raw implied
probability (the two-sided mid live, or the last trade price in the
backtest), then rescale so the field sums to 1 (`devig.py` —
`proportional` = divide by the sum; `power` = raise to a solved exponent,
which pulls longshots down harder, closer to the observed
favorite-longshot bias). `fair_i` is that de-vigged probability.

### `favorites_basket`  *(primary — this is the one the backtest headlines)*

Sort tradeable players (price in `[MIN_PRICE, MAX_PRICE]`) by price
**descending**. Walk from the favorite down, accumulating per-unit cost
(`price + fee`) until it would exceed `1 − EDGE_THRESHOLD`, then stop. The
skipped tail is the cheapest longshots. Every included leg gets the **same
contract count** (`unit_contracts`), so one of them winning returns
`unit_contracts` dollars for a per-unit basket cost of `≤ 1 −
EDGE_THRESHOLD`. **Loses only if a skipped longshot wins** — `skipped_prob`
reports that de-vigged tail mass, i.e. the strategy's own estimate of how
often it loses.

This is the literal reading of "buy YES on all available players up to a
fixed edge%": you'd buy the *whole* field for `1 + overround`, which is a
guaranteed loss; instead you buy the field *from the top down* only as far
as `1 − EDGE_THRESHOLD` lets you, and bet the skipped longshots don't win
often enough to matter.

### `devig_edge`

Include every tradeable player whose `fair_i − price_i − fee ≥
EDGE_THRESHOLD` per contract; size each leg by fractional Kelly on its own
edge; scale the whole basket down if it exceeds `MAX_EVENT_COST_DOLLARS`.
A bet that specific names are underpriced versus the field's own
de-vigged consensus.

**Honest limitation:** with a *complete* book (every player quoted,
overround > 1), proportional de-vig gives `fair_i = mid_i / S < mid_i ≤
ask_i` for every player, so this method **never fires** — there is no
positive-edge subset inside a book that already sums to more than 1. It
fires only when the *quoted* part of the field is **underround** (many
players with no bid, so the quoted mids sum to < 1 and the de-vig
*inflates* fairs) — which is exactly the thin, unarbitraged book the
thesis is about, but is the minority case. The backtest (single-price
trade history, no bid/ask) can barely evaluate it and will mostly report
"no leg clears edge threshold". Treat `devig_edge` as the live-only,
order-book-dependent lens; `favorites_basket` is what the historical test
actually measures.

## Files

| file | role |
|---|---|
| `discovery.py` | scan `WINNER_SERIES`, group open markets into `GolfEvent`s with per-player quotes |
| `devig.py` | `proportional` / `power` overround removal |
| `selection.py` | `plan_basket()` → a `BasketPlan` of YES legs, both methods, all fee/size/cap logic |
| `fees.py` | Kalshi quadratic fee (verbatim from `../resolution_alpha`) |
| `order_manager.py` | dry-run-by-default YES order placement (`yes`→`bid` translation) |
| `positions_store.py` | persist + reconcile `open_positions` (keyed by player ticker) so a looping `--execute` doesn't re-buy legs |
| `strategy.py` | `scan` / `--execute` / `--loop` entrypoint |
| `backtest.py` | replay both methods against settled events at N decision points |
| `config.py` | every knob, all `GOLF_FIELD_ALPHA_*`-overridable |
| `live/` | `.env.example` + detached start/stop scripts, see `live/README.md` |

## Usage

```bash
pip install -r requirements.txt

python discovery.py              # list open golf winner events + fields + overrounds
python devig.py                  # de-vig demo on a toy field
python strategy.py               # scan open events, print both baskets, place nothing
python strategy.py --execute     # also place YES orders for config.SELECTION_METHOD's basket
                                 #   (simulated unless GOLF_FIELD_ALPHA_DRY_RUN=false AND creds set)
python strategy.py --loop 300 --execute    # rescan every 5 min
```

`config.py`'s knobs (all `GOLF_FIELD_ALPHA_*`): `SELECTION_METHOD`,
`DEVIG_METHOD`, `EDGE_THRESHOLD`, `MIN_PRICE`/`MAX_PRICE`,
`MIN_FIELD_SIZE`, `MAX_OVERROUND`, `MIN_SECONDS_TO_CLOSE` /
`MAX_SECONDS_TO_CLOSE`, `KELLY_FRACTION`, `MAX_CONTRACTS_PER_PLAYER`,
`MAX_EVENT_COST_DOLLARS`, `DRY_RUN_SIMULATED_BALANCE_DOLLARS`,
`WINNER_SERIES`, `DRY_RUN`.

## Backtesting

```bash
python backtest.py --days 300 --decision-hours 120,72,24 --method both
python backtest.py --days 300 --series KXPGATOUR,KXKFTOUR --devig-method power --edge-threshold 0.05
```

For every settled single-winner event in the lookback, `backtest.py`:

1. pulls each player's real `/markets/trades` history (checkpointed per
   event to `backtest_runs/<event>.json` so re-sweeps are free);
2. at each `--decision-hours` point before close, prices every player at
   its **last trade at or before that moment** (that one price stands in
   for both the de-vig `implied` and the `buy_price` you'd pay — there's
   no historical spread to reconstruct, so this is **optimistic vs
   crossing a real bid/ask** across 20–40 legs);
3. runs the exact `selection.plan_basket` for each method at **1 contract
   per leg** (so the headline P&L is independent of the Kelly/unit sizing
   config — it measures the raw alpha of the *selection*);
4. settles the basket against the real winner and reports, per (method,
   decision point): basket count, avg legs, avg cost/unit, **basket hit
   rate**, de-vigged predicted win prob and the calibration gap, avg P&L
   per unit, total P&L, ROI, and how often the eventual winner **wasn't
   yet tradeable** at the decision point (a structural loss the live
   system would also take), plus a per-series breakdown.

### First look (tiny sample — mechanics check, not a result)

`python backtest.py --days 150 --series KXLIVTOUR --decision-hours 72,24
--max-overround 2.5` (only 3 LIV events exist, fields of ~55):

| decision | baskets | hit rate | de-vig predicted | avg P&L/unit | ROI |
|---|---|---|---|---|---|
| T-72h | 3 | 66.7% | 51.4% | −0.25 | −27% (1 loss: winner not yet tradeable) |
| T-24h | 3 | 100% | 58.4% | +0.06 | +6% |

The **calibration gap is the signal**: the de-vig predicts the favorites
basket wins ~55% of the time; at T-24h it went 3/3. That's favorite-
longshot bias showing up exactly where `favorites_basket` bets on it. But
n=3 — run it across every series (`--days 300`, no `--series`) for
`KXPGATOUR`/`KXKFTOUR`/`KXLPGATOUR`/`KXCHAMPTOUR`/`KXDPWORLDTOUR` before
believing anything.

## Limitations

- **No order-book depth / partial fills.** A basket leg is assumed to
  fill in full at the displayed ask (live) / last trade (backtest). Golf
  player books are thin; buying 20 contracts across 40 legs will move
  price and partially miss. This is the single biggest gap between the
  backtest number and reality — same class of unmodelled risk as the
  other two experiments, worse here because a basket is many
  simultaneous thin-book orders.
- **Backtest price is last-trade, not the ask.** No historical spread
  exists to reconstruct, so entry is priced optimistically. Real ROI is
  below the reported ROI by roughly the half-spread × legs.
- **Winner not always tradeable at the decision point.** A player with no
  trade before the decision timestamp is excluded from the basket —
  sometimes that's the eventual winner (a late-forming contender, a
  Monday qualifier). Reported explicitly; it's a real structural loss the
  live system shares, not a backtest artefact.
- **`devig_edge` barely testable historically** — see its section above.
- **Roster churn.** Withdrawals resolve a player NO early; the field the
  basket was sized against isn't quite the field that tees off. Not
  modelled.
- **Single-snapshot backtest.** Each decision point is independent; the
  live system re-scans and can add newly-qualifying legs as prices move
  through the week. Run multiple `--decision-hours` to see the time
  profile, but the compounding "keep adding legs" behaviour isn't
  simulated.
- **De-vig circularity.** `fair` is derived from the same prices being
  traded against, so `mkt-consistent E[value]` and `predicted win prob`
  measure internal consistency, not truth. Only the backtest's realized
  hit rate against actual winners is a real number.
- **Overround gate is blunt.** `MAX_OVERROUND` rejects an event whole;
  it doesn't notice *where* the vig sits (all on the favorite vs spread
  evenly), which is what actually determines whether a subset is
  mispriced.

## Status

Built 2026-08-27. Discovery, both selection methods, the fee model, the
dry-run order path, and the backtest all run end-to-end against the live
and settled Kalshi API. **Never run with real money** — nothing here has
been validated beyond "the code executes and the arithmetic is
internally consistent". Before flipping `GOLF_FIELD_ALPHA_DRY_RUN=false`:
run `backtest.py --days 300` across all series (n is currently far too
small), watch a full tournament in dry-run to see basket sizing and
re-entry behaviour over a real week, and place one tiny real order to
verify the execution path (the crypto experiments both found real bugs
doing exactly that). See `live/README.md`.
