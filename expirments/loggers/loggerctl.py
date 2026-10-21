"""Starts, stops and lists logging tasks on this machine (the Pi).

    python loggerctl.py start NAME --logger MODULE --days D [--interval S] [--max-mb M] [--option k=v ...]
    python loggerctl.py status
    python loggerctl.py stop NAME
    python loggerctl.py resume [NAME]     # after a reboot: relaunch unexpired tasks that aren't running
    python loggerctl.py remove NAME [--data]

Run it with the Python the loggers should use (the repo's .venv). Each task
is a JSON file in $LOGGERS_HOME/tasks/ and its data goes to
$LOGGERS_HOME/data/NAME/ (LOGGERS_HOME defaults to ~/loggers). A task runs
as a *transient* systemd unit, logger-NAME: it has a fixed expiry, nothing
restarts it, and it doesn't come back after a reboot unless you `resume`.
The unit runs at low priority (Nice 10, idle I/O, CPUQuota, MemoryMax), so
a logger can't starve the live trading service sharing the box. It has a
hard runtime limit of the task's remaining time plus 10 minutes, on top of
the runner's own expiry.

`start` refuses a task whose size cap doesn't fit: every task's max_mb,
plus a 300 MB floor kept free, must fit in the free disk. Data stays after
a task finishes until you `remove --data` it; pull it off first.
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("LOGGERS_HOME") or Path.home() / "loggers")
TASKS = HOME / "tasks"
DATA = HOME / "data"
MIN_FREE_MB = 300


def _unit(name: str) -> str:
    return f"logger-{name}"


def _load(name: str) -> dict:
    path = TASKS / f"{name}.json"
    if not path.exists():
        sys.exit(f"no task {name!r} ({path})")
    return json.loads(path.read_text())


def _tasks() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(TASKS.glob("*.json"))] if TASKS.exists() else []


def _active(name: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", _unit(name)]).returncode == 0


def _remaining(task: dict) -> float:
    return datetime.fromisoformat(task["expires_at"]).timestamp() - time.time()


def _status_file(task: dict) -> dict:
    try:
        return json.loads((Path(task["out_dir"]) / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def _launch(task: dict) -> None:
    remaining = _remaining(task)
    if remaining <= 0:
        sys.exit(f"task {task['name']} expired at {task['expires_at']}")
    if _active(task["name"]):
        sys.exit(f"task {task['name']} is already running")
    subprocess.run(["sudo", "systemctl", "reset-failed", _unit(task["name"])], stderr=subprocess.DEVNULL)
    cmd = [
        "sudo", "systemd-run", f"--unit={_unit(task['name'])}", f"--uid={getpass.getuser()}",
        f"--working-directory={HERE}", "--setenv=PYTHONUNBUFFERED=1",
        f"--description=backtest logger {task['name']} ({task['logger']}) until {task['expires_at']}",
        f"--property=RuntimeMaxSec={int(remaining) + 600}",
        f"--property=MemoryMax={task.get('memory_max', '250M')}",
        f"--property=CPUQuota={task.get('cpu_quota', '50%')}",
        "--property=Nice=10", "--property=IOSchedulingClass=idle",
        sys.executable, str(HERE / "runner.py"), str(TASKS / f"{task['name']}.json"),
    ]
    subprocess.run(cmd, check=True)
    print(f"started {_unit(task['name'])}: logs with `journalctl -u {_unit(task['name'])}`")


def cmd_start(args) -> None:
    TASKS.mkdir(parents=True, exist_ok=True)
    if (TASKS / f"{args.name}.json").exists():
        sys.exit(f"task {args.name!r} exists -- `resume` it, or `remove` it first")
    committed = sum(float(t.get("max_mb", 0)) - _status_file(t).get("bytes", 0) / 1e6 for t in _tasks())
    free_mb = shutil.disk_usage(HOME).free / 1e6
    if committed + args.max_mb + MIN_FREE_MB > free_mb:
        sys.exit(f"not enough disk: {free_mb:.0f} MB free, other tasks may still write {committed:.0f} MB, "
                 f"this one up to {args.max_mb:.0f} MB, and {MIN_FREE_MB} MB must stay free")
    try:
        __import__(args.logger)
    except Exception as exc:
        sys.exit(f"logger module {args.logger!r} doesn't import: {exc!r}")
    options = dict(o.split("=", 1) for o in args.option)
    task = {
        "name": args.name, "logger": args.logger, "interval_seconds": args.interval, "max_mb": args.max_mb,
        "min_free_mb": MIN_FREE_MB, "options": options, "out_dir": str(DATA / args.name),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=args.days)).isoformat(timespec="seconds"),
    }
    (TASKS / f"{args.name}.json").write_text(json.dumps(task, indent=1))
    _launch(task)


def cmd_status(args) -> None:
    free_mb = shutil.disk_usage(HOME if HOME.exists() else Path.home()).free / 1e6
    print(f"{free_mb:.0f} MB free on disk")
    for task in _tasks():
        st = _status_file(task)
        state = "running" if _active(task["name"]) else st.get("state", "never ran")
        if state == "running" or not st.get("finished_reason"):
            when = f"{_remaining(task) / 86400:.1f}d left" if _remaining(task) > 0 else "expired"
        else:
            when = st["finished_reason"]
        print(f"{task['name']:24s} {state:9s} {when:28s} {st.get('bytes', 0) / 1e6:7.2f}/{task['max_mb']:g} MB "
              f"ticks {st.get('ticks', 0)} errors {st.get('errors', 0)}")
        if st.get("last_error"):
            print(f"{'':24s} last error {st['last_error']['at']}: {st['last_error']['error'][:150]}")


def cmd_stop(args) -> None:
    subprocess.run(["sudo", "systemctl", "stop", _unit(args.name)], check=True)


def cmd_resume(args) -> None:
    tasks = [_load(args.name)] if args.name else _tasks()
    for task in tasks:
        if _remaining(task) > 0 and not _active(task["name"]):
            _launch(task)


def cmd_remove(args) -> None:
    task = _load(args.name)
    if _active(args.name):
        sys.exit(f"task {args.name} is running -- stop it first")
    if args.data:
        shutil.rmtree(task["out_dir"], ignore_errors=True)
    (TASKS / f"{args.name}.json").unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("name")
    p.add_argument("--logger", required=True, help="logger module in this directory")
    p.add_argument("--days", type=float, required=True)
    p.add_argument("--interval", type=float, default=60.0, help="seconds between ticks")
    p.add_argument("--max-mb", type=float, default=50.0)
    p.add_argument("--option", action="append", default=[], help="k=v passed to the logger's setup()")
    p.set_defaults(func=cmd_start)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("stop")
    p.add_argument("name")
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("resume")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_resume)
    p = sub.add_parser("remove")
    p.add_argument("name")
    p.add_argument("--data", action="store_true", help="also delete its data")
    p.set_defaults(func=cmd_remove)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
