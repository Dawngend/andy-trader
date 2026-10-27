"""Run one paper-only bond book pass using public market data."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path

from andy_trader.bond_book import format_report, run_once
from andy_trader.env import REPO_ROOT
from andy_trader.store import connect, default_database_path

JOURNAL_PATH = REPO_ROOT / ".bond-book.jsonl"


def journal(event: str, **details: object) -> None:
    with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(UTC).isoformat(), "event": event, **details}) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--report", action="store_true", help="Print summary after the pass")
    args = parser.parse_args(argv)
    try:
        path = Path(args.database) if args.database else default_database_path()
        with connect(path) as connection:
            errors = run_once(connection)
            for error in errors:
                journal("error", **error)
            if args.report:
                print(format_report(connection))
        journal("pass", errors=len(errors))
        return 1 if errors else 0
    except Exception as exc:
        journal("error", error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
