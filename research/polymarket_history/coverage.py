"""How much of the Up/Down universe the extracted fills actually cover."""

from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parents[2] / "research_data" / "polymarket"


def main() -> None:
    con = duckdb.connect()
    trades = (DATA / "updown_trades" / "*.parquet").as_posix()
    markets = (DATA / "updown_markets.parquet").as_posix()
    con.sql(f"create view t as select * from read_parquet('{trades}')")
    con.sql(f"create view m as select * from '{markets}' "
            "where outcome_prices in ('[''1'', ''0'']', '[''0'', ''1'']')")
    print(con.sql("""
        select count(*) as fills, count(distinct market_id) as rounds_with_fills,
               to_timestamp(min(timestamp)) as first_fill, to_timestamp(max(timestamp)) as last_fill,
               round(sum(usd_amount) / 1e6, 1) as usd_millions
        from t
    """).df().to_string(index=False))
    print(con.sql("""
        select m.asset, m.tf, count(distinct m.market_id) as resolved_rounds,
               count(distinct t.market_id) as rounds_with_fills
        from m left join (select distinct market_id from t) t using (market_id)
        group by all order by 1, 2
    """).df().to_string(index=False))
    print(con.sql("select side, count(*) from t group by 1").df().to_string(index=False))
    print(con.sql("select * from t limit 3").df().to_string(index=False))


if __name__ == "__main__":
    main()
