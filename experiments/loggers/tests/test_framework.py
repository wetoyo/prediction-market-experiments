"""The logger framework (store.py, runner.py) with a fake logger module. No
network, no systemd."""

import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner  # noqa: E402
import store  # noqa: E402


def _fake_logger(rows_per_tick=1, fail_on_tick=None):
    module = types.ModuleType("fake_logger")

    def collect(ctx):
        if ctx.tick == fail_on_tick:
            raise RuntimeError("boom")
        for i in range(rows_per_tick):
            yield "rows", {"ts": ctx.now, "i": i, "pad": "x" * 200}
        if ctx.due("slow", 3600):
            yield "slow", {"ts": ctx.now}

    module.collect = collect
    sys.modules["fake_logger"] = module


def _task(tmp_path, seconds=2.0, **kw):
    task = {
        "name": "t", "logger": "fake_logger", "interval_seconds": 0.2, "max_mb": 50, "min_free_mb": 0,
        "flush_seconds": 0.5, "out_dir": str(tmp_path / "data"),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(), **kw,
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task))
    return str(path)


def test_runs_until_expiry_and_survives_a_failing_tick(tmp_path):
    _fake_logger(fail_on_tick=2)
    started = time.time()
    assert runner.run(_task(tmp_path, seconds=1.5)) == "expired"
    assert time.time() - started < 5
    status = json.loads((tmp_path / "data" / "status.json").read_text())
    assert status["state"] == "finished" and status["errors"] == 1 and status["ticks"] >= 5
    rows = list(store.read(tmp_path / "data", "rows"))
    assert len(rows) == status["ticks"] - 1  # every tick but the failed one
    assert len(list(store.read(tmp_path / "data", "slow"))) == 1  # ctx.due: once an hour


def test_stops_at_the_size_cap(tmp_path):
    _fake_logger(rows_per_tick=2000)
    reason = runner.run(_task(tmp_path, seconds=60, max_mb=0.01, flush_seconds=0))
    assert reason.startswith("size cap")


def test_read_skips_a_truncated_member(tmp_path):
    writer = store.Writer(tmp_path, flush_seconds=0)
    for i in range(3):
        writer.add("s", {"ts": i})
        writer.flush(now=1792600000)
    path = next((tmp_path / "s").glob("*.jsonl.gz"))
    data = path.read_bytes()
    path.write_bytes(data[:-7])  # crash mid-write of the last member
    assert [r["ts"] for r in store.read(tmp_path, "s")] == [0, 1]
    assert [r["ts"] for r in store.read(tmp_path, "s", start=1)] == [1]
