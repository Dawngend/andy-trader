"""List the daily-high weather markets for the five Wunderground-settled cities.

Output: research_data/polymarket/weather_markets.parquet with each question's
bucket parsed into (kind, temp_lo, temp_hi, unit), so a replay can tell which
buckets an observed daily high rules out.

Bucket kinds, all inclusive, in the market's own unit:
    eq     "be 19°C"                  temp_lo = temp_hi = 19
    range  "be between 50-51°F"       temp_lo = 50, temp_hi = 51
    ge     "be 53°F or higher"        temp_lo = 53, temp_hi = None
    le     "be 20°C or below/lower"   temp_lo = None, temp_hi = 20

Question text is inconsistent across 2025-2026 (hyphen, en dash, "X°F and
Y°F", a space before the degree sign, "be tween", a missing "be", and
negative winter temperatures such as -2°C), so parsing is done here with
explicit patterns and the script refuses to write output if any row fails.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

DATA = Path(__file__).resolve().parents[2] / "research_data" / "polymarket"
CITIES = {"London": "EGLC", "NYC": "KLGA", "Seoul": "RKSI", "Shanghai": "ZSPD", "Paris": "LFPB"}

_NUM = r"(-?\d+)"
_DEG = r"\s*[°º]\s*"
_RANGE = re.compile(
    rf"{_NUM}(?:{_DEG}[CF])?\s*(?:-|–|—|\band\b|\bto\b)\s*{_NUM}{_DEG}([CF])", re.I
)
_SINGLE = re.compile(rf"(?<![\d-]){_NUM}{_DEG}([CF])", re.I)


def parse_bucket(question: str) -> tuple[str, int | None, int | None, str]:
    """Return (kind, temp_lo, temp_hi, unit) for one daily-high question."""

    text = question.lower()
    match = _RANGE.search(question)
    if match:
        lo, hi = int(match.group(1)), int(match.group(2))
        if lo > hi:
            raise ValueError(f"range reversed in {question!r}")
        return "range", lo, hi, match.group(3).upper()
    match = _SINGLE.search(question)
    if not match:
        raise ValueError(f"no temperature in {question!r}")
    value, unit = int(match.group(1)), match.group(2).upper()
    if "or higher" in text or "or above" in text:
        return "ge", value, None, unit
    if "or below" in text or "or lower" in text:
        return "le", None, value, unit
    return "eq", value, value, unit


def _selftest() -> None:
    cases = {
        "Will the highest temperature in London be 19°C on March 1?": ("eq", 19, 19, "C"),
        "Will the highest temperature in Seoul be -2°C on January 5?": ("eq", -2, -2, "C"),
        "Will the highest temperature in Paris be 19 °C on May 2?": ("eq", 19, 19, "C"),
        "Will the highest temperature in NYC be between 50-51°F on May 6?": ("range", 50, 51, "F"),
        "Will the highest temperature in NYC be between 50–51°F on May 6?": ("range", 50, 51, "F"),
        "Will the highest temperature in NYC be between 50°F and 51°F on May 6?": ("range", 50, 51, "F"),
        "Will the highest temperature in NYC be be tween 50-51°F on May 6?": ("range", 50, 51, "F"),
        "Will the highest temperature in Seoul be between -3--2°C on Jan 5?": ("range", -3, -2, "C"),
        "Will the highest temperature in London be 53°F or higher on March 17?": ("ge", 53, None, "F"),
        "Will the highest temperature in London be 20°C or below on July 22?": ("le", None, 20, "C"),
        "Will the highest temperature in London 20°F or below on July 22?": ("le", None, 20, "F"),
        "Will the highest temperature in NYC be 60°F or lower on May 6?": ("le", None, 60, "F"),
        "Will the highest temperature in Seoul be -5 °C or below on Jan 5?": ("le", None, -5, "C"),
    }
    for question, expected in cases.items():
        got = parse_bucket(question)
        assert got == expected, f"{question!r}: expected {expected}, got {got}"


def main() -> int:
    _selftest()
    con = duckdb.connect()
    city_rows = ", ".join(f"('{c}', '{s}')" for c, s in CITIES.items())
    con.sql(f"create table cities as select * from (values {city_rows}) as v(city, station)")
    rows = con.sql(f"""
        select m.id as market_id, c.city, c.station, m.question, m.slug, m.event_slug,
               m.outcome_prices, m.volume, m.end_date
        from '{(DATA / "markets.parquet").as_posix()}' m
        join cities c on m.event_title like 'Highest temperature in ' || c.city || ' on %'
        where m.outcome_prices in ('[''1'', ''0'']', '[''0'', ''1'']')
    """).fetchall()

    records, failures = [], []
    for market_id, city, station, question, slug, event_slug, prices, volume, end_date in rows:
        try:
            kind, lo, hi, unit = parse_bucket(question)
        except ValueError as exc:
            failures.append(str(exc))
            continue
        records.append({
            "market_id": market_id, "city": city, "station": station, "question": question,
            "slug": slug, "event_slug": event_slug, "yes_won": prices == "['1', '0']",
            "volume": volume, "end_date": end_date, "bucket_kind": kind,
            "temp_lo": lo, "temp_hi": hi, "unit": unit,
        })
    if failures:
        print(f"{len(failures)} question(s) failed to parse; nothing written:")
        for failure in failures[:20]:
            print("  ", failure)
        return 1

    out = DATA / "weather_markets.parquet"
    pq.write_table(pa.Table.from_pylist(records), out)
    con.sql(f"create view w as select * from '{out.as_posix()}'")
    print(con.sql("""
        select city, unit, bucket_kind, count(*) n, min(end_date)::date first_end,
               max(end_date)::date last_end, round(sum(volume) / 1e6, 2) volume_musd
        from w group by all order by city, bucket_kind
    """).df().to_string(index=False))
    events = con.sql("select count(distinct event_slug), sum(yes_won::int) from w").fetchone()
    print(f"\n{len(records):,} resolved markets in {events[0]:,} daily events; "
          f"{events[1]:,} resolved YES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
