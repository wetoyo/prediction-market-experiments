"""Runs one logging task from its task file until it expires, then exits.

    python runner.py <task.json>

Launched by loggerctl.py as a transient systemd unit, never directly by a
service manager: nothing restarts it, and nothing brings it back after a
reboot (loggerctl.py resume does that, for tasks that haven't expired).

Every `interval_seconds` it calls the task's logger module's
`collect(ctx)`, which yields (stream, record) pairs, and buffers them to
store.Writer. It stops on its own -- flushing first -- at whichever comes
first: the task's `expires_at`, its data passing `max_mb`, the disk's free
space falling under `min_free_mb`, its memory passing `max_rss_mb` (checked
here because the Pi's kernel has the memory cgroup disabled, so systemd's
MemoryMax isn't enforced there), or SIGTERM. An exception inside one tick
is logged and counted, not fatal. Progress goes to <out_dir>/status.json.

A logger module (see btc_implied_prob_quotes.py) is any module in this
directory with:
    collect(ctx) -> iterable of (stream, record)
and optionally setup(options). `ctx.now` is the tick's epoch time,
`ctx.due(key, seconds)` is True at most once every `seconds` (for things
logged less often than every tick), `ctx.state` is a dict kept across ticks.
"""

import importlib
import json
import logging
import os
import shutil
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("loggers.runner")


class Context:
    def __init__(self):
        self.now = time.time()
        self.tick = 0
        self.state: dict = {}
        self._last: dict[str, float] = {}

    def due(self, key: str, seconds: float) -> bool:
        last = self._last.get(key)
        if last is None or self.now - last >= seconds:
            self._last[key] = self.now
            return True
        return False


def _rss_mb() -> float | None:
    """Current resident memory, Linux only (None elsewhere)."""
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, AttributeError):
        return None


def _parse(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def run(task_path: str) -> str:
    task = json.loads(Path(task_path).read_text())
    expires = _parse(task["expires_at"])
    interval = float(task.get("interval_seconds", 60))
    max_bytes = float(task.get("max_mb", 50)) * 1e6
    min_free = float(task.get("min_free_mb", 300)) * 1e6
    max_rss = float(task.get("max_rss_mb", 200))
    out_dir = Path(task["out_dir"])
    writer = store.Writer(out_dir, flush_seconds=float(task.get("flush_seconds", 300)))
    status_path = out_dir / "status.json"

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append("SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: stopping.append("SIGINT"))

    module = importlib.import_module(task["logger"])
    if hasattr(module, "setup"):
        module.setup(task.get("options") or {})

    ctx = Context()
    status = {"task": task["name"], "logger": task["logger"], "started_at": _iso(time.time()),
              "expires_at": task["expires_at"], "ticks": 0, "errors": 0, "last_error": None, "state": "running"}

    def write_status():
        status.update(updated_at=_iso(time.time()), bytes=int(writer.bytes_on_disk),
                      rows=dict(writer.rows_written), ticks=ctx.tick)
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=1))
        tmp.replace(status_path)

    log.info("task %s: logger %s every %.0fs until %s, cap %.0f MB", task["name"], task["logger"], interval,
             task["expires_at"], max_bytes / 1e6)
    reason = None
    while True:
        now = time.time()
        if stopping:
            reason = stopping[0]
        elif now >= expires:
            reason = "expired"
        elif writer.bytes_on_disk >= max_bytes:
            reason = f"size cap {max_bytes / 1e6:.0f} MB reached"
        elif shutil.disk_usage(out_dir).free < min_free:
            reason = f"disk free below {min_free / 1e6:.0f} MB"
        elif (rss := _rss_mb()) is not None and rss > max_rss:
            reason = f"memory {rss:.0f} MB over {max_rss:.0f} MB"
        if reason:
            break

        ctx.now = now
        ctx.tick += 1
        try:
            for stream, record in module.collect(ctx):
                writer.add(stream, record)
        except Exception as exc:
            status["errors"] += 1
            status["last_error"] = {"at": _iso(now), "error": repr(exc)[:500]}
            log.error("tick %d failed: %s", ctx.tick, traceback.format_exc(limit=5))
        writer.maybe_flush(time.time())
        write_status()

        next_tick = now + interval
        while not stopping and time.time() < next_tick:
            time.sleep(min(1.0, max(0.0, next_tick - time.time())))

    writer.flush()
    status["state"] = "finished"
    status["finished_reason"] = reason
    write_status()
    log.info("task %s finished: %s", task["name"], reason)
    return reason


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    run(sys.argv[1])
