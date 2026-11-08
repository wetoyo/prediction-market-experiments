"""Settlement stations for Kalshi's daily temperature markets.

Each market's rules_primary names an NWS climate report, e.g. "the maximum
temperature recorded at New York City (CLINYC) for Nov 8, 2026". The CLI
code's last three letters are the station, and the ICAO id is "K" + those
letters (CLINYC -> KNYC, CLIMDW -> KMDW).

The climate day is midnight to midnight local *standard* time all year, so
during DST it runs 1am-1am on the wall clock. That's why each station keeps
a fixed standard UTC offset instead of a time zone.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


@dataclass(frozen=True)
class Station:
    cli: str           # "CLINYC"
    icao: str          # "KNYC"
    std_utc_offset: int  # hours, e.g. -5 for Eastern

    def day_window(self, day: date) -> tuple[datetime, datetime]:
        """UTC [start, end) of the climate day `day`."""
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=self.std_utc_offset)
        return start, start + timedelta(days=1)

    def climate_date(self, when: datetime) -> date:
        """The climate day a UTC instant falls in."""
        return (when.astimezone(timezone.utc) + timedelta(hours=self.std_utc_offset)).date()


_OFFSETS = {
    # Eastern
    "NYC": -5, "DCA": -5, "BOS": -5, "TTN": -5, "ATL": -5, "MIA": -5, "EWR": -5, "PHL": -5, "SDF": -5,
    # Central
    "MDW": -6, "DFW": -6, "MSY": -6, "OKC": -6, "AUS": -6, "HOU": -6, "SAT": -6, "MSP": -6,
    # Mountain (Phoenix has no DST, same standard offset)
    "DEN": -7, "PHX": -7,
    # Pacific
    "LAX": -8, "SAN": -8, "SEA": -8, "SFO": -8, "LAS": -8,
}

STATIONS: dict[str, Station] = {
    f"CLI{code}": Station(cli=f"CLI{code}", icao=f"K{code}", std_utc_offset=off) for code, off in _OFFSETS.items()
}


def station_for_cli(cli: str) -> Station | None:
    return STATIONS.get(cli.upper())
