"""Total Paper P&L Report Across All Books."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from andy_trader.env import REPO_ROOT
from andy_trader.store import connect, default_database_path
from andy_trader.total_pnl import (
    collect_all_groups,
    ensure_baseline,
    format_report,
    format_table,
    record_snapshot,
    set_baseline,
)

DEFAULT_OUTPUT_PATH = REPO_ROOT / "research" / "total_pnl.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--output", help="Path to output markdown file")
    parser.add_argument(
        "--reset-baseline",
        action="store_true",
        help="Reset zero point baseline to current total P&L",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.database) if args.database else default_database_path()
    output_path = Path(args.output) if args.output else DEFAULT_OUTPUT_PATH

    now = datetime.now(UTC)
    now_iso = now.isoformat()

    with connect(db_path) as connection:
        groups = collect_all_groups(connection)
        record_snapshot(connection, groups, recorded_at=now_iso)

        total_group = next((g for g in groups if g.name == "TOTAL"), None)
        total_pnl = total_group.pnl if total_group is not None else None

        if total_pnl is not None:
            if args.reset_baseline:
                set_baseline(connection, total_pnl, recorded_at=now_iso)
            else:
                ensure_baseline(connection, total_pnl, recorded_at=now_iso)

        report = format_report(connection, groups, recorded_at=now_iso, current_time=now)
        table = format_table(groups)

    print(table)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
