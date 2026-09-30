"""Inspect the largest profitable prints on already-decided weather buckets.

Shows, for the biggest opportunities, the question, the local date, when METAR
decided it, when the market ended, and when the fills happened, to tell a real
mispricing from a timing or data artifact.
"""

from pathlib import Path
import sys

import duckdb

REPO = Path(__file__).resolve().parents[2]
WEATHER = REPO / "research_data" / "weather"
TRADES = (REPO / "research_data" / "polymarket" / "weather_trades" / "*.parquet").as_posix()
MARKETS = (REPO / "research_data" / "polymarket" / "weather_markets.parquet").as_posix()


def main(city: str = "NYC", limit: int = 12) -> None:
    con = duckdb.connect()
    print(con.sql(f"""
        with d as (
            select * from '{(WEATHER / "certainty_margin1.parquet").as_posix()}'
            where decided_as is not null and decided_as = yes_won and city = '{city}'
        ),
        fills as (
            select d.market_id, d.date, d.decided_as, d.decided_at, t.timestamp, t.side, t.price,
                   t.token_amount,
                   case when not d.decided_as and t.side = 'SELL' then t.price
                        when d.decided_as and t.side = 'BUY' then 1 - t.price end as gross
            from d join read_parquet('{TRADES}') t
              on t.market_id = d.market_id and t.timestamp >= d.decided_at + 900
        )
        select m.question, f.date, to_timestamp(f.decided_at) as decided_utc,
               m.end_date as market_end, count(*) as fills,
               round(sum(f.token_amount * f.gross), 2) as gross_usd,
               round(avg(f.gross), 3) as avg_gross,
               to_timestamp(min(f.timestamp)) as first_fill, to_timestamp(max(f.timestamp)) as last_fill,
               round((max(f.timestamp) - any_value(f.decided_at)) / 3600.0, 1)
                 as hours_after_decided_last
        from fills f join '{MARKETS}' m using (market_id)
        where f.gross >= 0.02
        group by m.question, f.date, f.decided_at, m.end_date
        order by gross_usd desc limit {limit}
    """).df().to_string(index=False))


if __name__ == "__main__":
    main(*sys.argv[1:2])
