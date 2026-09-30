"""Download historical METAR temperatures for the settlement stations (read-only, free).

Source: Iowa Environmental Mesonet ASOS/METAR archive
(mesonet.agron.iastate.edu/cgi-bin/request/asos.py), routine and special
reports, air temperature in both deg C and deg F, times in UTC.

Output: research_data/weather/metar_<ICAO>.csv
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

OUT = Path(__file__).resolve().parents[2] / "research_data" / "weather"
STATIONS = ("EGLC", "KLGA", "RKSI", "ZSPD", "LFPB")
BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def fetch(station: str, start: str, end: str) -> bytes:
    y1, m1, d1 = start.split("-")
    y2, m2, d2 = end.split("-")
    params = [
        ("station", station), ("data", "tmpc"), ("data", "tmpf"), ("data", "metar"),
        ("year1", y1), ("month1", m1), ("day1", d1),
        ("year2", y2), ("month2", m2), ("day2", d2),
        ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"),
        ("missing", "M"), ("trace", "T"), ("direct", "no"),
        ("report_type", "3"), ("report_type", "4"),
    ]
    req = Request(f"{BASE}?{urlencode(params)}",
                  headers={"User-Agent": "andy-trader-research/1.0 (personal research)"})
    with urlopen(req, timeout=300) as resp:
        return resp.read()


def main(start: str = "2025-01-01", end: str = "2026-04-20") -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for station in STATIONS:
        target = OUT / f"metar_{station}.csv"
        if target.exists():
            print(f"{station}: already downloaded", flush=True)
            continue
        body = fetch(station, start, end)
        target.write_bytes(body)
        lines = body.count(b"\n")
        print(f"{station}: {lines:,} reports, {len(body) / 1e6:.1f} MB", flush=True)
        time.sleep(3)  # be polite to a free public archive
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
