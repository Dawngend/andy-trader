"""Step 1: list the crypto Up/Down rounds CT-11 trades, from the public markets table.

Source: SII-WANGZJ/Polymarket_data on Hugging Face (MIT), `markets.parquet`,
downloaded to research_data/polymarket/. Output: updown_markets.parquet beside it.
"""

from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parents[2] / "research_data" / "polymarket"


def main() -> None:
    con = duckdb.connect()
    src = (DATA / "markets.parquet").as_posix()
    out = (DATA / "updown_markets.parquet").as_posix()
    con.sql(
        f"""
        create or replace table m as
        select id as market_id, slug, question,
               regexp_extract(slug, '^(btc|eth|sol|xrp)-updown-(5m|15m)', 1) as asset,
               regexp_extract(slug, '^(btc|eth|sol|xrp)-updown-(5m|15m)', 2) as tf,
               answer1, answer2, outcome_prices, closed, volume, created_at, end_date
        from '{src}'
        where regexp_matches(slug, '^(btc|eth|sol|xrp)-updown-(5m|15m)-')
        """
    )
    print(con.sql(
        "select asset, tf, count(*) n, min(end_date) first_end, max(end_date) last_end, "
        "sum(volume)::bigint vol from m group by all order by 1, 2"
    ))
    print(con.sql(
        "select answer1, answer2, outcome_prices, closed, count(*) n from m "
        "group by all order by n desc limit 8"
    ))
    con.sql(f"copy m to '{out}' (format parquet)")
    print("wrote", out)


if __name__ == "__main__":
    main()
