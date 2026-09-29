"""Test every sub-$1 episode for a one-sided (stale) feed.

Hypothesis from btc-updown-5m-1780711500: during the episode only Up printed
events while Down's recorded book froze, so "Up ask + Down ask" combined a live
price with a stale one. For every episode found by gap_episodes.py, count
price_change events per token inside the episode window. If the quieter token
has zero or near-zero events while the other is busy, the episode is a stale
side, not an executable price.
"""

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
MARKETS = (REPO / "research_data" / "polymarket" / "markets.parquet").as_posix()
SAMPLES = (REPO / "research_data" / "pmxt" / "samples_btc5m_*.parquet").as_posix()
ARCHIVE = "https://r2v2.pmxt.dev/polymarket_orderbook_{hour}.parquet"


def main() -> None:
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs;")
    episodes = con.sql(f"""
        with s as (select * from read_parquet('{SAMPLES}') where verified and combined is not null),
        flagged as (
            select slug, second, second - row_number() over (partition by slug order by second) grp
            from s where net_07 < 1
        )
        select slug, min(second) start_s, count(*) length_s from flagged group by slug, grp
    """).fetchall()
    by_hour: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for slug, start_s, length_s in episodes:
        round_start = int(slug.rsplit("-", 1)[1])
        by_hour[datetime.fromtimestamp(round_start, timezone.utc).strftime("%Y-%m-%dT%H")].append(
            (slug, start_s, length_s)
        )

    rows = []
    for hour, items in sorted(by_hour.items()):
        for slug, start_s, length_s in items:
            round_start = int(slug.rsplit("-", 1)[1])
            cond, up = con.sql(
                f"select condition_id, token1 from '{MARKETS}' where slug = '{slug}'"
            ).fetchone()
            lo = (round_start + start_s - 1) * 1000
            hi = (round_start + start_s + length_s) * 1000
            up_n, down_n = con.sql(f"""
                select count(*) filter (where asset_id = '{up}'),
                       count(*) filter (where asset_id <> '{up}')
                from '{ARCHIVE.format(hour=hour)}'
                where market = '{cond}'::BLOB and event_type = 'price_change'
                  and epoch_ms(timestamp) between {lo} and {hi}
            """).fetchone()
            quiet, busy = min(up_n, down_n), max(up_n, down_n)
            rows.append((slug, start_s, length_s, up_n, down_n, quiet <= 0.02 * max(busy, 1)))

    one_sided = sum(r[5] for r in rows)
    print(f"{len(rows)} episodes; {one_sided} one-sided (quieter token has <=2% of the busier "
          f"token's price changes)")
    for r in sorted(rows, key=lambda r: -r[2])[:15]:
        print(f"  {r[0]} start {r[1]}s len {r[2]}s: Up {r[3]:,} vs Down {r[4]:,} price changes"
              f"{'  ONE-SIDED' if r[5] else ''}")
    two_sided = [r for r in rows if not r[5]]
    print(f"\nTwo-sided episodes: {len(two_sided)}, total seconds {sum(r[2] for r in two_sided)}")
    for r in two_sided[:15]:
        print(f"  {r[0]} start {r[1]}s len {r[2]}s: Up {r[3]:,} vs Down {r[4]:,}")


if __name__ == "__main__":
    main()
