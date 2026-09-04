"""Append-only SQLite store for crypto market observations and dated predictions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping, Sequence

from andy_trader.env import REPO_ROOT

DEFAULT_DB_FILENAME = "crypto_observations.db"

# Horizons the settlement job knows how to resolve. Adding one here without
# adding it to _HORIZON_DELTAS makes every prediction at that horizon
# permanently unsettleable, which is silent and therefore worse than a crash.
_HORIZON_DELTAS: Mapping[str, timedelta] = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


class CryptoStoreError(RuntimeError):
    """Raised when the store is asked to do something that would corrupt the record."""


def horizon_delta(horizon: str) -> timedelta:
    try:
        return _HORIZON_DELTAS[horizon]
    except KeyError as exc:
        known = ", ".join(sorted(_HORIZON_DELTAS))
        raise CryptoStoreError(f"Unknown horizon {horizon!r}; known horizons: {known}") from exc


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar as reported by one venue at one moment.

    `degraded` is not decoration. A collector that could not reach its source
    writes a degraded row with nulls and a reason rather than inventing a price
    or silently skipping, so a gap in the data is itself recorded as an
    observation. Downstream code must check it.
    """

    instrument: str
    venue: str
    interval: str
    open_time: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    degraded: bool = False
    degraded_reason: str | None = None

    def content_hash(self) -> str:
        """Hash the values, not the observation time.

        Re-fetching an unchanged closed candle must collapse onto the same row
        and bump times_seen. A candle whose values actually changed, which
        happens for the still-open bar and occasionally for venue revisions,
        hashes differently and lands as a new row. That is the honest record:
        we saw two different things and kept both.
        """

        payload = "|".join(
            (
                self.instrument,
                self.venue,
                self.interval,
                self.open_time,
                _fmt(self.open),
                _fmt(self.high),
                _fmt(self.low),
                _fmt(self.close),
                _fmt(self.volume),
                "degraded" if self.degraded else "ok",
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fmt(value: float | None) -> str:
    return "" if value is None else repr(round(float(value), 10))


@dataclass(frozen=True)
class Prediction:
    """A directional call, recorded before its outcome can be known.

    `probability_up` is always the probability that close(resolves_at) is
    strictly greater than `reference_price`. It is deliberately not "confidence
    in my direction": that formulation needs a transform before it can be
    scored, and the transform is where people quietly get Brier wrong.
    """

    predictor: str
    instrument: str
    horizon: str
    probability_up: float
    reference_price: float
    created_at: str
    resolves_at: str
    mode: str = "advisory"
    features: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability_up <= 1.0:
            raise CryptoStoreError(
                f"probability_up must be in [0, 1], got {self.probability_up!r}"
            )
        if self.reference_price <= 0:
            raise CryptoStoreError(f"reference_price must be positive, got {self.reference_price!r}")
        if self.mode not in {"advisory", "paper", "live"}:
            raise CryptoStoreError(f"Unknown mode {self.mode!r}")
        horizon_delta(self.horizon)


def default_database_path(environ: Mapping[str, str] | None = None) -> Path:
    import os

    source = os.environ if environ is None else environ
    configured = source.get("CRYPTO_DB_PATH", DEFAULT_DB_FILENAME)
    path = Path(configured)
    return path if path.is_absolute() else REPO_ROOT / path


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create both tables. Modelled directly on job_posting_observations."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_observations (
            content_hash TEXT PRIMARY KEY,
            instrument TEXT NOT NULL,
            venue TEXT NOT NULL,
            interval TEXT NOT NULL,
            open_time TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            degraded INTEGER NOT NULL DEFAULT 0,
            degraded_reason TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            times_seen INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS crypto_observations_lookup "
        "ON crypto_observations(instrument, interval, open_time)",
        "CREATE INDEX IF NOT EXISTS crypto_observations_venue ON crypto_observations(venue)",
        "CREATE INDEX IF NOT EXISTS crypto_observations_degraded ON crypto_observations(degraded)",
    ):
        connection.execute(index_sql)

    # Predictions are written before the outcome exists and are never updated
    # except by the settlement job, which fills only the settle_* columns. Any
    # other UPDATE against this table is a bug: it would rewrite history and
    # destroy the one property that makes the evaluation trustworthy.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            horizon TEXT NOT NULL,
            probability_up REAL NOT NULL,
            reference_price REAL NOT NULL,
            mode TEXT NOT NULL,
            features_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            resolves_at TEXT NOT NULL,
            settled_at TEXT,
            settle_price REAL,
            outcome_up INTEGER,
            settle_note TEXT,
            UNIQUE(predictor, instrument, horizon, created_at)
        )
        """
    )
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS crypto_predictions_pending "
        "ON crypto_predictions(resolves_at) WHERE settled_at IS NULL",
        "CREATE INDEX IF NOT EXISTS crypto_predictions_predictor ON crypto_predictions(predictor)",
    ):
        connection.execute(index_sql)


def record_observations(connection: sqlite3.Connection, candles: Iterable[Candle]) -> tuple[int, int]:
    """Insert new observations, bumping times_seen for ones already recorded.

    Returns (inserted, seen). Nothing is ever overwritten.
    """

    now = utc_now_iso()
    inserted = 0
    seen = 0
    for candle in candles:
        seen += 1
        digest = candle.content_hash()
        cursor = connection.execute(
            """
            INSERT INTO crypto_observations
            (content_hash, instrument, venue, interval, open_time, open, high, low, close,
             volume, degraded, degraded_reason, first_seen_at, last_seen_at, times_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(content_hash) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                times_seen = crypto_observations.times_seen + 1
            """,
            (
                digest,
                candle.instrument,
                candle.venue,
                candle.interval,
                candle.open_time,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                1 if candle.degraded else 0,
                candle.degraded_reason,
                now,
                now,
            ),
        )
        # rowcount is 1 for both INSERT and the upsert path, so compare timestamps
        # instead of trusting it.
        row = connection.execute(
            "SELECT first_seen_at, times_seen FROM crypto_observations WHERE content_hash = ?",
            (digest,),
        ).fetchone()
        if row is not None and row["times_seen"] == 1:
            inserted += 1
        del cursor
    connection.commit()
    return inserted, seen


def record_prediction(connection: sqlite3.Connection, prediction: Prediction) -> int:
    """Write one call before its outcome is knowable. Returns the prediction id."""

    cursor = connection.execute(
        """
        INSERT INTO crypto_predictions
        (predictor, instrument, horizon, probability_up, reference_price, mode,
         features_json, created_at, resolves_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(predictor, instrument, horizon, created_at) DO NOTHING
        """,
        (
            prediction.predictor,
            prediction.instrument,
            prediction.horizon,
            prediction.probability_up,
            prediction.reference_price,
            prediction.mode,
            json.dumps(dict(prediction.features), sort_keys=True),
            prediction.created_at,
            prediction.resolves_at,
        ),
    )
    connection.commit()
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    existing = connection.execute(
        """
        SELECT id FROM crypto_predictions
        WHERE predictor = ? AND instrument = ? AND horizon = ? AND created_at = ?
        """,
        (prediction.predictor, prediction.instrument, prediction.horizon, prediction.created_at),
    ).fetchone()
    if existing is None:  # pragma: no cover - only reachable on a corrupted store
        raise CryptoStoreError("Prediction neither inserted nor found after conflict")
    return int(existing["id"])


def close_price_at(
    connection: sqlite3.Connection,
    instrument: str,
    at_iso: str,
    *,
    interval: str = "1h",
    tolerance_minutes: int = 90,
) -> tuple[float | None, str]:
    """Best available close for `instrument` at `at_iso`, and a note explaining it.

    Returns (price, note). A None price means the store cannot settle this yet,
    which is a legitimate state and must not be filled with the nearest guess
    from outside the tolerance window.
    """

    target = datetime.fromisoformat(at_iso)
    window = timedelta(minutes=tolerance_minutes)
    low = (target - window).isoformat()
    high = (target + window).isoformat()
    row = connection.execute(
        """
        SELECT open_time, close, venue,
               ABS(JULIANDAY(open_time) - JULIANDAY(?)) AS distance
        FROM crypto_observations
        WHERE instrument = ? AND interval = ? AND degraded = 0 AND close IS NOT NULL
          AND open_time BETWEEN ? AND ?
        ORDER BY distance ASC, times_seen DESC
        LIMIT 1
        """,
        (at_iso, instrument, interval, low, high),
    ).fetchone()
    if row is None:
        return None, f"no non-degraded {interval} close within {tolerance_minutes}m of {at_iso}"
    return float(row["close"]), f"{row['venue']} {interval} close at {row['open_time']}"


def settle_due_predictions(
    connection: sqlite3.Connection,
    *,
    now_iso: str | None = None,
    interval: str = "1h",
    tolerance_minutes: int = 90,
) -> dict[str, int]:
    """Resolve every prediction whose horizon has elapsed.

    This function deliberately never reads probability_up. It looks up the price
    and compares it to reference_price. Keeping the outcome computation blind to
    the call is what stops a settlement bug from flattering the score.
    """

    now = now_iso or utc_now_iso()
    pending = connection.execute(
        """
        SELECT id, instrument, reference_price, resolves_at
        FROM crypto_predictions
        WHERE settled_at IS NULL AND resolves_at <= ?
        ORDER BY resolves_at ASC
        """,
        (now,),
    ).fetchall()

    settled = 0
    unresolvable = 0
    for row in pending:
        price, note = close_price_at(
            connection,
            row["instrument"],
            row["resolves_at"],
            interval=interval,
            tolerance_minutes=tolerance_minutes,
        )
        if price is None:
            unresolvable += 1
            continue
        outcome_up = 1 if price > float(row["reference_price"]) else 0
        connection.execute(
            """
            UPDATE crypto_predictions
            SET settled_at = ?, settle_price = ?, outcome_up = ?, settle_note = ?
            WHERE id = ? AND settled_at IS NULL
            """,
            (utc_now_iso(), price, outcome_up, note, row["id"]),
        )
        settled += 1
    connection.commit()
    return {"due": len(pending), "settled": settled, "unresolvable": unresolvable}


def fetch_settled(
    connection: sqlite3.Connection,
    *,
    predictor: str | None = None,
    instrument: str | None = None,
    horizon: str | None = None,
) -> Sequence[sqlite3.Row]:
    clauses = ["settled_at IS NOT NULL", "outcome_up IS NOT NULL"]
    params: list[object] = []
    for column, value in (("predictor", predictor), ("instrument", instrument), ("horizon", horizon)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    sql = "SELECT * FROM crypto_predictions WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC"
    return connection.execute(sql, tuple(params)).fetchall()
