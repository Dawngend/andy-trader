"""Order intents: one frozen record per decision, before anything is sent.

An intent captures what a decision wanted to do -- instrument, side, size,
stop, target, which decision produced it, and the skill gate's verdict at
that moment -- and nothing else touches it afterwards. `dry_run_executor`
re-checks the world against a *fresh* gate read and current conditions
before deciding whether the intent would have been sent; the gate fields
stored here are the frozen record of what was known when the intent was
created, kept for audit, not re-used as the live answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3
from typing import Literal

Side = Literal["long", "short"]

DEFAULT_EXPIRY_SECONDS = 300

# The one leverage every path agrees on: an intent built without an explicit
# leverage asks for exactly what `bybit_sender.LEVERAGE` sends with and what
# `dry_run_executor.REQUIRED_LEVERAGE` requires, so a default-built intent is
# never refused over leverage. Kept as a literal to avoid importing the sender
# module here (same pattern as the executor); a test asserts the three stay
# equal. Rows already stored keep whatever leverage they were written with.
DEFAULT_INTENT_LEVERAGE = 10.0


class OrderIntentError(ValueError):
    """Raised when an order intent is not internally consistent."""


@dataclass(frozen=True)
class OrderIntent:
    instrument: str
    predictor: str
    side: Side
    size: float
    stop: float
    target: float
    source_decision_id: int
    created_at: str
    expires_at: str
    leverage: float = DEFAULT_INTENT_LEVERAGE
    gate_eligible: bool = False
    gate_reason: str = ""

    def __post_init__(self) -> None:
        if self.side not in ("long", "short"):
            raise OrderIntentError(f"side must be 'long' or 'short', got {self.side!r}")
        if self.size <= 0:
            raise OrderIntentError(f"size must be positive, got {self.size!r}")
        if self.stop <= 0 or self.target <= 0:
            raise OrderIntentError("stop and target must be positive prices")
        if self.leverage <= 0:
            raise OrderIntentError(f"leverage must be positive, got {self.leverage!r}")
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.created_at):
            raise OrderIntentError("expires_at must be after created_at")


def new_intent(
    *,
    instrument: str,
    predictor: str,
    side: Side,
    size: float,
    stop: float,
    target: float,
    source_decision_id: int,
    now_iso: str,
    expiry_seconds: int = DEFAULT_EXPIRY_SECONDS,
    leverage: float = DEFAULT_INTENT_LEVERAGE,
    gate_eligible: bool = False,
    gate_reason: str = "",
) -> OrderIntent:
    """Build an intent expiring `expiry_seconds` after `now_iso`."""

    created = datetime.fromisoformat(now_iso)
    expires = created + timedelta(seconds=expiry_seconds)
    return OrderIntent(
        instrument=instrument,
        predictor=predictor,
        side=side,
        size=size,
        stop=stop,
        target=target,
        source_decision_id=source_decision_id,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        leverage=leverage,
        gate_eligible=gate_eligible,
        gate_reason=gate_reason,
    )


def initialize_order_intents(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS order_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument TEXT NOT NULL,
            predictor TEXT NOT NULL,
            side TEXT NOT NULL,
            size REAL NOT NULL,
            stop REAL NOT NULL,
            target REAL NOT NULL,
            leverage REAL NOT NULL DEFAULT {DEFAULT_INTENT_LEVERAGE},
            source_decision_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            gate_eligible INTEGER NOT NULL,
            gate_reason TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS order_intents_expiry ON order_intents(expires_at)"
    )
    connection.commit()


def record_intent(connection: sqlite3.Connection, intent: OrderIntent) -> int:
    initialize_order_intents(connection)
    cursor = connection.execute(
        """
        INSERT INTO order_intents
        (instrument, predictor, side, size, stop, target, leverage,
         source_decision_id, created_at, expires_at, gate_eligible, gate_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent.instrument,
            intent.predictor,
            intent.side,
            intent.size,
            intent.stop,
            intent.target,
            intent.leverage,
            intent.source_decision_id,
            intent.created_at,
            intent.expires_at,
            int(intent.gate_eligible),
            intent.gate_reason,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def fetch_unexpired_intents(
    connection: sqlite3.Connection, *, now_iso: str
) -> list[sqlite3.Row]:
    """Intents not yet past their expiry, oldest first."""

    initialize_order_intents(connection)
    return connection.execute(
        "SELECT * FROM order_intents WHERE expires_at > ? ORDER BY id ASC",
        (now_iso,),
    ).fetchall()


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
