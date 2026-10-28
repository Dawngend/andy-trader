"""Run one pass of the Polymarket live runner."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from andy_trader import pm_live_runner
from andy_trader.polymarket_sender import send_buy
from andy_trader.store import default_database_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--config", help="Path to runner config file")
    parser.add_argument("--live", action="store_true", help="Forward live orders (default is dry run)")
    parser.add_argument("--status", action="store_true", help="Print status summary and recent log rows")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    send=send_buy,
    clock=pm_live_runner.utc_now,
    read_market=None,
    read_book=None,
) -> int:
    args = parse_args(argv)
    db_path = Path(args.database) if args.database else default_database_path()
    config_path = Path(args.config) if args.config else None

    if args.status:
        pm_live_runner.print_status(db_path, clock=clock)
        return 0

    pm_live_runner.run_pass(
        database_path=db_path,
        config_path=config_path,
        live=args.live,
        send=send,
        clock=clock,
        read_market=read_market,
        read_book=read_book,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
