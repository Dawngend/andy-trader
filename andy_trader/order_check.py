"""Rule-based pre-order check for new paper positions."""

from __future__ import annotations

import argparse
import math
import sqlite3
from datetime import datetime, timedelta


def initialize_order_checks(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS order_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            side TEXT NOT NULL,
            risk_score REAL NOT NULL,
            flagged INTEGER NOT NULL,
            reasons TEXT NOT NULL,
            size_fraction_in REAL NOT NULL,
            size_fraction_out REAL NOT NULL
        )
        """
    )


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def risk_score(
    volatility: float, size_fraction: float, data_age_minutes: float, recent_stopouts: int
) -> float:
    """Mean of four 0..1 parts.

    Volatility is current 24-hour volatility / the last seven days' volatility;
    twice the seven-day level scores 1. Size scores its fraction directly.
    Data age reaches 1 at 60 minutes. Three recent bracket stops score 1.
    """
    parts = (
        _clamp(volatility / 2),
        _clamp(size_fraction),
        _clamp(data_age_minutes / 60),
        _clamp(recent_stopouts / 3),
    )
    return sum(parts) / 4


def check_order(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    side: str,
    now_iso: str,
    volatility: float,
    size_fraction: float,
    data_age_minutes: float,
    recent_stopouts: int,
) -> float:
    """Record an entry check and return its permitted size fraction."""
    initialize_order_checks(connection)
    score = risk_score(volatility, size_fraction, data_age_minutes, recent_stopouts)
    previous = [row[0] for row in connection.execute(
        "SELECT risk_score FROM order_checks ORDER BY id DESC LIMIT 200"
    )]
    # Only scores strictly above the historical 90th percentile are flagged.
    # Ties at the boundary pass, keeping the budget from growing on equal scores.
    cutoff = sorted(previous)[math.ceil(len(previous) * 0.9) - 1] if len(previous) >= 30 else None
    flagged = cutoff is not None and score > cutoff
    reasons = []
    if flagged:
        if volatility >= 1:
            reasons.append("elevated volatility")
        if size_fraction >= 0.5:
            reasons.append("large position")
        if data_age_minutes >= 30:
            reasons.append("aging data")
        if recent_stopouts:
            reasons.append("recent bracket stops")
        if not reasons:
            reasons.append("top-decile combined risk")
    output = size_fraction * 0.5 if flagged else size_fraction
    connection.execute(
        """INSERT INTO order_checks
           (checked_at, predictor, instrument, side, risk_score, flagged, reasons,
            size_fraction_in, size_fraction_out)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now_iso, predictor, instrument, side, score, int(flagged), "; ".join(reasons),
         size_fraction, output),
    )
    connection.commit()
    return output


def recent_stopouts(connection: sqlite3.Connection, predictor: str, instrument: str, now_iso: str) -> int:
    since = (datetime.fromisoformat(now_iso) - timedelta(hours=24)).isoformat()
    return connection.execute(
        """SELECT COUNT(*) FROM paper_trades
           WHERE predictor = ? AND instrument = ? AND side = 'flat'
             AND reason = 'bracket stop' AND executed_at > ? AND executed_at <= ?""",
        (predictor, instrument, since, now_iso),
    ).fetchone()[0]


def catch_report(connection: sqlite3.Connection) -> dict[str, int | float]:
    """Count bracket-stop exits linked to checks on their opening trades."""
    from andy_trader.portfolio import initialize_portfolio

    initialize_portfolio(connection)
    entries: dict[tuple[str, str], tuple[str, str]] = {}
    stops = flagged_stops = 0
    for predictor, instrument, side, executed_at, reason in connection.execute(
        "SELECT predictor, instrument, side, executed_at, reason FROM paper_trades ORDER BY id"
    ):
        pair = (predictor, instrument)
        if side != "flat":
            entries[pair] = (side, executed_at)
        else:
            entry = entries.pop(pair, None)
            if reason == "bracket stop" and entry is not None:
                stops += 1
                check = connection.execute(
                    """SELECT flagged FROM order_checks
                       WHERE predictor = ? AND instrument = ? AND side = ? AND checked_at = ?
                       ORDER BY id DESC LIMIT 1""",
                    (predictor, instrument, entry[0], entry[1]),
                ).fetchone()
                flagged_stops += int(check[0]) if check else 0
    total, flagged = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(flagged), 0) FROM order_checks"
    ).fetchone()
    return {
        "bracket_stops": stops,
        "flagged_bracket_stops": flagged_stops,
        "flagged_share": flagged / total if total else 0.0,
    }


def main() -> None:
    from pathlib import Path

    from andy_trader.store import connect, default_database_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(default_database_path()))
    args = parser.parse_args()
    with connect(Path(args.database)) as connection:
        report = catch_report(connection)
    print(f"Bracket stops: {report['bracket_stops']}")
    print(f"Flagged at entry: {report['flagged_bracket_stops']}")
    print(f"Overall flagged share: {report['flagged_share']:.1%}")


if __name__ == "__main__":
    main()
