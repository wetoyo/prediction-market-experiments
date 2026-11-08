"""Offline tests: METAR parsing, climate-day bounds, lock rules, entry sizing.

    python -m pytest tests          (from experiments/weather_lock)
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from discovery import TempMarket, event_date, kind_from_rules
from lock import locked_side, plan_entry
from nws_obs import DayBounds, Reading, c_to_f, day_bounds, parse_report
from stations import STATIONS

NYC = STATIONS["CLINYC"]
UTC = timezone.utc


def test_station_ids_and_day_window():
    assert NYC.icao == "KNYC" and STATIONS["CLIMDW"].icao == "KMDW"
    start, end = NYC.day_window(date(2026, 11, 8))
    assert start == datetime(2026, 11, 8, 5, tzinfo=UTC) and end - start == timedelta(days=1)
    # 04:30Z on Nov 8 is still Nov 7 in Eastern standard time.
    assert NYC.climate_date(datetime(2026, 11, 8, 4, 30, tzinfo=UTC)) == date(2026, 11, 7)


def test_parse_tgroup_and_six_hour_groups():
    raw = "KNYC 072351Z AUTO VRB05KT 10SM BKN065 11/02 A2986 RMK AO2 SLP102 T01110017 10144 20089"
    t = datetime(2026, 11, 7, 23, 51, tzinfo=UTC)
    rs = {r.kind: r for r in parse_report(raw, t)}
    assert rs["instant"].source == "tgroup"
    assert rs["instant"].lo_f == pytest.approx(c_to_f(11.05))
    assert rs["max6"].lo_f == pytest.approx(c_to_f(14.35))
    assert rs["min6"].hi_f == pytest.approx(c_to_f(8.95))


def test_parse_body_only_and_negative():
    t = datetime(2026, 1, 5, 12, tzinfo=UTC)
    (r,) = parse_report("KMSP 051151Z 31010KT 10SM CLR M05/M12 A3020 RMK AO2", t)
    assert r.source == "body" and r.lo_f == pytest.approx(c_to_f(-5.5)) and r.hi_f == pytest.approx(c_to_f(-4.5))
    (r,) = parse_report("KMSP 051151Z 31010KT 10SM CLR M05/M12 A3020 RMK AO2 T10561117", t)
    assert r.source == "tgroup" and r.lo_f == pytest.approx(c_to_f(-5.65))


def test_day_bounds_confirmation_and_window():
    day = date(2026, 11, 8)
    start, _ = NYC.day_window(day)
    def inst(h, c):
        return Reading("instant", start + timedelta(hours=h), c_to_f(c - 0.05), c_to_f(c + 0.05), "tgroup")
    readings = [
        inst(-1, 30.0),            # previous day: ignored
        inst(8, 15.0), inst(9, 17.2), inst(10, 17.0), inst(11, 25.0),  # 25.0 a lone spike
    ]
    b = day_bounds(NYC, day, readings, min_confirming=2, now=start + timedelta(hours=12))
    # Second-highest lower bound: 17.15C = 62.87F -> printed high >= 63.
    assert b.high_at_least == 63
    assert not b.complete
    b1 = day_bounds(NYC, day, readings, min_confirming=1, now=start + timedelta(hours=12))
    assert b1.high_at_least == 77  # 24.95C = 76.91F


def test_six_hour_group_must_be_inside_day():
    day = date(2026, 11, 8)
    start, _ = NYC.day_window(day)
    early = Reading("max6", start + timedelta(hours=3), c_to_f(19.95), c_to_f(20.05), "group6")  # window starts before the day
    inside = Reading("max6", start + timedelta(hours=12), c_to_f(18.95), c_to_f(19.05), "group6")
    base = [Reading("instant", start + timedelta(hours=h), c_to_f(9.95), c_to_f(10.05), "tgroup") for h in (1, 2)]
    b = day_bounds(NYC, day, base + [early, inside], min_confirming=1)
    assert b.high_at_least == round(c_to_f(18.95))


def _market(kind, strike_type, floor=None, cap=None, yes_bid=0.5, yes_ask=0.52):
    now = datetime(2026, 11, 8, tzinfo=UTC)
    return TempMarket(
        ticker="T", event_ticker="E", series_ticker="S", station=NYC, kind=kind, day=date(2026, 11, 8),
        strike_type=strike_type, floor=floor, cap=cap, yes_bid=yes_bid, yes_ask=yes_ask,
        yes_bid_size=100, yes_ask_size=100, tick=0.01, close_time=now, settle_time=now, exchange_index=0,
        subtitle="",
    )


def _bounds(high=None, low=None):
    return DayBounds("KNYC", date(2026, 11, 8), high, low, 10, None, False)


@pytest.mark.parametrize("strike_type,floor,cap,high,margin,expected", [
    ("greater", 68, None, 69, 0, "yes"),
    ("greater", 68, None, 69, 1, None),
    ("greater", 68, None, 70, 1, "yes"),
    ("less", None, 61, 61, 0, "no"),
    ("less", None, 61, 61, 1, None),
    ("less", None, 61, 62, 1, "no"),
    ("between", 61, 62, 63, 0, "no"),
    ("between", 61, 62, 63, 1, None),
    ("between", 61, 62, 64, 1, "no"),
    ("between", 61, 62, None, 0, None),
])
def test_high_locks(strike_type, floor, cap, high, margin, expected):
    assert locked_side(_market("high", strike_type, floor, cap), _bounds(high=high), margin) == expected


@pytest.mark.parametrize("strike_type,floor,cap,low,margin,expected", [
    ("greater", 52, None, 52, 0, "no"),
    ("greater", 52, None, 52, 1, None),
    ("greater", 52, None, 51, 1, "no"),
    ("less", None, 45, 44, 0, "yes"),
    ("less", None, 45, 44, 1, None),
    ("less", None, 45, 43, 1, "yes"),
    ("between", 48, 49, 47, 0, "no"),
    ("between", 48, 49, 46, 1, "no"),
    ("between", 48, 49, 47, 1, None),
])
def test_low_locks(strike_type, floor, cap, low, margin, expected):
    assert locked_side(_market("low", strike_type, floor, cap), _bounds(low=low), margin) == expected


def test_plan_entry_raises_limit_to_edge_and_caps():
    m = _market("high", "less", cap=61, yes_bid=0.05, yes_ask=0.06)  # NO ask = 0.95
    e, why = plan_entry(m, "no", min_edge=0.02, max_contracts=10, max_cost=100)
    assert e is not None and e.best_ask == pytest.approx(0.95)
    assert e.contracts == 10 and e.edge_at_limit >= 0.02
    assert e.limit_price >= 0.95 and e.limit_price <= 0.98
    # Cost cap binds.
    e, _ = plan_entry(m, "no", min_edge=0.02, max_contracts=10, max_cost=3.0)
    assert e.contracts == 3 and e.worst_cost <= 3.0


def test_plan_entry_refuses_thin_edge():
    m = _market("high", "less", cap=61, yes_bid=0.01, yes_ask=0.02)  # NO ask = 0.99
    e, why = plan_entry(m, "no", min_edge=0.02, max_contracts=10, max_cost=100)
    assert e is None and "edge" in why
    m = _market("high", "less", cap=61, yes_bid=0.0, yes_ask=0.01)
    assert plan_entry(m, "no", 0.02, 10, 100)[0] is None


def test_ticker_and_rules_parsing():
    assert event_date("KXHIGHNY-26NOV08") == date(2026, 11, 8)
    assert event_date("KXLOWTCHI-26JAN31-B20.5") == date(2026, 1, 31)
    assert event_date("KXHIGHNY") is None
    assert kind_from_rules("If the maximum temperature recorded at ...") == "high"
    assert kind_from_rules("If the minimum temperature recorded at ...") == "low"
