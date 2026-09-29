"""Are crypto Up/Down prices calibrated, and does any miscalibration survive the taker fee?

For every resolved round with fills, take the last traded Up price at fixed
times before the round closes, and compare it with whether Up won. A price is
a probability claim: if rounds priced at 0.70 with 60 s left win 70% of the
time, the market is calibrated there and buying at 0.70 has zero expected
value before fees and a loss after them.

Expected value of buying one Up share at price p, per share, after the
Polymarket taker fee (feeRate 0.07, fee = 0.07 * p * (1 - p)):
    EV_up = win_rate - p - 0.07 * p * (1 - p)
and symmetrically for buying Down at 1 - p.

Overfitting guard: bins are scored on the TRAIN period (rounds ending before
2026-03-01) and every bin that looks profitable there is re-scored on the TEST
period (2026-03-01 onward), which the selection never saw. Only a bin that is
profitable in both, with a z-score above 2 in the test period, is reported as a
candidate. Everything else is reported as noise, because it is.

Output: research/polymarket_history/results/calibration_*.csv and a printed report.
"""

from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "research_data" / "polymarket"
RESULTS = Path(__file__).resolve().parent / "results"
FEE_RATE = 0.07
SPLIT_EPOCH = 1772323200  # 2026-03-01T00:00:00Z
CHECKPOINTS = {"5m": (240, 120, 60, 30, 10), "15m": (600, 300, 120, 60, 30)}
WINDOW_S = 30  # the price is the last fill in the WINDOW_S seconds before the checkpoint


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    con = duckdb.connect()
    con.sql("SET memory_limit = '6GB'; SET threads = 6;")
    trades = (DATA / "updown_trades" / "*.parquet").as_posix()
    markets = (DATA / "updown_markets.parquet").as_posix()
    con.sql(f"""
        create table rounds as
        select market_id, asset, tf,
               cast(regexp_extract(slug, '-([0-9]+)$', 1) as bigint) as start_s,
               cast(regexp_extract(slug, '-([0-9]+)$', 1) as bigint)
                 + case tf when '5m' then 300 else 900 end as end_s,
               (outcome_prices = '[''1'', ''0'']')::int as up_won
        from '{markets}'
        where outcome_prices in ('[''1'', ''0'']', '[''0'', ''1'']')
    """)
    checkpoint_rows = ", ".join(
        f"('{tf}', {r})" for tf, remaining in CHECKPOINTS.items() for r in remaining
    )
    con.sql(f"create table cps as select * from (values {checkpoint_rows}) as v(tf, remaining)")
    con.sql(f"""
        create table priced as
        select r.market_id, r.asset, r.tf, c.remaining, r.up_won,
               (r.end_s < {SPLIT_EPOCH}) as train,
               arg_max(t.price, t.timestamp) as p
        from rounds r
        join cps c using (tf)
        join read_parquet('{trades}') t
          on t.market_id = r.market_id
         and t.timestamp >  r.end_s - c.remaining - {WINDOW_S}
         and t.timestamp <= r.end_s - c.remaining
        group by all
    """)
    n = con.sql("select count(*), count(distinct market_id) from priced").fetchone()
    print(f"Priced observations: {n[0]:,} across {n[1]:,} rounds\n")

    con.sql(f"""
        create table bins as
        select tf, remaining, train, floor(p * 20) / 20 as bin_lo,
               count(*) as n, avg(p) as mean_p, avg(up_won) as win_rate,
               avg(up_won) - avg(p) - {FEE_RATE} * avg(p * (1 - p)) as ev_up,
               (1 - avg(up_won)) - (1 - avg(p)) - {FEE_RATE} * avg(p * (1 - p)) as ev_down,
               sqrt(avg(up_won) * (1 - avg(up_won)) / count(*)) as se
        from priced where p > 0 and p < 1
        group by all
    """)
    con.sql(f"copy bins to '{(RESULTS / 'calibration_bins.csv').as_posix()}' (header)")

    print("Calibration by time remaining (all assets, all periods): Brier score vs market price")
    print(con.sql("""
        select tf, remaining, count(*) n,
               round(avg((p - up_won) ^ 2), 4) as brier_market,
               round(avg((0.5 - up_won) ^ 2), 4) as brier_coinflip,
               round(1 - avg((p - up_won) ^ 2) / avg((0.5 - up_won) ^ 2), 4) as skill
        from priced group by all order by tf, remaining desc
    """).df().to_string(index=False))

    print("\nBins profitable after fees in TRAIN (n >= 500), re-scored on TEST:")
    result = con.sql("""
        with tr as (select * from bins where train and n >= 500),
             te as (select * from bins where not train)
        select tr.tf, tr.remaining, tr.bin_lo,
               case when tr.ev_up > tr.ev_down then 'Up' else 'Down' end as side,
               tr.n as n_train,
               round(greatest(tr.ev_up, tr.ev_down), 4) as ev_train,
               te.n as n_test,
               round(case when tr.ev_up > tr.ev_down then te.ev_up else te.ev_down end, 4) as ev_test,
               round(case when tr.ev_up > tr.ev_down then te.ev_up else te.ev_down end / te.se, 2)
                 as z_test
        from tr join te using (tf, remaining, bin_lo)
        where greatest(tr.ev_up, tr.ev_down) > 0
        order by z_test desc
    """)
    df = result.df()
    df.to_csv(RESULTS / "calibration_candidates.csv", index=False)
    print(df.head(25).to_string(index=False) if len(df) else "  none")
    survivors = df[(df["ev_test"] > 0) & (df["z_test"] > 2)] if len(df) else df
    print(f"\n{len(df)} bins looked profitable in TRAIN; {len(survivors)} held up in TEST "
          "(EV > 0 and z > 2).")
    executable_check(con, trades)


