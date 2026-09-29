"""Scoping: how big is Polymarket's weather-market universe in the public markets table?

Read-only inventory for the weather-repricing idea from the Phase 3 notes: count
temperature and weather markets by year and city, their traded volume, and
resolved share, so the research can be sized before any collector is built.
"""

from pathlib import Path

import duckdb

MARKETS = (Path(__file__).resolve().parents[2] / "research_data" / "polymarket"
           / "markets.parquet").as_posix()


def main() -> None:
    con = duckdb.connect()
    con.sql(f"""
        create view w as select * from '{MARKETS}'
        where regexp_matches(lower(question),
              '(highest temperature|lowest temperature|temperature in|°f|°c|degrees|rainfall|precipitation|snow)')
    """)
    print(con.sql("""
        select year(end_date) as year, count(*) as markets, count(distinct event_id) as events,
               round(sum(volume) / 1e6, 2) as volume_musd
        from w group by 1 order by 1
    """).df().to_string(index=False))
    print("\nMost common event titles (city/date series):")
    print(con.sql("""
        select regexp_replace(event_title, ' on [A-Z][a-z]+ [0-9]+.*$', '') as series,
               count(distinct event_id) as events, round(sum(volume) / 1e6, 2) as volume_musd
        from w group by 1 order by volume_musd desc limit 20
    """).df().to_string(index=False))
    print("\nSample questions:")
    for (q,) in con.sql("select question from w order by volume desc limit 8").fetchall():
        print("  ", q)


if __name__ == "__main__":
    main()
