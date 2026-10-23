"""One paper-only trend40 pass. Run every hour; the rebalance itself happens
once per UTC day, see `andy_trader.trend_book`."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import time
import uuid

from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.store import connect, default_database_path
from andy_trader.trend_book import run_once

LOCK_PATH = REPO_ROOT / ".trend-book.lock"
JOURNAL_PATH = REPO_ROOT / ".trend-book.jsonl"


def journal(event, **details):
    with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(UTC).isoformat(), "event": event, **details}) + "\n")


def acquire_lock():
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database")
    args = parser.parse_args(argv)
    token = acquire_lock()
    if token is None:
        return 0
    try:
        load_env_file(REPO_ROOT / ".env")
        with connect(Path(args.database) if args.database else default_database_path()) as connection:
            run_once(connection)
        return 0
    except Exception as exc:
        journal("error", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if LOCK_PATH.exists() and LOCK_PATH.read_text() == token:
            LOCK_PATH.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
