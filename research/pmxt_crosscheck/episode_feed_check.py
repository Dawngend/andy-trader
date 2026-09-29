"""Were the long sub-$1 episodes real, or did the recorded feed go quiet?

For each of the longest episodes, count PMXT events (any type, both tokens) per
second inside the episode window and in the round as a whole. A live BTC 5m
book emits hundreds of events per second; an episode during which the feed
records almost nothing is a stale book, not an executable price.
"""

from datetime import datetime, timezone
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
MARKETS = (REPO / "research_data" / "polymarket" / "markets.parquet").as_posix()
ARCHIVE = "https://r2v2.pmxt.dev/polymarket_orderbook_{hour}.parquet"
EPISODES = [  # slug, start second, length (from gap_episodes.py)
    ("btc-updown-5m-1780714500", 11, 289),
    ("btc-updown-5m-1782829200", 179, 17),
    ("btc-updown-5m-1783131600", 115, 16),
    ("btc-updown-5m-1780711500", 35, 14),
    ("btc-updown-5m-1782828900", 199, 12),
]


def main() -> None:
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs;")
    for slug, start_s, length_s in EPISODES:
        round_start = int(slug.rsplit("-", 1)[1])
        hour = datetime.fromtimestamp(round_start, timezone.utc).strftime("%Y-%m-%dT%H")
        cond = con.sql(f"select condition_id from '{MARKETS}' where slug = '{slug}'").fetchone()[0]
        lo = (round_start + start_s - 1) * 1000
        hi = (round_start + start_s + length_s) * 1000
        row = con.sql(f"""
            select count(*) filter (where epoch_ms(timestamp) between {lo} and {hi}) as in_episode,
                   count(*) filter (where epoch_ms(timestamp) between {round_start * 1000}
                                    and {(round_start + 300) * 1000}) as in_round,
                   count(*) filter (where event_type = 'last_trade_price'
                                    and epoch_ms(timestamp) between {lo} and {hi}) as trades_in_episode
            from '{ARCHIVE.format(hour=hour)}' where market = '{cond}'::BLOB
        """).fetchone()
        per_s_ep = row[0] / max(length_s + 1, 1)
        per_s_round = row[1] / 300
        print(f"{slug} start {start_s}s len {length_s}s: {row[0]:,} events in episode "
              f"({per_s_ep:.1f}/s) vs {per_s_round:.1f}/s for the round; "
              f"{row[2]} trades during episode", flush=True)


if __name__ == "__main__":
    main()
