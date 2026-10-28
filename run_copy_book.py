"""Run one paper-only copy book pass using public market data."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import time
import uuid

from andy_trader import copy_book
from andy_trader.copy_book import format_report, run_once
from andy_trader.env import REPO_ROOT
from andy_trader.store import connect, default_database_path

LOCK_PATH = REPO_ROOT / ".copy-book.lock"
JOURNAL_PATH = REPO_ROOT / ".copy-book.jsonl"


def journal(event: str, **details: object) -> None:
    with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(UTC).isoformat(), "event": event, **details}) + "\n")


def acquire_lock() -> str | None:
    token = uuid.uuid4().hex
    try:
        if time.time() - LOCK_PATH.stat().st_mtime < 180:
            return None
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    try:
        with LOCK_PATH.open("x") as handle:
            handle.write(token)
    except FileExistsError:
        return None
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--report", action="store_true", help="Print summary after the pass")
    args = parser.parse_args(argv)

    token = acquire_lock()
    if token is None:
        if args.report:
            print("Book is busy, try again.")
        return 0

    try:
        started = time.monotonic()
        path = Path(args.database) if args.database else default_database_path()
        with connect(path) as connection:
            errors = run_once(connection)
            for error in errors:
                journal("error", **error)
            if args.report:
                print(format_report(connection))
        journal(
            "pass",
            errors=len(errors),
            seconds=round(time.monotonic() - started, 3),
            book_reads=copy_book.BOOK_READS,
            requests=copy_book.REQUEST_COUNT,
        )
        return 1 if errors else 0
    except Exception as exc:
        journal("error", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if LOCK_PATH.exists() and LOCK_PATH.read_text() == token:
            LOCK_PATH.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
