"""Append-only record of model requests and per-instrument decisions."""

from __future__ import annotations

import sqlite3


def initialize_ledger(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS llm_decisions (
            id INTEGER PRIMARY KEY,
            created_at TEXT,
            batch_id TEXT,
            instrument TEXT,
            model TEXT,
            template_rev TEXT,
            config_rev TEXT,
            list_arm TEXT,
            list_seed TEXT,
            list_position INTEGER,
            rendered_context TEXT,
            raw_response TEXT,
            p_up REAL,
            reason TEXT,
            status TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            cost REAL,
            replay_of INTEGER
        )
    """)
    existing = {row[1] for row in connection.execute("PRAGMA table_info(llm_decisions)")}
    for column in ("list_arm", "list_seed"):
        if column not in existing:
            connection.execute(f"ALTER TABLE llm_decisions ADD COLUMN {column} TEXT")


def record_decision(connection: sqlite3.Connection, **values: object) -> int:
    columns = (
        "created_at", "batch_id", "instrument", "model", "template_rev",
        "config_rev", "list_arm", "list_seed", "list_position", "rendered_context", "raw_response",
        "p_up", "reason", "status", "prompt_tokens", "completion_tokens",
        "cost", "replay_of",
    )
    cursor = connection.execute(
        f"INSERT INTO llm_decisions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        tuple(values.get(column) for column in columns),
    )
    return cursor.lastrowid


def latest_decision(
    connection: sqlite3.Connection, instrument: str, not_before_iso: str
) -> sqlite3.Row | None:
    return connection.execute("""
        SELECT * FROM llm_decisions
        WHERE instrument = ? AND created_at >= ? AND status NOT IN ('replay', 'ablation')
        ORDER BY created_at DESC, id DESC LIMIT 1
    """, (instrument, not_before_iso)).fetchone()


def fetch_decisions(connection: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM llm_decisions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
