"""Print the official rules text and resolution source for recent weather events per city.

Read-only calls to Polymarket's public Gamma API. The settlement station and
rounding rule decide what a historical replay must compare against, so they
are read from the rules, not assumed.
"""

import json
import sys
import time
from urllib.request import Request, urlopen

import duckdb
from pathlib import Path

MARKETS = (Path(__file__).resolve().parents[2] / "research_data" / "polymarket"
           / "markets.parquet").as_posix()
CITIES = sys.argv[1:] or ["London", "NYC", "Seoul", "Hong Kong", "Shanghai", "Paris"]


def gamma(path: str) -> object:
    req = Request(f"https://gamma-api.polymarket.com{path}",
                  headers={"User-Agent": "andy-trader-research/1.0 (personal research)"})
    with urlopen(req, timeout=20) as resp:
        return json.load(resp)


def main() -> None:
    con = duckdb.connect()
    for city in CITIES:
        row = con.sql(f"""
            select event_slug from '{MARKETS}'
            where event_title like 'Highest temperature in {city} on %'
            order by end_date desc limit 1
        """).fetchone()
        if not row:
            print(f"### {city}: no event found\n")
            continue
        events = gamma(f"/events?slug={row[0]}")
        event = events[0] if isinstance(events, list) and events else {}
        markets = event.get("markets") or [{}]
        print(f"### {city}: {row[0]}")
        print("resolutionSource:", event.get("resolutionSource") or markets[0].get("resolutionSource"))
        print((event.get("description") or markets[0].get("description") or "")[:1200])
        print()
        time.sleep(1)


if __name__ == "__main__":
    main()
