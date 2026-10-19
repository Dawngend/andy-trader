"""Dry-run executor with an explicit --live sender seam.

For every unexpired, not-yet-decided intent, re-checks against the world as
it stands right now -- a fresh price, a fresh skill-gate read, a 1x leverage
cap, a max size per coin, and a trade-rate limit -- and writes a
`would_send` or `rejected` row with the reason. The legacy send_order stub
is never called. Live sending requires --live and the sender's process env gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import argparse
import sqlite3

from andy_trader.order_intent import fetch_unexpired_intents, utc_now_iso
from andy_trader.paper_gate import evaluate_paper_eligibility

MAX_LEVERAGE = 1.0
DEFAULT_MAX_SIZE_PER_COIN = 500.0
DEFAULT_MAX_TRADES_PER_HOUR = 4

# Legacy compatibility constant, ignored by the executor.
ALLOW_LIVE_SEND = False


class SendNotImplemented(NotImplementedError):
    """Raised by `send_order`. No later change has filled this in yet."""


def send_order(intent: sqlite3.Row) -> None:
    raise SendNotImplemented("send_order is not implemented; this is a dry-run-only executor")


def initialize_order_decisions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS order_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent_id INTEGER NOT NULL UNIQUE,
            checked_at TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            price_checked REAL
        )
        """
    )
    connection.commit()


def latest_price(
    connection: sqlite3.Connection, instrument: str, *, interval: str = "1h"
) -> float | None:
    """Most recently observed non-degraded close for `instrument`, or None."""

    row = connection.execute(
        """
        SELECT close FROM crypto_observations
        WHERE instrument = ? AND interval = ? AND degraded = 0 AND close IS NOT NULL
        ORDER BY open_time DESC LIMIT 1
        """,
        (instrument, interval),
    ).fetchone()
    return float(row["close"]) if row is not None else None


def _already_decided(connection: sqlite3.Connection, intent_id: int) -> bool:
    row = connection.execute(
        "SELECT 1 FROM order_decisions WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    return row is not None


def _trades_in_last_hour(
    connection: sqlite3.Connection, instrument: str, *, now_iso: str
) -> int:
    cutoff = (datetime.fromisoformat(now_iso) - timedelta(hours=1)).isoformat()
    row = connection.execute(
        """
        SELECT COUNT(*) AS n FROM order_decisions
        JOIN order_intents ON order_intents.id = order_decisions.intent_id
        WHERE order_intents.instrument = ? AND order_decisions.decision = 'would_send'
          AND order_decisions.checked_at > ?
        """,
        (instrument, cutoff),
    ).fetchone()
    return int(row["n"]) if row else 0


@dataclass(frozen=True)
class Decision:
    intent_id: int
    decision: str  # "would_send" or "rejected"
    reason: str
    price_checked: float | None


def evaluate_intent(
    connection: sqlite3.Connection,
    intent: sqlite3.Row,
    *,
    now_iso: str,
    max_size_per_coin: float = DEFAULT_MAX_SIZE_PER_COIN,
    max_trades_per_hour: int = DEFAULT_MAX_TRADES_PER_HOUR,
) -> Decision:
    """Re-check one intent against current conditions and decide its fate."""

    price = latest_price(connection, intent["instrument"])
    if price is None:
        return Decision(intent["id"], "rejected", "no live price available", None)

    if float(intent["leverage"]) > MAX_LEVERAGE:
        return Decision(
            intent["id"], "rejected",
            f"leverage {intent['leverage']:.2f}x exceeds the {MAX_LEVERAGE:.2f}x cap", price,
        )

    if float(intent["size"]) > max_size_per_coin:
        return Decision(
            intent["id"], "rejected",
            f"size {intent['size']:.2f} exceeds the {max_size_per_coin:.2f} max per coin", price,
        )

    trades_last_hour = _trades_in_last_hour(connection, intent["instrument"], now_iso=now_iso)
    if trades_last_hour >= max_trades_per_hour:
        return Decision(
            intent["id"], "rejected",
            f"{trades_last_hour} trades on {intent['instrument']} in the last hour "
            f">= limit {max_trades_per_hour}", price,
        )

    verdict = evaluate_paper_eligibility(
        connection, predictor=intent["predictor"], instrument=intent["instrument"]
    )
    if not verdict.eligible:
        return Decision(intent["id"], "rejected", f"skill gate blocks this pair: {verdict.reason}", price)

    return Decision(intent["id"], "would_send", "passed price, gate, leverage, size and rate checks", price)


def _record_decision(connection: sqlite3.Connection, decision: Decision, *, checked_at: str) -> None:
    connection.execute(
        """
        INSERT INTO order_decisions (intent_id, checked_at, decision, reason, price_checked)
        VALUES (?, ?, ?, ?, ?)
        """,
        (decision.intent_id, checked_at, decision.decision, decision.reason, decision.price_checked),
    )
    connection.commit()


def run_dry_run(
    connection: sqlite3.Connection,
    *,
    now_iso: str | None = None,
    max_size_per_coin: float = DEFAULT_MAX_SIZE_PER_COIN,
    max_trades_per_hour: int = DEFAULT_MAX_TRADES_PER_HOUR,
    live_sender=None,
) -> dict[str, int]:
    """Decide every unexpired, undecided intent. Returns a would_send/rejected count."""

    initialize_order_decisions(connection)
    checked_at = now_iso or utc_now_iso()
    intents = [
        row for row in fetch_unexpired_intents(connection, now_iso=checked_at)
        if not _already_decided(connection, row["id"])
    ]

    would_send = rejected = 0
    for intent in intents:
        decision = evaluate_intent(
            connection, intent, now_iso=checked_at,
            max_size_per_coin=max_size_per_coin, max_trades_per_hour=max_trades_per_hour,
        )
        _record_decision(connection, decision, checked_at=checked_at)
        if decision.decision == "would_send":
            would_send += 1
            if live_sender is not None:
                live_sender(connection, intent)
        else:
            rejected += 1

    return {"checked": len(intents), "would_send": would_send, "rejected": rejected}


def main(argv=None) -> None:
    from pathlib import Path

    from andy_trader.store import connect, default_database_path
    from andy_trader import bybit_sender

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args(argv)

    connection = connect(Path(default_database_path()))
    report = run_dry_run(connection, live_sender=bybit_sender.send_live if args.live else None)
    print(
        f"Checked {report['checked']} intent(s): "
        f"{report['would_send']} would_send, {report['rejected']} rejected."
    )


if __name__ == "__main__":
    main()
