"""Run a single pass of the Strategy Manager."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from andy_trader.env import REPO_ROOT
from andy_trader.store import default_database_path
from andy_trader.strategy_manager import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_PATH,
    run_strategy_manager_pass,
    write_report_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--output", help="Path to output markdown file")
    parser.add_argument("--config", help="Path to config file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print decisions and write nothing",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.database) if args.database else default_database_path()
    output_path = Path(args.output) if args.output else DEFAULT_OUTPUT_PATH
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH

    if args.dry_run and not db_path.is_file():
        print("no database", file=sys.stderr)
        return 1

    try:
        decisions, report_content = run_strategy_manager_pass(
            database_path=db_path,
            config_path=config_path,
            output_path=output_path,
            dry_run=args.dry_run,
        )
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    if args.dry_run:
        for d in decisions:
            action_tag = f" [{d.action}]" if d.action else ""
            print(f"{d.strategy_key}: {d.new_status}{action_tag} - {d.reason}")
        return 0

    try:
        write_report_file(output_path, report_content)
    except Exception as exc:
        sys.stderr.write(f"Error writing report: {exc}\n")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
