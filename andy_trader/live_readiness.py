"""Fail-closed evidence gate for any future Andy Trader live-money path.

This command does not place orders and cannot unlock trading. It makes the
remaining blockers executable and visible so a human cannot mistake a few
profitable quote snapshots for a production-ready system.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import sys
from typing import Mapping, Sequence

from andy_trader.complete_set import CompleteSetError, _http_json
from andy_trader.store import connect, default_database_path


GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
MIN_OBSERVATION_DAYS = 14
MIN_CONFIRMED_SETTLED_TRADES = 100


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def fetch_geoblock(*, timeout_seconds: float = 8.0) -> Mapping[str, object]:
    payload = _http_json(GEOBLOCK_URL, timeout_seconds)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("blocked"), bool):
        raise CompleteSetError("Polymarket geoblock endpoint returned an invalid response")
    return payload


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(connection, table):
        return False
    return column in {
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")
    }


def evaluate_live_readiness(
    connection: sqlite3.Connection,
    *,
    geoblock: Mapping[str, object] | None,
    live_execution_adapter_verified: bool = False,
    atomic_two_leg_failure_handling_verified: bool = False,
    minimum_observation_days: int = MIN_OBSERVATION_DAYS,
    minimum_confirmed_settled_trades: int = MIN_CONFIRMED_SETTLED_TRADES,
) -> ReadinessReport:
    """Evaluate evidence without mutating the database or weakening a gate."""

    if minimum_observation_days <= 0 or minimum_confirmed_settled_trades <= 0:
        raise ValueError("readiness sample floors must be positive")

    observation_days = 0
    rounds = 0
    if _table_exists(connection, "complete_set_observations"):
        row = connection.execute(
            "SELECT COUNT(DISTINCT substr(observed_at, 1, 10)) AS days, "
            "COUNT(DISTINCT round_id) AS rounds FROM complete_set_observations"
        ).fetchone()
        observation_days = int(row["days"])
        rounds = int(row["rounds"])

    confirmed_settled = 0
    confirmed_losses = 0
    has_confirmation_column = _column_exists(
        connection,
        "complete_set_paper_trades",
        "confirmation_net_combined_cost",
    )
    if has_confirmation_column:
        row = connection.execute(
            "SELECT COUNT(*) AS settled, "
            "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses "
            "FROM complete_set_paper_trades "
            "WHERE settled_at IS NOT NULL AND confirmation_net_combined_cost IS NOT NULL"
        ).fetchone()
        confirmed_settled = int(row["settled"])
        confirmed_losses = int(row["losses"] or 0)

    if geoblock is None:
        geo_passed = False
        geo_evidence = "not checked from the intended execution host"
    else:
        blocked = geoblock.get("blocked")
        country = str(geoblock.get("country") or "unknown")
        region = str(geoblock.get("region") or "unknown")
        geo_passed = blocked is False
        geo_evidence = f"country={country}, region={region}, blocked={blocked!r}"

    enough_observations = observation_days >= minimum_observation_days
    enough_confirmed = confirmed_settled >= minimum_confirmed_settled_trades
    no_confirmed_losses = confirmed_settled > 0 and confirmed_losses == 0

    return ReadinessReport(
        checks=(
            ReadinessCheck("execution-host eligibility", geo_passed, geo_evidence),
            ReadinessCheck(
                "market coverage",
                enough_observations,
                f"{observation_days} distinct UTC days and {rounds} rounds; "
                f"requires at least {minimum_observation_days} days",
            ),
            ReadinessCheck(
                "confirmed shadow sample",
                enough_confirmed,
                f"{confirmed_settled} settled re-quoted paper trades; "
                f"requires at least {minimum_confirmed_settled_trades}",
            ),
            ReadinessCheck(
                "confirmed shadow losses",
                no_confirmed_losses,
                f"{confirmed_losses} non-profitable trades in {confirmed_settled} confirmed settlements",
            ),
            ReadinessCheck(
                "authenticated execution adapter",
                live_execution_adapter_verified,
                "absent: this repository has no authenticated order-placement path",
            ),
            ReadinessCheck(
                "two-leg failure handling",
                atomic_two_leg_failure_handling_verified,
                "unverified: Polymarket batch responses are independent and one FOK leg can fill while the other fails",
            ),
        )
    )


def _print_report(report: ReadinessReport) -> None:
    print("Andy Trader live-money readiness: " + ("READY" if report.ready else "NOT READY"))
    for check in report.checks:
        label = "PASS" if check.passed else "BLOCK"
        print(f"[{label}] {check.name}: {check.evidence}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the geoblock request; eligibility remains a blocking unknown",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    geoblock: Mapping[str, object] | None = None
    if not args.offline:
        try:
            geoblock = fetch_geoblock(timeout_seconds=args.timeout)
        except CompleteSetError as exc:
            print(f"geoblock check failed closed: {exc}", file=sys.stderr)

    db_path = Path(args.database) if args.database else default_database_path()
    with connect(db_path) as connection:
        report = evaluate_live_readiness(connection, geoblock=geoblock)
    _print_report(report)
    return 0 if report.ready else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
