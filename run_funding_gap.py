"""Run one pass of the cross-exchange funding gap logger, or print a report."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Callable
import uuid

from andy_trader.env import REPO_ROOT
from andy_trader.funding_gap import print_report, run_once
from andy_trader.store import connect, default_database_path

LOCK_PATH = REPO_ROOT / ".funding-gap.lock"


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


def main(argv: list[str] | None = None, *, fetch: Callable[[str], object] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to SQLite database.")
    parser.add_argument("--report", action="store_true", help="Print gap report.")
    args = parser.parse_args(argv)

    db_path = Path(args.database) if args.database else default_database_path()

    if args.report:
        with connect(db_path) as connection:
            print_report(connection)
        return 0

    token = acquire_lock()
    if token is None:
        print("Skipped: another run holds the lock")
        return 0
    try:
        with connect(db_path) as connection:
            written, failed = run_once(connection, fetch=fetch)
        print(f"Rows written: {written}, venues failed: {failed}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if LOCK_PATH.exists() and LOCK_PATH.read_text() == token:
            LOCK_PATH.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
