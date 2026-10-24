"""One paper-only cheap ask book pass.

Runs the cheap ask book rule against recent fast:continuation predictions.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import time
import uuid

from andy_trader.cheap_ask_book import format_report, run_once
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.store import connect, default_database_path

LOCK_PATH = REPO_ROOT / ".cheap-ask-book.lock"
JOURNAL_PATH = REPO_ROOT / ".cheap-ask-book.jsonl"


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
    load_env_file(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--report", action="store_true", help="Print summary report")
    args = parser.parse_args(argv)

    token = acquire_lock()
    if token is None:
        return 0

    try:
        db_path = Path(args.database) if args.database else default_database_path()
        with connect(db_path) as connection:
            run_once(connection)
            if args.report:
                print(format_report(connection))
        return 0
    except Exception as exc:
        journal("error", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if LOCK_PATH.exists() and LOCK_PATH.read_text() == token:
            LOCK_PATH.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
