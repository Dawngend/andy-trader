"""Durable SQLite registry for retrained models and their out-of-sample audit records.

No model is ever eligible for live prediction without an audit row in this table
certifying its out-of-sample holdout performance and whether it cleared the promotion gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ModelRegistryEntry:
    """One immutable record of a model retraining run and its holdout audit."""

    model_id: str
    trained_at: str
    instrument: str
    interval: str
    horizon: str
    train_start_time: str
    train_end_time: str
    train_bars: int
    holdout_start_time: str
    holdout_end_time: str
    holdout_bars: int
    hyperparameters: Mapping[str, object]
    holdout_brier: float
    holdout_brier_reference: float
    holdout_brier_skill: float
    base_rate_brier_skill: float
    promoted: bool
    promotion_reason: str
    weights_path: str | None = None
    id: int | None = None


def initialize_registry(connection: sqlite3.Connection) -> None:
    """Create model_registry table and associated indexes."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS model_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id TEXT NOT NULL UNIQUE,
            trained_at TEXT NOT NULL,
            instrument TEXT NOT NULL,
            interval TEXT NOT NULL,
            horizon TEXT NOT NULL,
            train_start_time TEXT NOT NULL,
            train_end_time TEXT NOT NULL,
            train_bars INTEGER NOT NULL,
            holdout_start_time TEXT NOT NULL,
            holdout_end_time TEXT NOT NULL,
            holdout_bars INTEGER NOT NULL,
            hyperparameters_json TEXT NOT NULL,
            holdout_brier REAL NOT NULL,
            holdout_brier_reference REAL NOT NULL,
            holdout_brier_skill REAL NOT NULL,
            base_rate_brier_skill REAL NOT NULL,
            promoted INTEGER NOT NULL DEFAULT 0,
            promotion_reason TEXT NOT NULL,
            weights_path TEXT
        )
        """
    )
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS model_registry_lookup "
        "ON model_registry(instrument, interval, horizon, trained_at)",
        "CREATE INDEX IF NOT EXISTS model_registry_promoted ON model_registry(promoted)",
    ):
        connection.execute(index_sql)


def record_registry_entry(
    connection: sqlite3.Connection, entry: ModelRegistryEntry
) -> int:
    """Insert a new retraining audit row. Returns the generated primary key id."""
    initialize_registry(connection)
    cursor = connection.execute(
        """
        INSERT INTO model_registry
        (model_id, trained_at, instrument, interval, horizon,
         train_start_time, train_end_time, train_bars,
         holdout_start_time, holdout_end_time, holdout_bars,
         hyperparameters_json, holdout_brier, holdout_brier_reference,
         holdout_brier_skill, base_rate_brier_skill, promoted, promotion_reason, weights_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.model_id,
            entry.trained_at,
            entry.instrument,
            entry.interval,
            entry.horizon,
            entry.train_start_time,
            entry.train_end_time,
            entry.train_bars,
            entry.holdout_start_time,
            entry.holdout_end_time,
            entry.holdout_bars,
            json.dumps(dict(entry.hyperparameters), sort_keys=True),
            float(entry.holdout_brier),
            float(entry.holdout_brier_reference),
            float(entry.holdout_brier_skill),
            float(entry.base_rate_brier_skill),
            1 if entry.promoted else 0,
            entry.promotion_reason,
            entry.weights_path,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _row_to_entry(row: sqlite3.Row) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        id=int(row["id"]),
        model_id=str(row["model_id"]),
        trained_at=str(row["trained_at"]),
        instrument=str(row["instrument"]),
        interval=str(row["interval"]),
        horizon=str(row["horizon"]),
        train_start_time=str(row["train_start_time"]),
        train_end_time=str(row["train_end_time"]),
        train_bars=int(row["train_bars"]),
        holdout_start_time=str(row["holdout_start_time"]),
        holdout_end_time=str(row["holdout_end_time"]),
        holdout_bars=int(row["holdout_bars"]),
        hyperparameters=json.loads(row["hyperparameters_json"]),
        holdout_brier=float(row["holdout_brier"]),
        holdout_brier_reference=float(row["holdout_brier_reference"]),
        holdout_brier_skill=float(row["holdout_brier_skill"]),
        base_rate_brier_skill=float(row["base_rate_brier_skill"]),
        promoted=bool(row["promoted"]),
        promotion_reason=str(row["promotion_reason"]),
        weights_path=row["weights_path"],
    )


def fetch_latest_promoted_model(
    connection: sqlite3.Connection,
    instrument: str,
    horizon: str,
    interval: str = "1h",
) -> ModelRegistryEntry | None:
    """Fetch the most recently trained promoted model entry, or None if none promoted."""
    initialize_registry(connection)
    row = connection.execute(
        """
        SELECT * FROM model_registry
        WHERE instrument = ? AND horizon = ? AND interval = ? AND promoted = 1
        ORDER BY trained_at DESC, id DESC
        LIMIT 1
        """,
        (instrument, horizon, interval),
    ).fetchone()
    if row is None:
        return None
    return _row_to_entry(row)


def fetch_registry_entries(
    connection: sqlite3.Connection,
    *,
    instrument: str | None = None,
    horizon: str | None = None,
    interval: str | None = None,
    promoted_only: bool = False,
    limit: int = 100,
) -> list[ModelRegistryEntry]:
    """List recent registry entries, newest first."""
    initialize_registry(connection)
    clauses: list[str] = []
    params: list[object] = []
    if instrument is not None:
        clauses.append("instrument = ?")
        params.append(instrument)
    if horizon is not None:
        clauses.append("horizon = ?")
        params.append(horizon)
    if interval is not None:
        clauses.append("interval = ?")
        params.append(interval)
    if promoted_only:
        clauses.append("promoted = 1")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM model_registry {where} ORDER BY trained_at DESC, id DESC LIMIT ?"
    params.append(limit)

    rows = connection.execute(sql, tuple(params)).fetchall()
    return [_row_to_entry(row) for row in rows]
