"""Run one public-data wallet scout pass."""

import argparse
from pathlib import Path

from andy_trader.store import default_database_path
from andy_trader.wallet_scout import PERIODS, run_scout


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=positive_int, default=50)
    parser.add_argument("--period", choices=PERIODS, default="MONTH")
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument("--report", type=Path, default=Path("research/wallet_scout_report.md"))
    parser.add_argument("--max-pages", type=positive_int, default=40)
    args = parser.parse_args()
    run_id = run_scout(args.database, args.report, args.top, args.period, args.max_pages)
    print(f"Run {run_id}: {args.report}")


if __name__ == "__main__":
    main()
