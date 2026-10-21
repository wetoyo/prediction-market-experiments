"""On-disk format for logger output: one directory per stream, one gzipped
JSONL file per UTC day, appended to as a series of gzip members.

    <out_dir>/<stream>/<YYYY-MM-DD>.jsonl.gz

Each flush appends one complete gzip member (several minutes of records), so
a process killed mid-run loses at most the unflushed buffer, and a member
cut off by a crash or a full disk is skipped on read rather than making the
whole day unreadable. `read` is what a backtest uses to load it back.
"""

import gzip
import json
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path


class Writer:
    def __init__(self, out_dir: Path, flush_seconds: float = 300.0):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.flush_seconds = flush_seconds
        self._buffers: dict[str, list[str]] = {}
        self._last_flush = time.time()
        self.rows_written: dict[str, int] = {}
        self.bytes_on_disk = sum(p.stat().st_size for p in self.out_dir.rglob("*.jsonl.gz"))

    def add(self, stream: str, record: dict) -> None:
        self._buffers.setdefault(stream, []).append(json.dumps(record, separators=(",", ":")))

    def maybe_flush(self, now: float) -> None:
        if now - self._last_flush >= self.flush_seconds:
            self.flush(now)

    def flush(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        for stream, lines in self._buffers.items():
            if not lines:
                continue
            path = self.out_dir / stream / f"{day}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            member = gzip.compress(("\n".join(lines) + "\n").encode(), compresslevel=9)
            with open(path, "ab") as fh:
                fh.write(member)
            self.bytes_on_disk += len(member)
            self.rows_written[stream] = self.rows_written.get(stream, 0) + len(lines)
            lines.clear()
        self._last_flush = now


def _members(data: bytes):
    """Yields each complete gzip member's text; stops at a truncated one."""
    while data:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            text = d.decompress(data)
        except zlib.error:
            return
        if not d.eof:
            return  # cut off mid-member (crash or full disk while writing)
        yield text.decode()
        data = d.unused_data


def read(out_dir, stream: str, start: float | None = None, end: float | None = None):
    """Yields a stream's records in file order, optionally only those whose
    `ts` is within [start, end]."""
    for path in sorted((Path(out_dir) / stream).glob("*.jsonl.gz")):
        for text in _members(path.read_bytes()):
            for line in text.splitlines():
                if not line:
                    continue
                record = json.loads(line)
                ts = record.get("ts")
                if ts is not None and ((start is not None and ts < start) or (end is not None and ts > end)):
                    continue
                yield record
