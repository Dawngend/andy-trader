"""From METAR to settlement: daily highs, bucket outcomes, and the moment each bucket is decided.

Step 1 (validation): rebuild each market's daily high from the station's METAR
reports in the station's local calendar day, in the market's own unit, and
check it reproduces how the market actually resolved. If it does not, nothing
built on top of it can be trusted.

Step 2 (certainty times): the daily high can only rise during the day, so a
bucket becomes impossible (NO certain) as soon as the running high passes its
upper edge, and a "X or higher" bucket becomes certain YES as soon as the
running high reaches X. `margin` requires the running high to clear the edge by
that many whole units, to absorb rounding and unit-conversion differences
between METAR and Wunderground.

Output: research_data/weather/certainty.parquet (one row per market with its
earliest certainty time, if any) and a printed validation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
WEATHER = REPO / "research_data" / "weather"
MARKETS = REPO / "research_data" / "polymarket" / "weather_markets.parquet"
ZONES = {"EGLC": "Europe/London", "KLGA": "America/New_York", "RKSI": "Asia/Seoul",
         "ZSPD": "Asia/Shanghai", "LFPB": "Europe/Paris"}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_DATE = re.compile(r"-on-([a-z]+)-(\d{1,2})(?:-(\d{4}|\d{2}))?(?:-[a-z0-9]+)?$")


def market_date(event_slug: str, end_date: datetime) -> str:
    match = _DATE.search(event_slug)
    if not match:
        raise ValueError(f"no date in {event_slug!r}")
    # Slugs use both full ("january") and short ("jan") month names.
    month = next((n for name, n in MONTHS.items() if name.startswith(match.group(1)[:3])), None)
    if month is None:
        raise ValueError(f"unknown month in {event_slug!r}")
    day = int(match.group(2))
    raw_year = match.group(3)
    year = (2000 + int(raw_year) if raw_year and len(raw_year) == 2
            else int(raw_year) if raw_year else end_date.year)
    if not match.group(3) and month == 12 and end_date.month == 1:
        year -= 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def load_reports(station: str) -> list[tuple[int, str, float, float]]:
    """(epoch_s, local_date, temp_c, temp_f) for every report with a temperature."""

    zone = ZoneInfo(ZONES[station])
    rows = duckdb.sql(f"""
        select valid, try_cast(tmpc as double), try_cast(tmpf as double)
        from read_csv('{(WEATHER / f"metar_{station}.csv").as_posix()}', all_varchar = true)
        where tmpc <> 'M'
    """).fetchall()
    out = []
    for valid, tmpc, tmpf in rows:
        ts = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        out.append((int(ts.timestamp()), ts.astimezone(zone).strftime("%Y-%m-%d"), tmpc, tmpf))
    out.sort()
    return out


def unit_value(temp_c: float, temp_f: float, unit: str) -> int:
    # Wunderground shows whole degrees; round half away from zero like it does.
    value = temp_c if unit == "C" else temp_f
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def bucket_yes(kind: str, lo: int | None, hi: int | None, high: int) -> bool:
    if kind == "eq":
        return high == lo
    if kind == "range":
        return lo <= high <= hi
    if kind == "ge":
        return high >= lo
    return high <= hi  # le


def certainty(kind: str, lo: int | None, hi: int | None, running: int, margin: int) -> bool | None:
    """True/False once the running high decides the bucket, else None."""

    if kind == "ge" and running >= lo + margin:
        return True
    if kind in ("eq", "range", "le") and running > hi + margin:
        return False
    return None


def main(margin: int = 0) -> int:
    reports = {s: load_reports(s) for s in ZONES}
    by_day: dict[tuple[str, str], list[tuple[int, float, float]]] = {}
    for station, rows in reports.items():
        for ts, local_date, tc, tf in rows:
            by_day.setdefault((station, local_date), []).append((ts, tc, tf))

    markets = duckdb.sql(f"select * from '{MARKETS.as_posix()}'").fetchall()
    cols = [d[0] for d in duckdb.sql(f"select * from '{MARKETS.as_posix()}' limit 0").description]
    out, agree, total, missing = [], 0, 0, 0
    certain_right = certain_wrong = 0
    bad_dates: list[str] = []
    for row in markets:
        m = dict(zip(cols, row))
        try:
            date = market_date(m["event_slug"], m["end_date"])
        except ValueError:
            bad_dates.append(m["event_slug"])
            continue
        day = by_day.get((m["station"], date))
        if not day:
            missing += 1
            continue
        running, decided_at, decided_as = None, None, None
        for ts, tc, tf in day:
            value = unit_value(tc, tf, m["unit"])
            running = value if running is None else max(running, value)
            if decided_at is None:
                verdict = certainty(m["bucket_kind"], m["temp_lo"], m["temp_hi"], running, margin)
                if verdict is not None:
                    decided_at, decided_as = ts, verdict
        predicted = bucket_yes(m["bucket_kind"], m["temp_lo"], m["temp_hi"], running)
        total += 1
        agree += predicted == m["yes_won"]
        if decided_as is not None:
            if decided_as == m["yes_won"]:
                certain_right += 1
            else:
                certain_wrong += 1
        out.append({"market_id": m["market_id"], "city": m["city"], "date": date,
                    "bucket_kind": m["bucket_kind"], "yes_won": m["yes_won"],
                    "metar_high": running, "predicted_yes": predicted,
                    "decided_at": decided_at, "decided_as": decided_as})

    pq.write_table(pa.Table.from_pylist(out), WEATHER / f"certainty_margin{margin}.parquet")
    print(f"margin {margin}: {total:,} markets matched to METAR days ({missing} without data, "
          f"{len(bad_dates)} unreadable dates{': ' + ', '.join(sorted(set(bad_dates))[:5]) if bad_dates else ''})")
    print(f"  METAR daily high reproduces the resolution for {agree:,}/{total:,} "
          f"({agree / total:.2%})")
    decided = certain_right + certain_wrong
    print(f"  decided before the day ended: {decided:,}; of those, correct {certain_right:,} "
          f"({certain_right / max(decided, 1):.2%}), wrong {certain_wrong:,}")
    con = duckdb.connect()
    con.sql(f"create view c as select * from '{(WEATHER / f'certainty_margin{margin}.parquet').as_posix()}'")
    print(con.sql("""
        select city, count(*) n, round(avg((predicted_yes = yes_won)::int), 4) as reproduce_rate,
               sum((decided_as is not null)::int) as decided,
               sum((decided_as is not null and decided_as <> yes_won)::int) as decided_wrong
        from c group by 1 order by 1
    """).df().to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 0))
