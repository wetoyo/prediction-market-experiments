"""weather_mos_alpha configuration.

Every Kalshi daily-high series with a US settlement station that has NWS
MOS guidance. Each settles on the station's NWS climate report (CLIxxx):
via the NWS CLI product until 2026-08-14, via The Weather Company's copy
of it from 2026-08-15 (same station, same number). Stations were read off
each series' `rules_primary` text.
"""

import os

# series -> (ICAO station for MOS / IEM CLI, IANA time zone)
CITIES = {
    "KXHIGHNY": ("KNYC", "America/New_York"),       # Central Park
    "KXHIGHCHI": ("KMDW", "America/Chicago"),       # Midway
    "KXHIGHMIA": ("KMIA", "America/New_York"),
    "KXHIGHAUS": ("KAUS", "America/Chicago"),       # Bergstrom
    "KXHIGHDEN": ("KDEN", "America/Denver"),
    "KXHIGHLAX": ("KLAX", "America/Los_Angeles"),
    "KXHIGHPHIL": ("KPHL", "America/New_York"),
    "KXHIGHTLV": ("KLAS", "America/Los_Angeles"),
    "KXHIGHTSATX": ("KSAT", "America/Chicago"),
    "KXHIGHTPHX": ("KPHX", "America/Phoenix"),
    "KXHIGHTHOU": ("KHOU", "America/Chicago"),      # Hobby
    "KXHIGHTOKC": ("KOKC", "America/Chicago"),
    "KXHIGHTATL": ("KATL", "America/New_York"),
    "KXHIGHTBOS": ("KBOS", "America/New_York"),
    "KXHIGHTDAL": ("KDFW", "America/Chicago"),
    "KXHIGHTMIN": ("KMSP", "America/Chicago"),
    "KXHIGHTSFO": ("KSFO", "America/Los_Angeles"),
    "KXHIGHTNOLA": ("KMSY", "America/Chicago"),
    "KXHIGHTDC": ("KDCA", "America/New_York"),
    "KXHIGHTSEA": ("KSEA", "America/Los_Angeles"),
    "KXHIGHTSAN": ("KSAN", "America/Los_Angeles"),
    "KXHIGHTEWR": ("KEWR", "America/New_York"),
    "KXHIGHTTTN": ("KTTN", "America/New_York"),
}

# MOS models pulled from the IEM archive. NBS = NBM short-range text
# guidance (hourly runs, carries its own spread `xnd`); GFS = MAV; NAM = MET.
MOS_MODELS = ("NBS", "GFS", "NAM")

# Hours between a MOS run's nominal time and when it is usable. Real
# availability is ~1-1.5h (NBM) and ~3.5-4h (GFS/NAM MOS) after runtime;
# the backtest uses these, conservatively rounded up.
MOS_DELAY_HOURS = {"NBS": 2.0, "GFS": 4.0, "NAM": 4.0}

# Decision times (local clock): (label, days before the event day, hour).
DECISION_TIMES = (("d-1_10h", 1, 10), ("d0_08h", 0, 8))

# Backtest window (event dates) and the chronological split.
BACKTEST_START = "2026-01-15"
BACKTEST_END = "2026-10-22"
# Train on event dates before this (65% of the window), test on the rest.
SPLIT_DATE = "2026-07-16"
# Error model may use CLI history from this date (strictly before the split).
ERROR_HISTORY_START = "2023-01-01"

FEE_RATE = 0.07

# --- live runner -------------------------------------------------------------
DRY_RUN = os.environ.get("WXA_DRY_RUN", "1") != "0"
ORDER_TAG = "wxa"