def executable_check(con: duckdb.DuckDBPyConnection, trades: str) -> None:
    """Re-score the favorite bins at prices a buyer actually paid.

    The last traded price can be a sale at the bid, one tick below what a buyer
    pays. In the unified YES perspective, a BUY fill is someone paying the Up
    ask, and a SELL fill at p is someone selling Up at the bid, which is the
    same trade as buying Down at 1 - p. So the entry cost for Up is the last
    BUY fill price, and for Down it is 1 minus the last SELL fill price.
    """

    print("\nBase rate of Up wins by period:")
    print(con.sql("""
        select case when train then 'train' else 'test' end as period,
               count(distinct market_id) as rounds, round(avg(up_won), 4) as up_win_rate
        from priced group by 1
    """).df().to_string(index=False))
    con.sql(f"""
        create table exec_priced as
        select r.market_id, r.tf, c.remaining, r.up_won, (r.end_s < {SPLIT_EPOCH}) as train,
               arg_max(t.price, t.timestamp) filter (where t.side = 'BUY') as up_cost,
               1 - arg_max(t.price, t.timestamp) filter (where t.side = 'SELL') as down_cost
        from rounds r
        join cps c using (tf)
        join read_parquet('{trades}') t
          on t.market_id = r.market_id
         and t.timestamp >  r.end_s - c.remaining - {WINDOW_S}
         and t.timestamp <= r.end_s - c.remaining
        group by all
    """)
    print("\nFavorites at executable prices (entry cost 0.90 to 0.99), EV per share after fee:")
    print(con.sql(f"""
        with legs as (
            select tf, remaining, train, 'Up' as side, up_cost as cost, up_won as won
            from exec_priced where up_cost between 0.90 and 0.99
            union all
            select tf, remaining, train, 'Down', down_cost, 1 - up_won
            from exec_priced where down_cost between 0.90 and 0.99
        )
        select tf, remaining, side, case when train then 'train' else 'test' end as period,
               count(*) as n, round(avg(cost), 4) as mean_cost, round(avg(won), 4) as win_rate,
               round(avg(won) - avg(cost) - {FEE_RATE} * avg(cost * (1 - cost)), 4) as ev,
               round((avg(won) - avg(cost) - {FEE_RATE} * avg(cost * (1 - cost)))
                     / sqrt(avg(won) * (1 - avg(won)) / count(*)), 2) as z
        from legs group by all
        order by tf, remaining desc, side, period desc
    """).df().to_string(index=False))


if __name__ == "__main__":
    main()
