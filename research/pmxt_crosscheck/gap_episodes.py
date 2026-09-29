"""When sub-$1 complete sets appear in verified PMXT seconds, and how long they last.

An "episode" is a run of consecutive verified seconds in one round with net
cost (feeRate 0.07) below $1. Duration matters more than frequency: Andy's
live re-quotes found every edge gone within about a second, and a one-second
episode in once-per-second sampling cannot be told apart from an instant.
"""

from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parents[2] / "research_data" / "pmxt"


def main() -> None:
    con = duckdb.connect()
    src = (DATA / "samples_btc5m_*.parquet").as_posix()
    con.sql(f"create view s as select * from read_parquet('{src}') where verified and combined is not null")
    print("Seconds into the 300-second round where net cost < $1 (fee 0.07)")
    print(con.sql("""
        select (second // 30) * 30 as from_second, count(*) as seconds,
               round(avg(net_07), 4) as mean_net_cost
        from s where net_07 < 1 group by 1 order by 1
    """).df().to_string(index=False))
    con.sql("""
        create table e as
        with flagged as (
            select slug, second, net_07,
                   second - row_number() over (partition by slug order by second) as grp
            from s where net_07 < 1
        )
        select slug, grp, min(second) as start_s, count(*) as length_s, min(net_07) as best_net
        from flagged group by slug, grp
    """)
    print("\nEpisodes (consecutive seconds with net cost < $1)")
    print(con.sql("""
        select count(*) as episodes, count(distinct slug) as rounds_with_any,
               sum((length_s = 1)::int) as one_second,
               sum((length_s between 2 and 5)::int) as two_to_five,
               sum((length_s > 5)::int) as over_five,
               max(length_s) as longest_s
        from e
    """).df().to_string(index=False))
    print("\nLongest episodes")
    print(con.sql("""
        select slug, start_s, length_s, round(best_net, 4) as best_net_cost
        from e order by length_s desc limit 10
    """).df().to_string(index=False))
    total_rounds = con.sql("select count(distinct slug) from s").fetchone()[0]
    print(f"\nRounds measured: {total_rounds}")


if __name__ == "__main__":
    main()
