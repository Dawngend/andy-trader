"""Run one pass of the Polymarket AI approval gate and optionally write a report."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from andy_trader import pm_ai_gate
from andy_trader.store import default_database_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--report", action="store_true", help="Write report after pass")
    parser.add_argument("--report-only", action="store_true", help="Write report without pass")
    parser.add_argument("--output", help="Path to report markdown output")
    parser.add_argument("--max", type=int, default=10, help="Maximum candidates to score (1 to 10)")
    args = parser.parse_args(argv)
    if not (1 <= args.max <= 10):
        parser.error("--max must be a whole number from 1 to 10")
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    ask_model=pm_ai_gate.ask_model,
    clock=pm_ai_gate.utc_now,
) -> int:
    args = parse_args(argv)
    db_path = Path(args.database) if args.database else default_database_path()
    output_path = Path(args.output) if args.output else pm_ai_gate.DEFAULT_REPORT_PATH

    if args.report_only:
        report_text = pm_ai_gate.generate_report(db_path, clock=clock, readonly=True)
        if "no decisions yet" in report_text.lower():
            print("no decisions yet")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_text, encoding="utf-8")
        return 0

    pm_ai_gate.run_pass(
        db_path,
        max_candidates=args.max,
        ask_model=ask_model,
        clock=clock,
    )

    if args.report:
        report_text = pm_ai_gate.generate_report(db_path, clock=clock)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
