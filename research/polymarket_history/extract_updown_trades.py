"""Step 2: pull the fills of resolved crypto Up/Down rounds out of the remote dataset.

Reads `quant.parquet` (21 GB, "unified YES perspective") from Hugging Face one
row group at a time, fetching only six columns and keeping only the rounds
listed by build_market_list.py, so what lands on D: stays far below the agreed
20 GB cap. Trader addresses (maker, taker) and transaction hashes are never
downloaded: nothing here studies individuals.

Row groups are processed in batches and each batch is written to its own file,
so an interrupted run resumes where it stopped. Row groups that end before the
first Up/Down round existed are skipped using the file's own statistics.

Output: research_data/polymarket/updown_trades/rg_<first>_<last>.parquet
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from huggingface_hub import HfFileSystem
from pyarrow.fs import FSSpecHandler, PyFileSystem

DATA = Path(__file__).resolve().parents[2] / "research_data" / "polymarket"
OUT = DATA / "updown_trades"
REMOTE = "datasets/SII-WANGZJ/Polymarket_data/quant.parquet"
COLUMNS = ["timestamp", "market_id", "price", "usd_amount", "token_amount", "side"]
BATCH = 10  # row groups per output file
CAP_GB = 18.0  # stop short of the agreed 20 GB cap


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    markets = (DATA / "updown_markets.parquet").as_posix()
    con = duckdb.connect()
    ids = {
        row[0]
        for row in con.sql(
            f"""select market_id from '{markets}'
                where outcome_prices in ('[''1'', ''0'']', '[''0'', ''1'']')"""
        ).fetchall()
    }
    first_ts = con.sql(f"select epoch(min(created_at))::bigint from '{markets}'").fetchone()[0]
    id_array = pa.array(sorted(ids), type=pa.string())

    # pyarrow's own filesystem wrapper gives true random access with pre-buffering,
    # which coalesces each row group's column chunks into a few large range
    # requests. Reading through a plain fsspec file handle instead issued many
    # small round trips and ran at a small fraction of the line speed.
    pafs = PyFileSystem(FSSpecHandler(HfFileSystem()))
    pf = pq.ParquetFile(REMOTE, filesystem=pafs, pre_buffer=True)
    meta = pf.metadata
    ts_col = meta.schema.to_arrow_schema().get_field_index("timestamp")
    wanted = []
    for rg in range(meta.num_row_groups):
        stats = meta.row_group(rg).column(ts_col).statistics
        if stats is None or not stats.has_min_max or stats.max >= first_ts:
            wanted.append(rg)
    print(f"{len(ids):,} resolved rounds; {len(wanted)} of {meta.num_row_groups} "
          "row groups overlap their lifetime", flush=True)

    for start in range(0, len(wanted), BATCH):
        batch = wanted[start:start + BATCH]
        target = OUT / f"rg_{batch[0]:05d}_{batch[-1]:05d}.parquet"
        if target.exists():
            continue
        t0 = time.time()
        table = pf.read_row_groups(batch, columns=COLUMNS)
        merged = table.filter(pc.is_in(table["market_id"], value_set=id_array))
        tmp = target.with_suffix(".tmp")
        pq.write_table(merged, tmp, compression="zstd")
        tmp.replace(target)
        total = sum(p.stat().st_size for p in OUT.glob("*.parquet")) / 1e9
        done = min(start + BATCH, len(wanted))
        print(f"[{done}/{len(wanted)}] row groups {batch[0]}-{batch[-1]}: "
              f"{merged.num_rows:,} fills in {time.time() - t0:.0f}s; total {total:.2f} GB",
              flush=True)
        if total > CAP_GB:
            print("stopping: approaching the 20 GB cap", flush=True)
            return 1
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
