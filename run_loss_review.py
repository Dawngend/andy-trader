"""Write a loss review report from the paper ledger."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

from andy_trader.loss_review import DEFAULT_OUTPUT_PATH, build_report, read_positions
from andy_trader.loss_rules import DEFAULT_RULES_PATH, learn_rules
from andy_trader.store import default_database_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to database file")
    parser.add_argument("--output", help="Path to output Markdown file")
    parser.add_argument("--rules-output", help="Path to output loss rules JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Print the report and write nothing")
    args = parser.parse_args(argv)
    database = Path(args.database) if args.database else default_database_path()
    output = Path(args.output) if args.output else DEFAULT_OUTPUT_PATH
    rules_output = Path(args.rules_output) if args.rules_output else DEFAULT_RULES_PATH
    try:
        if output.resolve() == database.resolve() and not args.dry_run:
            raise ValueError("Output must not overwrite the database")
        if rules_output.resolve() == database.resolve() and not args.dry_run:
            raise ValueError("Rules output must not overwrite the database")
        rows, counts = read_positions(database)
        rules = learn_rules(rows)
        report = build_report(rows, counts, rules=rules)
        if args.dry_run:
            print(report, end="")
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(report, encoding="utf-8")
            rules_output.parent.mkdir(parents=True, exist_ok=True)
            rules_payload = {
                "generated_at": datetime.now(UTC).isoformat(),
                "rules": rules,
            }
            rules_output.write_text(json.dumps(rules_payload, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
