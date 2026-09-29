"""Generic fill extraction for an explicit set of markets (e.g. weather buckets).

Same method as extract_updown_trades.py: read quant.parquet from Hugging Face one
batch of row groups at a time through pyarrow's filesystem wrapper, keep six
columns and only the listed market ids, write resumable batch files.

Usage:
    python extract_trades_for_markets.py <market_list.parquet> <output_dir_name> [min_epoch]

<market_list.parquet> needs a `market_id` column (it may live under research_data/polymarket/).
Output: research_data/polymarket/<output_dir_name>/rg_<first>_<last>.parquet
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pyarrow as pa
from huggingface_hub import HfFileSystem
from pyarrow.fs import FSSpecHandler, PyFileSystem

DATA = Path(__file__).resolve().parents[2] / "research_data" / "polymarket"
REMOTE = "datasets/SII-WANGZJ/Polymarket_data/quant.parquet"
COLUMNS = ["timestamp", "market_id", "price", "usd_amount", "token_amount", "side"]
BATCH = 10
CAP_GB = 18.0


def main(market_list: str, out_name: str, min_epoch: int = 0) -> int:
    out = DATA / out_name
    out.mkdir(parents=True, exist_ok=True)
    src = Path(market_list)
    if not src.is_absolute():
        src = DATA / src
    ids = [r[0] for r in duckdb.sql(f"select distinct market_id from '{src.as_posix()}'").fetchall()]
    id_array = pa.array(sorted(ids), type=pa.string())

    pf = pq.ParquetFile(REMOTE, filesystem=PyFileSystem(FSSpecHandler(HfFileSystem())), pre_buffer=True)
    meta = pf.metadata
    ts_col = meta.schema.to_arrow_schema().get_field_index("timestamp")
    wanted = [
        rg for rg in range(meta.num_row_groups)
        if (stats := meta.row_group(rg).column(ts_col).statistics) is None
        or not stats.has_min_max or stats.max >= min_epoch
    ]
    print(f"{len(ids):,} markets; {len(wanted)} of {meta.num_row_groups} row groups", flush=True)
    for start in range(0, len(wanted), BATCH):
        batch = wanted[start:start + BATCH]
        target = out / f"rg_{batch[0]:05d}_{batch[-1]:05d}.parquet"
        if target.exists():
            continue
        t0 = time.time()
        table = pf.read_row_groups(batch, columns=COLUMNS)
        kept = table.filter(pc.is_in(table["market_id"], value_set=id_array))
        tmp = target.with_suffix(".tmp")
        pq.write_table(kept, tmp, compression="zstd")
        tmp.replace(target)
        total = sum(p.stat().st_size for p in DATA.rglob("*.parquet")) / 1e9
        print(f"[{min(start + BATCH, len(wanted))}/{len(wanted)}] {kept.num_rows:,} fills, "
              f"{time.time() - t0:.0f}s; polymarket data total {total:.2f} GB", flush=True)
        if total > CAP_GB:
            print("stopping: approaching the 20 GB cap", flush=True)
            return 1
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0))
