"""Weekly Strategy Board: side-by-side view of Polymarket paper books."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from andy_trader.env import REPO_ROOT
from andy_trader.store import connect, default_database_path
from andy_trader.strategy_board import format_board

DEFAULT_OUTPUT_PATH = REPO_ROOT / "research" / "strategy_board.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--output", help="Path to output markdown file")
    parser.add_argument("--date", help="Override date string (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    db_path = Path(args.database) if args.database else default_database_path()
    output_path = Path(args.output) if args.output else DEFAULT_OUTPUT_PATH
    date_str = args.date or datetime.now(UTC).date().isoformat()

    with connect(db_path) as connection:
        board = format_board(connection)

    print(board)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = f"# Weekly Strategy Board - {date_str}\n\nDate: {date_str}\n\n{board}\n"
    output_path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
