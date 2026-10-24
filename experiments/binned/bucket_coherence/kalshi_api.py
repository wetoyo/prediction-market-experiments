"""Throttled, cached client for Kalshi's public REST API.

All agents in this repo share one outbound IP, so every request goes through
one process-wide throttle (MAX_RPS, default 3.5/s, under the 4/s budget) and
backs off exponentially on 429 and 5xx. GET responses are cached under
data/http/ as gzipped JSON keyed by path + params, so re-running the screen
costs no requests. Pass cache=False for live snapshots (order books).
"""

import gzip
import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_DIR = DATA_DIR / "http"
MAX_RPS = float(os.environ.get("BCO_MAX_RPS", "3.5"))

_lock = threading.Lock()
_last = [0.0]
_session = requests.Session()
STATS = {"requests": 0, "cache_hits": 0, "retries": 0}


def _throttle() -> None:
    with _lock:
        wait = _last[0] + 1.0 / MAX_RPS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def _key(path: str, params: dict) -> Path:
    raw = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    h = hashlib.sha1(raw.encode()).hexdigest()
    return CACHE_DIR / h[:2] / f"{h}.json.gz"


def get(path: str, cache: bool = True, **params) -> dict:
    params = {k: v for k, v in params.items() if v is not None}
    p = _key(path, params)
    if cache and p.exists():
        STATS["cache_hits"] += 1
        with gzip.open(p, "rt") as f:
            return json.load(f)
    for attempt in range(10):
        _throttle()
        try:
            r = _session.get(BASE_URL + path, params=params, timeout=60)
            STATS["requests"] += 1
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200:
            d = r.json()
            if cache:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                with gzip.open(tmp, "wt") as f:
                    json.dump(d, f)
                tmp.replace(p)
            return d
        if r is not None and r.status_code not in (429,) and r.status_code < 500:
            r.raise_for_status()
        STATS["retries"] += 1
        time.sleep(min(2 ** attempt, 60) * (0.5 + random.random()))
    raise RuntimeError(f"gave up on {path} {params}")


def paginate(path: str, key: str, cache: bool = True, max_pages: int = 1000, **params) -> list[dict]:
    out, cursor = [], None
    for _ in range(max_pages):
        d = get(path, cache=cache, cursor=cursor, **params)
        rows = d.get(key, []) or []
        out.extend(rows)
        cursor = d.get("cursor")
        if not cursor or not rows:
            break
    return out
