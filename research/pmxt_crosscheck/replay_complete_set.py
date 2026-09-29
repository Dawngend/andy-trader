"""Replay historical PMXT order books through Andy Trader's own complete-set measurement.

Andy Trader's CT-11 collector measures, live, whether buying equal Up and Down
shares of a crypto Up/Down round costs less than the $1 it settles to, after
walking both ask books and paying Polymarket's taker fee on each leg. Its live
record starts 2026-09-06. The public PMXT archive (r2v2.pmxt.dev) holds full
Polymarket L2 books for roughly 2026-04-26 to 2026-07-20, so the two never
overlap. This script asks the same question of that earlier period, using the
unchanged `andy_trader.complete_set.observe_complete_set`, so any difference
from the live record is about the market, not about a second implementation.

Replay rules:
- A token's ask book is reset by every `book` snapshot and updated by every
  `price_change` with side SELL (an ask level; size 0 removes it). BUY changes
  touch bids and are ignored here: buying a complete set only crosses asks.
- A round is measured only after both tokens have received a snapshot.
- The state is sampled once per second of the round, carrying the last book
  forward, so a gap that lasts ten seconds counts ten times and a gap that
  lasts 50 ms counts at most once. Episode durations are reported separately.

Fee rates: Andy uses feeRate 0.07 (Polymarket docs, checked 2026-09-18). PMXT
trade events carry fee_rate_bps = 1000 in this period, which may be the
maximum rate signed on orders rather than the rate charged, so every result is
reported at both 0.07 and 0.10.

Usage: python research/pmxt_crosscheck/replay_complete_set.py 2026-05-05T12 2026-06-01T12 ...
Output: research_data/pmxt/samples_<hour>.parquet and a printed summary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from andy_trader.complete_set import observe_complete_set  # noqa: E402

DATA = REPO / "research_data"
MARKETS = (DATA / "polymarket" / "markets.parquet").as_posix()
OUT = DATA / "pmxt"
ARCHIVE = "https://r2v2.pmxt.dev/polymarket_orderbook_{hour}.parquet"
TARGET = 10.0
FEE_RATES = (0.07, 0.10)


def _asks_payload(book: dict[Decimal, float]) -> dict[str, list[dict[str, str]]]:
    return {"asks": [{"price": str(p), "size": repr(s)} for p, s in book.items() if s > 0]}


def _level(price: str) -> Decimal:
    # Snapshots send "0.99" and price changes send "0.9900"; as dict keys those
    # would be two different levels, so every price is keyed by its value.
    return Decimal(price).normalize()


def replay_hour(con: duckdb.DuckDBPyConnection, hour: str, prefix: str) -> list[dict]:
    start = int(datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).timestamp())
    rounds = con.sql(
        f"""
        select slug, condition_id, token1 as up_token, token2 as down_token, answer1,
               try_cast(regexp_extract(slug, '-([0-9]+)$', 1) as bigint) as round_start
        from '{MARKETS}'
        where slug like '{prefix}-%'
          and try_cast(regexp_extract(slug, '-([0-9]+)$', 1) as bigint)
              between {start} and {start + 3600 - 1}
        """
    ).fetchall()
    if not rounds:
        return []
    # PMXT stores the condition id as its "0x..." text in a BLOB column.
    conds = ", ".join(f"'{r[1]}'::BLOB" for r in rounds)
    events = con.sql(
        f"""
        select epoch_ms(timestamp) as ts, market::VARCHAR as cond, event_type,
               asset_id, asks, price::VARCHAR as price, size::DOUBLE as size, side,
               best_ask::VARCHAR as best_ask
        from '{ARCHIVE.format(hour=hour)}'
        where market in ({conds})
          and event_type in ('book', 'price_change')
        order by ts
        """
    ).fetchall()

    by_cond: dict[str, list[tuple]] = {}
    for row in events:
        by_cond.setdefault(row[1], []).append(row)

    samples: list[dict] = []
    for slug, cond, up_token, down_token, answer1, round_start in rounds:
        if answer1 != "Up":
            continue
        books: dict[str, dict[Decimal, float]] = {}
        exchange_best: dict[str, Decimal] = {}
        seen_snapshot: set[str] = set()
        stream = by_cond.get(cond, [])
        idx = 0
        round_end = (round_start + 300) * 1000
        for second in range(round_start * 1000, round_end, 1000):
            while idx < len(stream) and stream[idx][0] <= second:
                _, _, event_type, asset_id, asks, price, size, side, best_ask = stream[idx]
                idx += 1
                if event_type == "book":
                    books[asset_id] = {_level(p): float(s) for p, s in json.loads(asks or "[]")}
                    seen_snapshot.add(asset_id)
                    continue
                if asset_id not in books:
                    continue
                if side == "SELL":
                    if size and size > 0:
                        books[asset_id][_level(price)] = size
                    else:
                        books[asset_id].pop(_level(price), None)
                if best_ask is not None:
                    # Polymarket's Up and Down books mirror each other, and some
                    # ask removals only arrive as events on the other token. The
                    # exchange's own best ask is recorded on every change, so no
                    # ask can sit below it: anything lower is stale and dropped.
                    floor = _level(best_ask)
                    exchange_best[asset_id] = floor
                    for level in [p for p in books[asset_id] if p < floor]:
                        del books[asset_id][level]
            if up_token not in seen_snapshot or down_token not in seen_snapshot:
                continue
            record = {
                "slug": slug,
                "second": second // 1000 - round_start,
                # True only when the rebuilt best ask on both sides equals the
                # exchange's last recorded best ask; the analysis keeps only these.
                "verified": all(
                    books[token] and exchange_best.get(token) == min(books[token])
                    for token in (up_token, down_token)
                ),
            }
            for fee in FEE_RATES:
                obs = observe_complete_set(
                    slug, str(second), _asks_payload(books[up_token]),
                    _asks_payload(books[down_token]), target_notional=TARGET, fee_rate=fee,
                )
                tag = f"{int(fee * 100):02d}"
                record["naive"] = obs.naive_combined_cost
                record["combined"] = obs.combined_cost
                record[f"net_{tag}"] = obs.net_combined_cost
            samples.append(record)
    return samples


def main(hours: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs;")
    for hour in hours:
        target = OUT / f"samples_btc5m_{hour}.parquet"
        if target.exists():
            print(f"{hour}: already done", flush=True)
            continue
        t0 = time.time()
        try:
            samples = replay_hour(con, hour, "btc-updown-5m")
        except duckdb.HTTPException:
            print(f"{hour}: no archive file", flush=True)
            continue
        if not samples:
            print(f"{hour}: no measurable rounds", flush=True)
            continue
        pq.write_table(pa.Table.from_pylist(samples), target)
        n_rounds = len({s["slug"] for s in samples})
        print(f"{hour}: {n_rounds} rounds, {len(samples):,} second-samples, "
              f"{time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
