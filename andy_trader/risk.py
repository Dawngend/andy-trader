"""CT-10: risk interlock. The gate that has to exist before any of this touches real money.

Per (predictor, instrument): a daily loss cap, a drawdown-from-peak cap, a
permanent total-loss halt, and a trade-rate throttle. Limits default to the
same four numbers a real prior-art Polymarket bot shipped (see the
2026-09-03 review folded into the project's build plan): 5% daily, 25%
drawdown, 40% total loss (permanent), and a rate cap.

**Two different failure severities, on purpose.**

- `soft` trip (daily loss or drawdown breached): recoverable. A human can
  review what happened and `rearm_kill_switch` to resume. This is meant to
  catch a bad day, not end the experiment.
- `hard` halt (total loss vs starting capital breached): NOT rearmable
  through this module at all. `rearm_kill_switch` refuses and says so. The
  realistic failure mode this project has cared about from the start is a
  bad deploy or a loop bug quietly bleeding capital unattended -- a halt a
  human can casually clear from the same automation that caused the problem
  defeats the point of having one. Clearing a hard halt means opening the
  database yourself and deciding that deliberately, outside this code path.

**The kill switch is per (predictor, instrument), not global.** One
predictor blowing through its limits on one instrument should not block a
different, healthy predictor elsewhere. Nothing here is autonomous-live
specific -- it runs in paper mode too, because paper mode is supposed to be
a faithful rehearsal for live, not a different, unguarded thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3
from typing import Literal

Severity = Literal["soft", "hard"]
_ONE_HOUR = timedelta(hours=1)

DEFAULT_MAX_DAILY_LOSS_PCT = 5.0
DEFAULT_MAX_DRAWDOWN_PCT = 25.0
DEFAULT_MAX_TOTAL_LOSS_PCT = 40.0  # permanent halt, see module docstring
DEFAULT_MAX_TRADES_PER_HOUR = 4


class RiskError(ValueError):
    """Raised when a risk-interlock operation cannot be carried out honestly."""


@dataclass(frozen=True)
class RiskLimits:
    max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT
    max_total_loss_pct: float = DEFAULT_MAX_TOTAL_LOSS_PCT
    max_trades_per_hour: int = DEFAULT_MAX_TRADES_PER_HOUR


@dataclass(frozen=True)
class RiskDecision:
    """The verdict on whether a trade may proceed right now."""

    allowed: bool
    reason: str
    tripped_now: bool = False
    severity: Severity | None = None
    daily_loss_pct: float = 0.0
    drawdown_pct: float = 0.0
    total_loss_pct: float = 0.0
    trades_last_hour: int = 0


def initialize_risk(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_kill_switch (
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            tripped INTEGER NOT NULL DEFAULT 0,
            severity TEXT,
            tripped_at TEXT,
            tripped_reason TEXT,
            rearmed_at TEXT,
            rearm_note TEXT,
            PRIMARY KEY (predictor, instrument)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            event TEXT NOT NULL,
            reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def _record_event(
    connection: sqlite3.Connection, *, predictor: str, instrument: str, event: str, reason: str, now_iso: str
) -> None:
    connection.execute(
        "INSERT INTO risk_events (predictor, instrument, event, reason, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (predictor, instrument, event, reason, now_iso),
    )
    connection.commit()


def fetch_kill_switch_state(
    connection: sqlite3.Connection, *, predictor: str, instrument: str
) -> dict[str, object] | None:
    initialize_risk(connection)
    row = connection.execute(
        "SELECT * FROM risk_kill_switch WHERE predictor = ? AND instrument = ?",
        (predictor, instrument),
    ).fetchone()
    return dict(row) if row is not None else None


def _trip(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    severity: Severity,
    reason: str,
    now_iso: str,
) -> None:
    connection.execute(
        """
        INSERT INTO risk_kill_switch (predictor, instrument, tripped, severity, tripped_at, tripped_reason)
        VALUES (?, ?, 1, ?, ?, ?)
        ON CONFLICT(predictor, instrument) DO UPDATE SET
            tripped = 1, severity = excluded.severity,
            tripped_at = excluded.tripped_at, tripped_reason = excluded.tripped_reason,
            rearmed_at = NULL, rearm_note = NULL
        """,
        (predictor, instrument, severity, now_iso, reason),
    )
    connection.commit()
    _record_event(connection, predictor=predictor, instrument=instrument, event=f"tripped:{severity}", reason=reason, now_iso=now_iso)


def rearm_kill_switch(
    connection: sqlite3.Connection, *, predictor: str, instrument: str, now_iso: str, note: str
) -> bool:
    """Explicit human action to clear a soft trip. Refuses outright for a hard halt.

    Returns True if cleared, False if refused (always for a hard halt, or if
    nothing was tripped in the first place -- there is nothing to rearm).
    """

    state = fetch_kill_switch_state(connection, predictor=predictor, instrument=instrument)
    if state is None or not state["tripped"]:
        return False
    if state["severity"] == "hard":
        _record_event(
            connection, predictor=predictor, instrument=instrument,
            event="rearm_refused", reason="hard halt cannot be rearmed through this module", now_iso=now_iso,
        )
        return False
    connection.execute(
        "UPDATE risk_kill_switch SET tripped = 0, rearmed_at = ?, rearm_note = ? "
        "WHERE predictor = ? AND instrument = ?",
        (now_iso, note, predictor, instrument),
    )
    connection.commit()
    _record_event(connection, predictor=predictor, instrument=instrument, event="rearmed", reason=note, now_iso=now_iso)
    return True


def _day_start_iso(now_iso: str) -> str:
    parsed = datetime.fromisoformat(now_iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def check_and_enforce(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    starting_cash: float,
    now_iso: str,
    limits: RiskLimits = RiskLimits(),
) -> RiskDecision:
    """The gate a trade must pass before it is allowed to execute.

    Never blocks marking to market or observing -- only ever blocks the
    *trade* action. A predictor that is not allowed to trade right now still
    gets its equity tracked honestly.
    """

    initialize_risk(connection)

    existing = fetch_kill_switch_state(connection, predictor=predictor, instrument=instrument)
    if existing is not None and existing["tripped"]:
        return RiskDecision(
            allowed=False,
            reason=f"kill switch already tripped ({existing['severity']}): {existing['tripped_reason']}",
            severity=existing["severity"],
        )

    curve = connection.execute(
        "SELECT equity, recorded_at FROM paper_equity_curve WHERE predictor = ? AND instrument = ? "
        "ORDER BY recorded_at ASC",
        (predictor, instrument),
    ).fetchall()

    if not curve:
        return RiskDecision(allowed=True, reason="no equity history yet; nothing to enforce against")

    latest_equity = float(curve[-1]["equity"])
    peak_equity = max(float(row["equity"]) for row in curve)
    day_start = _day_start_iso(now_iso)
    todays_points = [row for row in curve if row["recorded_at"] >= day_start]
    day_open_equity = float(todays_points[0]["equity"]) if todays_points else latest_equity

    daily_loss_pct = max(0.0, (1.0 - latest_equity / day_open_equity) * 100.0) if day_open_equity > 0 else 0.0
    drawdown_pct = max(0.0, (1.0 - latest_equity / peak_equity) * 100.0) if peak_equity > 0 else 0.0
    total_loss_pct = max(0.0, (1.0 - latest_equity / starting_cash) * 100.0) if starting_cash > 0 else 0.0

    trades_last_hour = _count_trades_last_hour(connection, predictor=predictor, instrument=instrument, now_iso=now_iso)

    common = dict(
        daily_loss_pct=daily_loss_pct,
        drawdown_pct=drawdown_pct,
        total_loss_pct=total_loss_pct,
        trades_last_hour=trades_last_hour,
    )

    if total_loss_pct >= limits.max_total_loss_pct:
        reason = f"total loss {total_loss_pct:.2f}% >= permanent halt limit {limits.max_total_loss_pct:.2f}%"
        _trip(connection, predictor=predictor, instrument=instrument, severity="hard", reason=reason, now_iso=now_iso)
        return RiskDecision(allowed=False, reason=reason, tripped_now=True, severity="hard", **common)

    if drawdown_pct >= limits.max_drawdown_pct:
        reason = f"drawdown from peak {drawdown_pct:.2f}% >= limit {limits.max_drawdown_pct:.2f}%"
        _trip(connection, predictor=predictor, instrument=instrument, severity="soft", reason=reason, now_iso=now_iso)
        return RiskDecision(allowed=False, reason=reason, tripped_now=True, severity="soft", **common)

    if daily_loss_pct >= limits.max_daily_loss_pct:
        reason = f"daily loss {daily_loss_pct:.2f}% >= limit {limits.max_daily_loss_pct:.2f}%"
        _trip(connection, predictor=predictor, instrument=instrument, severity="soft", reason=reason, now_iso=now_iso)
        return RiskDecision(allowed=False, reason=reason, tripped_now=True, severity="soft", **common)

    if trades_last_hour >= limits.max_trades_per_hour:
        reason = f"{trades_last_hour} trades in the last hour >= rate limit {limits.max_trades_per_hour}"
        _record_event(connection, predictor=predictor, instrument=instrument, event="throttled", reason=reason, now_iso=now_iso)
        return RiskDecision(allowed=False, reason=reason, **common)

    return RiskDecision(allowed=True, reason="within all configured limits", **common)


def _count_trades_last_hour(connection: sqlite3.Connection, *, predictor: str, instrument: str, now_iso: str) -> int:
    cutoff = (datetime.fromisoformat(now_iso) - _ONE_HOUR).isoformat()
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM paper_trades WHERE predictor = ? AND instrument = ? AND executed_at > ?",
        (predictor, instrument, cutoff),
    ).fetchone()
    return int(row["n"]) if row else 0
