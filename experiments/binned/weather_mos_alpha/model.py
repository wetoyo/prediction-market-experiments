"""Forecast model: a distribution for the day's high around the MOS max.

Pure functions, no network, so the live runner and the backtest share
them. Pieces:

* `MosIndex` -- per station/model, the MOS runs as issued; `.latest(ts, day)`
  is the newest run usable at `ts` (runtime + publication delay <= ts) that
  forecasts `day`. This is the no-lookahead gate.
* `features` -- the forecasts available at one decision time.
* `ErrorModel` -- fitted on (features, observed high) pairs from the
  training period only: mu = forecast + seasonal per-city bias, sigma =
  seasonal per-city residual stdev (optionally scaled by NBM's own spread),
  per decision time. Fitting is a kernel-weighted mean/stdev over
  day-of-year (+/- `window` days, all training years).
* `bucket_prob` -- P(the CLI integer high lands in a bucket) under a normal
  (or Student-t) error, with the +/-0.5 integer rounding.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from scipy import stats


class MosIndex:
    def __init__(self, forecasts: dict, delay_hours: float):
        """forecasts: runtime_ts -> {local_date_iso: (max, sd|None)} (data.mos_max_forecasts)."""
        self.runs = sorted(forecasts)
        self.fc = forecasts
        self.delay = delay_hours * 3600.0

    def latest(self, ts: float, day: str, max_age_hours: float = 36.0):
        """(max, sd, runtime_ts) from the newest run usable at ts, or None."""
        i = bisect.bisect_right(self.runs, ts - self.delay) - 1
        while i >= 0:
            rt = self.runs[i]
            if ts - rt > max_age_hours * 3600:
                return None
            v = self.fc[rt].get(day)
            if v is not None:
                return v[0], v[1], rt
            i -= 1
        return None


def decision_ts(day: str, days_before: int, hour: int, tz: str) -> float:
    d = date.fromisoformat(day) - timedelta(days=days_before)
    return datetime(d.year, d.month, d.day, hour, tzinfo=ZoneInfo(tz)).timestamp()


def features(indexes: dict, ts: float, day: str) -> dict | None:
    """{model: max, 'sd': NBM spread} at ts, or None if NBS is missing.
    `indexes` = {model: MosIndex}."""
    out = {}
    for mdl, idx in indexes.items():
        v = idx.latest(ts, day)
        if v is not None:
            out[mdl] = v[0]
            if mdl == "NBS" and v[1] is not None:
                out["sd"] = v[1]
    if "NBS" not in out:
        return None
    return out


def point_forecast(f: dict, kind: str) -> float:
    """The raw center before bias correction."""
    if kind == "nbs":
        return f["NBS"]
    if kind == "mean3":  # NBS, GFS, NAM equally weighted where present
        vals = [f[m] for m in ("NBS", "GFS", "NAM") if m in f]
        return sum(vals) / len(vals)
    if kind == "nbs_gfs":
        return 0.5 * (f["NBS"] + f.get("GFS", f["NBS"]))
    raise ValueError(kind)


def _doy(day: str) -> int:
    return date.fromisoformat(day).timetuple().tm_yday


def _circ(a: int, b: int) -> int:
    d = abs(a - b) % 366
    return min(d, 366 - d)


@dataclass
class ErrorModel:
    """Seasonal per-city bias/spread of (observed high - point forecast)."""
    center: str = "nbs"          # point_forecast kind
    spread: str = "seasonal"     # 'seasonal' | 'nbm' (sigma = k * NBM sd, k seasonal)
    dist: str = "normal"         # 'normal' | 't'
    t_df: float = 5.0
    window: int = 30             # +/- days of year in the kernel
    min_sigma: float = 1.0
    # fitted: key (city, label) -> list of (doy, residual, nbm_sd)
    samples: dict = field(default_factory=dict)
    _cache: dict = field(default_factory=dict)

    def fit(self, rows: list[dict]) -> "ErrorModel":
        """rows: {city, label, day, f (features), obs}. Training period only."""
        self.samples = {}
        self._cache = {}
        for r in rows:
            res = r["obs"] - point_forecast(r["f"], self.center)
            self.samples.setdefault((r["city"], r["label"]), []).append(
                (_doy(r["day"]), res, r["f"].get("sd")))
        return self

    def _params(self, city: str, label: str, doy: int):
        key = (city, label, doy)
        if key in self._cache:
            return self._cache[key]
        pts = [(res, sd) for d, res, sd in self.samples.get((city, label), ()) if _circ(d, doy) <= self.window]
        if len(pts) < 30:  # thin: widen to the whole year
            pts = [(res, sd) for _d, res, sd in self.samples.get((city, label), ())]
        if len(pts) < 10:
            self._cache[key] = None
            return None
        n = len(pts)
        bias = sum(p[0] for p in pts) / n
        var = sum((p[0] - bias) ** 2 for p in pts) / (n - 1)
        sigma = max(math.sqrt(var), self.min_sigma)
        k = None
        if self.spread == "nbm":
            sds = [p[1] for p in pts if p[1]]
            if sds:
                # scale so that k * mean(NBM sd) reproduces the seasonal rms residual
                rms_sd = math.sqrt(sum(s * s for s in sds) / len(sds))
                k = sigma / rms_sd
        out = (bias, sigma, k)
        self._cache[key] = out
        return out

    def predict(self, city: str, label: str, day: str, f: dict):
        """(mu, sigma) of the day's high, or None if the city has no fit."""
        p = self._params(city, label, _doy(day))
        if p is None:
            return None
        bias, sigma, k = p
        mu = point_forecast(f, self.center) + bias
        if self.spread == "nbm" and k is not None and f.get("sd"):
            sigma = max(k * f["sd"], self.min_sigma)
        return mu, sigma

    def cdf(self, x: float, mu: float, sigma: float) -> float:
        z = (x - mu) / sigma
        if self.dist == "t":
            # unit-variance t so sigma keeps its meaning
            scale = math.sqrt((self.t_df - 2) / self.t_df)
            return float(stats.t.cdf(z / scale, self.t_df))
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bucket_bounds(m: dict) -> tuple[float, float]:
    """Continuous [lo, hi) for a Kalshi bucket on an integer temperature.

    between floor..cap (inclusive): [floor-0.5, cap+0.5)
    greater than floor:             [floor+0.5, inf)
    less than cap:                  (-inf, cap-0.5)
    """
    st = m["strike_type"]
    if st == "between":
        return m["floor_strike"] - 0.5, m["cap_strike"] + 0.5
    if st == "greater":
        return m["floor_strike"] + 0.5, math.inf
    if st == "less":
        return -math.inf, m["cap_strike"] - 0.5
    raise ValueError(st)


def bucket_contains(m: dict, temp: int) -> bool:
    lo, hi = bucket_bounds(m)
    return lo <= temp < hi


def bucket_prob(em: ErrorModel, m: dict, mu: float, sigma: float) -> float:
    lo, hi = bucket_bounds(m)
    p_hi = 1.0 if hi == math.inf else em.cdf(hi, mu, sigma)
    p_lo = 0.0 if lo == -math.inf else em.cdf(lo, mu, sigma)
    return min(max(p_hi - p_lo, 0.0), 1.0)


def int_logloss(em: ErrorModel, obs: int, mu: float, sigma: float) -> float:
    """-log P(integer high == obs): the scoring rule used to pick a model."""
    p = em.cdf(obs + 0.5, mu, sigma) - em.cdf(obs - 0.5, mu, sigma)
    return -math.log(max(p, 1e-6))

