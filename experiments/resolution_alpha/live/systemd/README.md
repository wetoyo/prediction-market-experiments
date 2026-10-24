# systemd units (Raspberry Pi deployment)

Version-controlled copies of the two units that run resolution_alpha on the Pi.
They were previously hand-written straight into `/etc/systemd/system/` and drifted
(the kill-switch unit had a stale hardcoded `BASELINE_DOLLARS=100 THRESHOLD_PCT=-15`).

Both assume the repo at `/home/wetoyo/prediction-market-experiments` and the
repo-root venv at `.venv/`. Edit the paths if that changes.

## What they do

- **`resolution-alpha.service`** — runs `runner.py` directly (not via
  `start_live.sh`), env from `live/.env`. `Restart=on-failure`, but not out of a
  deliberate SIGTERM/SIGKILL. `Wants=` the kill-switch unit, so starting this
  starts both.
- **`resolution-alpha-killswitch.service`** — execs `live/arm_killswitch.sh`,
  which resolves the runner PID from systemd, computes a **fresh** equity
  baseline (cash + open-position cost basis), and hands off to
  `pnl_killswitch.sh` at `-10%`. `pnl_killswitch.sh` self-exits without arming
  when `RESOLUTION_ALPHA_DISABLE_KILLSWITCH` is truthy in `.env`, so this unit is
  a clean no-op in that case (no bad-baseline killer left running).

## Moving the Pi from `expirments/` to `experiments/` (one time)

The top-level folder was renamed from `expirments/` to `experiments/` on
2026-10-23. `git pull` moves only the tracked files. Everything gitignored
stays behind in the old folder: `.env`, `live/logs/` (the ledger, the
order records and the live DB), the loggers' `data/` and any `*.pid`.
The installed units also still point at the old path. So do this once, with
the service stopped:

```sh
cd ~/prediction-market-experiments
sudo systemctl stop resolution-alpha.service
git pull                                   # creates experiments/, leaves the ignored files in expirments/
rsync -a expirments/ experiments/          # carry .env, logs/, data/ etc. across (never overwrites tracked files' content)
find expirments -type f | head             # what's left should be only ignored files rsync just copied
rm -rf expirments
chmod +x experiments/resolution_alpha/live/arm_killswitch.sh
sudo cp experiments/resolution_alpha/live/systemd/resolution-alpha*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart resolution-alpha.service
```

Logger units started by `loggerctl.py` before the rename run from the old
path. Stop them before the move and `resume` them afterwards.

## Install / update on the Pi

```sh
cd ~/prediction-market-experiments
git pull
chmod +x experiments/resolution_alpha/live/arm_killswitch.sh
sudo cp experiments/resolution_alpha/live/systemd/resolution-alpha*.service /etc/systemd/system/
sudo systemctl daemon-reload
# start (pulls in the kill-switch unit too):
sudo systemctl restart resolution-alpha.service
```

## Watch it

```sh
journalctl -u resolution-alpha -f            # runner (banner + trades only under LIGHTWEIGHT_MODE)
journalctl -u resolution-alpha-killswitch -f # kill-switch heartbeat / "not arming"
```

## Stop

```sh
sudo systemctl stop resolution-alpha.service   # kill-switch stops with it (BindsTo/PartOf)
```
