"""Did the market still offer an already-decided weather bucket at a profit?

For every market that METAR decided before the day ended (margin 1, 99.97%
correct against real resolutions), look at the fills that happened AFTER the
deciding report plus a publication/reaction delay, and ask what a buyer of the
now-certain side could have paid:

- Decided NO (the high already passed the bucket): buying NO costs 1 minus the
  YES bid. A taker SELL fill at YES price p hit the YES bid, so NO was
  available at 1 - p, and it settles to $1: profit p per share before fees.
- Decided YES ("X or higher" already reached): a taker BUY fill at p lifted the
  YES ask, so YES was available at p: profit 1 - p per share before fees.

Fee per share uses Polymarket's formula feeRate * price * (1 - price); results
are shown at feeRate 0 and 0.07 because the weather fee rate was not verified.
Each fill's size caps how many shares could have been taken at that price, so
totals are an upper bound on what one extra trader could have captured.

Output: research/weather_scoping/results/decided_bucket_trades.csv + printed summary.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
WEATHER = REPO / "research_data" / "weather"
TRADES = (REPO / "research_data" / "polymarket" / "weather_trades" / "*.parquet").as_posix()
MARKETS = (REPO / "research_data" / "polymarket" / "weather_markets.parquet").as_posix()
RESULTS = Path(__file__).resolve().parent / "results"
DELAYS_S = (300, 900, 3600)  # 5 min, 15 min, 1 h after the deciding report time
MIN_PROFIT = 0.02            # ignore sub-2-cent prints near settlement


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    con = duckdb.connect()
    # A reading in the last hour of the local day can be disputed as belonging
    # to the next day (the largest raw "opportunity", NYC 2026-03-07, was
    # decided at 23:51 local on a DST-change night and traded at 0.71 the next
    # day while resolution was unclear). Those are resolution risk, not
    # mispricing, so decisions after 23:00 local are excluded.
    zones = {"London": "Europe/London", "NYC": "America/New_York", "Seoul": "Asia/Seoul",
             "Shanghai": "Asia/Shanghai", "Paris": "Europe/Paris"}
    zone_rows = ", ".join(f"('{c}', '{z}')" for c, z in zones.items())
    con.sql(f"create table zones as select * from (values {zone_rows}) as v(city, tz)")
    con.sql(f"""
        create table d as
        select c.market_id, c.city, c.date, c.decided_at, c.decided_as, c.yes_won
        from '{(WEATHER / "certainty_margin1.parquet").as_posix()}' c
        join zones z using (city)
        -- Every decided market is kept, including the rare ones METAR called
        -- wrong: a strategy cannot know in advance which calls will fail, so a
        -- wrong call must count as a full loss, not be filtered out.
        where c.decided_as is not null
          and hour(timezone(z.tz, to_timestamp(c.decided_at))) < 23
    """)
    con.sql(f"create view t as select * from read_parquet('{TRADES}')")
    delay_rows = ", ".join(f"({d})" for d in DELAYS_S)
    con.sql(f"create table delays as select * from (values {delay_rows}) as v(delay_s)")
    con.sql(f"""
        create table opp as
        select d.city, d.market_id, d.decided_as, l.delay_s, t.timestamp, t.side, t.price,
               t.token_amount as shares,
               -- edge_if_right: profit per share if the METAR call is right. It is
               -- what a trader sees and filters on. A wrong call pays 0 instead
               -- of 1, so the realized result is exactly one dollar lower.
               case when not d.decided_as and t.side = 'SELL' then t.price
                    when d.decided_as and t.side = 'BUY' then 1 - t.price end as edge_if_right,
               case when not d.decided_as and t.side = 'SELL' then t.price
                    when d.decided_as and t.side = 'BUY' then 1 - t.price end
                 - (d.decided_as <> d.yes_won)::int as gross_per_share,
               case when not d.decided_as then 1 - t.price else t.price end as entry_price
        from d cross join delays l
        join t on t.market_id = d.market_id and t.timestamp >= d.decided_at + l.delay_s
        where (not d.decided_as and t.side = 'SELL') or (d.decided_as and t.side = 'BUY')
    """)
    summary = con.sql(f"""
        select delay_s / 60 as delay_min, city,
               count(distinct market_id) as markets_with_fills,
               count(*) as fills,
               round(sum(shares * (edge_if_right >= {MIN_PROFIT})::int)) as shares_2c_plus,
               round(sum(shares * gross_per_share
                         * (edge_if_right >= {MIN_PROFIT})::int), 2) as gross_usd,
               round(sum(shares * (gross_per_share - 0.07 * entry_price * (1 - entry_price))
                         * (edge_if_right >= {MIN_PROFIT})::int), 2) as net_usd_fee07,
               round(avg(gross_per_share) filter (where edge_if_right >= {MIN_PROFIT}), 4)
                 as avg_gross_per_share
        from opp group by all order by delay_min, city
    """).df()
    summary.to_csv(RESULTS / "decided_bucket_trades.csv", index=False)
    decided = con.sql("select count(*) from d").fetchone()[0]
    print(f"{decided:,} markets decided early by METAR (margin 1, before 23:00 local); "
          "wrong calls counted as losses\n")
    print(summary.to_string(index=False))
    print("\nTotals by delay (all cities):")
    print(con.sql(f"""
        select delay_s / 60 as delay_min, count(distinct market_id) as markets,
               round(sum(shares * gross_per_share * (edge_if_right >= {MIN_PROFIT})::int), 2)
                 as gross_usd,
               round(sum(shares * (gross_per_share - 0.07 * entry_price * (1 - entry_price))
                         * (edge_if_right >= {MIN_PROFIT})::int), 2) as net_usd_fee07
        from opp group by 1 order by 1
    """).df().to_string(index=False))
    print("\nConcentration (15-min delay): share of gross profit from the top markets")
    print(con.sql(f"""
        with per_market as (
            select market_id, sum(shares * gross_per_share) as usd
            from opp where delay_s = 900 and edge_if_right >= {MIN_PROFIT}
            group by 1
        ), ranked as (
            select usd, row_number() over (order by usd desc) as rk, sum(usd) over () as total
            from per_market
        )
        select count(*) as markets, round(max(total), 2) as total_usd,
               round(sum(usd) filter (where rk = 1) / max(total), 3) as top1_share,
               round(sum(usd) filter (where rk <= 5) / max(total), 3) as top5_share,
               round(median(usd), 2) as median_market_usd
        from ranked
    """).df().to_string(index=False))
    print("\nHow long after the deciding report the profitable prints happened (15-min delay set):")
    print(con.sql(f"""
        select case when timestamp - (select decided_at from d where d.market_id = opp.market_id) < 3600 then '<1h'
                    when timestamp - (select decided_at from d where d.market_id = opp.market_id) < 6*3600 then '1-6h'
                    else '>6h' end as after_report,
               count(*) as fills, round(sum(shares * gross_per_share), 2) as gross_usd
        from opp where delay_s = 900 and edge_if_right >= {MIN_PROFIT}
        group by 1 order by 1
    """).df().to_string(index=False))


if __name__ == "__main__":
    main()
