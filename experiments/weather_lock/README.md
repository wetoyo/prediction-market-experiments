# weather_lock

**Thesis:** Kalshi's daily high and low temperature markets (`KXHIGHNY`,
`KXHIGHCHI`, `KXLOWTBOS`, ... about 48 series, 24 US stations) settle on the
day's max or min at one named NWS station. The highest temperature reported
so far is a hard floor on the final high, and the lowest so far is a hard
ceiling on the final low. Once the reports pass a bracket, that bracket is
decided. If the book still offers the decided side below about 98c, buy it.

There's no forecast in this. It trades only outcomes the station's own
reports have already settled, a few degrees inside a safety margin.

Data source: NWS station observations (api.weather.gov), the same endpoint as
the `nws` vendor in `D:\Files\Code\vendors` (`vendors/nws`, `observations`).
It's called directly here, so the runner needs no extra dependency.

## How a lock is decided

1. **Station and day.** Each market's `rules_primary` names a climate report,
   e.g. `CLINYC`, which is station `KNYC`. The event ticker gives the day
   (`KXHIGHNY-26NOV08`). The climate day runs midnight to midnight in local
   *standard* time, so 1am-1am in summer (`stations.py`).
2. **Bounds** (`nws_obs.py`). Readings come from each raw METAR, with known
   precision: the T-group is 0.1C, the body group is whole C (+-0.9F), and the
   6-hour max/min groups count only when their whole window is inside the day.
   The bound is the 2nd-best reading (`MIN_CONFIRMING_OBS`), so one bad report
   can't create a lock. It's then rounded the way the climate report rounds.
3. **Lock** (`lock.py`). `LOCK_MARGIN_F` (1F) comes off first. A high of at
   least H kills every bracket that tops out below H. A low of at most L kills
   every bracket that starts above L.
4. **Entry.** An IOC buy at the highest price that still clears `MIN_EDGE`
   (2c after fees). Kalshi fills every level up to that price and cancels the
   rest. Caps apply per market, per event and in total, plus a fraction of the
   bankroll per order.

Locks only ever go one way. The runner never bets that a high has peaked, even
after the day ends, because hourly reports can miss the true peak.

## Checked against live data (2026-11-08)

- One scan found 65 locked markets across yesterday's events. Every one was
  already priced at 100% by the book (no ask on the locked side). So the
  bounds agree with the market, and none of those were tradeable.
- For each of yesterday's 18 open events, the computed bound sat at or inside
  the bracket the market priced at 97-99c (0 violations, 0-2F of slack).
- **Not yet known:** how often, and for how long, a lock is left priced below
  98c intraday, when a new high is posted mid-afternoon. That's what
  `live/state/signals.jsonl` records: one line per locked market per tick, with
  the ask and size. Read that before scaling up.

## Risks

- **Settlement source.** Most series settle on "The Weather Company" figure
  for the station, not the NWS climate report itself. Preliminary readings
  there can differ by rounding. `LOCK_MARGIN_F` covers this. Don't set it
  to 0.
- **Data errors and revisions.** A bad sensor reading that the climate report
  later drops could fake a lock. `MIN_CONFIRMING_OBS=2` covers a single
  outlier, but not a sustained sensor fault.
- **Fill risk is small but real.** IOC orders never rest. A partial fill is
  simply booked as a partial.
- **No exit logic.** Positions ride to settlement the next morning.

## Running it

```
cd experiments/weather_lock
pip install -r requirements.txt
python strategy.py                  # one scan, prints locked markets, places nothing
python -m pytest tests              # offline tests

cd live
cp .env.example .env                # edit
./start_live.sh                     # or start_live.bat on Windows; ./stop_live.sh to stop
```

The loop is `strategy.py --loop 60 --execute`. It stays dry-run until
`WEATHER_LOCK_DRY_RUN=false` **and** `KALSHI_API_KEY_ID` /
`KALSHI_PRIVATE_KEY_PATH` are set. Dry-run "fills" go to
`positions_state.json.dryrun`, never the live state file.

On the Pi, use the systemd unit in `live/systemd/weather-lock.service`. Copy it
to `/etc/systemd/system/`, then `systemctl enable --now weather-lock`.
resolution_alpha is live there, so first read `../MULTI_RUNNER_HANDOFF.md`.
This runner registers with the allocation guard like the others, and it can't
start trading beside resolution_alpha until that runner is in shared mode.

Order tag: `wl` (`client_order_id = wl-<y|n>-<hex>`).

## Files

| file | role |
|---|---|
| `stations.py` | CLI code -> ICAO and standard UTC offset; climate-day windows |
| `nws_obs.py` | METAR parsing with precision bounds; `day_bounds()` |
| `discovery.py` | Kalshi series/market discovery and parsing |
| `lock.py` | `locked_side()` and `plan_entry()` |
| `order_manager.py` | IOC orders, dry-run by default, TaggedLedger wiring |
| `positions_store.py` | persisted positions and caps; startup reconcile |
| `strategy.py` | the loop |
| `live/` | start/stop scripts, `.env.example`, systemd unit; `state/` holds the signal log and ledger status |
