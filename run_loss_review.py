"""Write a loss review report from the paper ledger."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from andy_trader.loss_review import DEFAULT_OUTPUT_PATH, build_report, read_positions
from andy_trader.store import default_database_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--output", help="Path to output Markdown file")
    parser.add_argument("--dry-run", action="store_true", help="Print the report and write nothing")
    args = parser.parse_args(argv)
    database = Path(args.database) if args.database else default_database_path()
    output = Path(args.output) if args.output else DEFAULT_OUTPUT_PATH
    try:
        if output.resolve() == database.resolve() and not args.dry_run:
            raise ValueError("Output must not overwrite the database")
        rows, counts = read_positions(database)
        report = build_report(rows, counts)
        if args.dry_run:
            print(report, end="")
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(report, encoding="utf-8")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
