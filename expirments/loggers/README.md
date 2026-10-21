# loggers

Cheap, time-limited data loggers for backtests, meant to run on the Pi next
to the live trading service. Record only what can't be fetched again later
(order book quotes, model inputs such as Deribit's implied-vol surface). Pull
the data off afterwards and backtest it somewhere else.

- `runner.py` runs one task until it expires, or until its data hits the size
  cap, the disk gets low, or it's stopped. It always flushes before exiting.
- `loggerctl.py` starts, stops, lists and resumes tasks, each as a
  **transient** systemd unit (`logger-<name>`). Nothing restarts a task, and a
  task doesn't come back after a reboot unless you `resume` it. Units run at
  Nice 10, with idle I/O, `CPUQuota=50%` and a hard runtime limit. The runner
  stops itself past 200 MB of memory, because the Pi's kernel ignores systemd's
  `MemoryMax`.
- `store.py` handles the format: `data/<task>/<stream>/<YYYY-MM-DD>.jsonl.gz`,
  appended as one gzip member per flush, so a crash loses at most a few
  minutes. `store.read(dir, stream, start, end)` loads it back.
- Logger modules (one per strategy) define `collect(ctx)`, which yields
  `(stream, record)` pairs. See `runner.py` for the context object.

## Loggers

| module | what | size |
|---|---|---|
| `btc_implied_prob_quotes` | per minute: each tradable BTC market's yes bid/ask with btc_implied_prob's model price (P, sigma, forward, delta), the BTC perp quote and funding estimate, and the Deribit index. Every 10 min: the Deribit smile. Hourly: realized vol | ~1.6 MB/day |

## On the Pi

The Pi runs from its own shallow clone, so the live service's checkout is
never touched:

```bash
git clone --depth 1 --recurse-submodules --shallow-submodules \
    https://github.com/wetoyo/prediction-market-experiments.git ~/loggers/repo
cd ~/loggers/repo/expirments/loggers
PY=~/prediction-market-experiments/.venv/bin/python   # the trading venv, read-only use

$PY loggerctl.py start btc-quotes --logger btc_implied_prob_quotes --days 14 --max-mb 60
$PY loggerctl.py status
$PY loggerctl.py stop btc-quotes
$PY loggerctl.py resume          # after a reboot
journalctl -u logger-btc-quotes
```

To update the logger code, run `git -C ~/loggers/repo pull` and then restart
the task with `stop` followed by `resume`. `start` refuses a task whose
`--max-mb` doesn't fit: all tasks' remaining caps plus 300 MB must be free.

Pull the data from the dev machine, then free the space:

```bash
scp -r wetoyo@100.109.148.56:loggers/data/btc-quotes ./data/
ssh wetoyo@100.109.148.56 "~/prediction-market-experiments/.venv/bin/python ~/loggers/repo/expirments/loggers/loggerctl.py remove btc-quotes --data"
```
