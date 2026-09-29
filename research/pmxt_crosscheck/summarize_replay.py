"""Aggregate the PMXT complete-set replay into the numbers the report quotes.

Reads research_data/pmxt/samples_btc5m_*.parquet written by replay_complete_set.py
and keeps only `verified` seconds (rebuilt best ask equals the exchange's own
recorded best ask on both sides).
"""

from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parents[2] / "research_data" / "pmxt"


def main() -> None:
    con = duckdb.connect()
    src = (DATA / "samples_btc5m_*.parquet").as_posix()
    con.sql(f"create view s as select *, regexp_extract(filename, '([0-9-]+T[0-9]+)', 1) as hour "
            f"from read_parquet('{src}', filename = true)")
    print("Coverage")
    print(con.sql("""
        select count(distinct hour) as hours, count(distinct slug) as rounds,
               count(*) as seconds, sum(verified::int) as verified_seconds,
               round(avg(verified::int), 4) as verified_share
        from s
    """).df().to_string(index=False))
    print("\nVerified seconds: how often a complete set cost less than $1")
    print(con.sql("""
        select count(*) as seconds,
               sum((naive < 1)::int) as best_asks_below_1,
               sum((combined < 1)::int) as walked_10_below_1,
               sum((net_07 < 1)::int) as net_fee07_below_1,
               sum((net_10 < 1)::int) as net_fee10_below_1,
               min(naive) as min_best_ask_sum,
               min(combined) as min_walked_cost,
               min(net_07) as min_net_fee07,
               round(avg(naive), 4) as mean_best_ask_sum
        from s where verified and combined is not null
    """).df().to_string(index=False))
    print("\nBest-ask sum distribution (verified seconds)")
    print(con.sql("""
        select round(naive, 2) as best_ask_sum, count(*) as seconds
        from s where verified and naive is not null
        group by 1 order by 1 limit 12
    """).df().to_string(index=False))
    print("\nUnverified seconds (excluded): best-ask sums below 1")
    print(con.sql("""
        select count(*) as seconds, sum((naive < 1)::int) as below_1
        from s where not verified
    """).df().to_string(index=False))


if __name__ == "__main__":
    main()
