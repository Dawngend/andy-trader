"""Did anyone actually trade at the sub-$1 complete-set prices during an episode?

Prints every last_trade_price event for both tokens inside one episode window,
next to the replay's per-second best asks, so a reader can see whether trades
executed at or below the asks the replay says were available.
"""

from datetime import datetime, timezone
from pathlib import Path
import sys

import duckdb

REPO = Path(__file__).resolve().parents[2]
MARKETS = (REPO / "research_data" / "polymarket" / "markets.parquet").as_posix()
SAMPLES = (REPO / "research_data" / "pmxt" / "samples_btc5m_*.parquet").as_posix()
ARCHIVE = "https://r2v2.pmxt.dev/polymarket_orderbook_{hour}.parquet"


def main(slug: str, start_s: int, length_s: int) -> None:
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs;")
    round_start = int(slug.rsplit("-", 1)[1])
    hour = datetime.fromtimestamp(round_start, timezone.utc).strftime("%Y-%m-%dT%H")
    cond, up = con.sql(f"select condition_id, token1 from '{MARKETS}' where slug = '{slug}'").fetchone()
    lo, hi = round_start + start_s - 2, round_start + start_s + length_s + 1
    print("Replay seconds (verified):")
    print(con.sql(f"""
        select second, naive, round(combined, 4) walked, round(net_07, 4) net07
        from read_parquet('{SAMPLES}')
        where slug = '{slug}' and second between {start_s - 2} and {start_s + length_s + 1}
        order by second
    """).df().to_string(index=False))
    print("\nTrades and best quotes in the window:")
    print(con.sql(f"""
        select (epoch_ms(timestamp) - {round_start * 1000}) / 1000.0 as t_s,
               case when asset_id = '{up}' then 'Up' else 'Down' end as token,
               event_type, side, price, size, best_bid, best_ask
        from '{ARCHIVE.format(hour=hour)}'
        where market = '{cond}'::BLOB and event_type = 'last_trade_price'
          and epoch_ms(timestamp) between {lo * 1000} and {hi * 1000}
        order by timestamp limit 60
    """).df().to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
