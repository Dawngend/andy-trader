"""Order intents: one frozen record per decision, before anything is sent."""

import sqlite3

import pytest

from andy_trader.order_intent import (
    DEFAULT_INTENT_LEVERAGE,
    OrderIntentError,
    fetch_unexpired_intents,
    initialize_order_intents,
    new_intent,
    record_intent,
)

NOW = "2026-10-17T12:00:00+00:00"


def connection():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_order_intents(db)
    return db


def build(**overrides):
    defaults = dict(
        instrument="BTC-USD", predictor="p", side="long", size=100.0,
        stop=95.0, target=110.0, source_decision_id=1, now_iso=NOW,
    )
    defaults.update(overrides)
    return new_intent(**defaults)


def test_new_intent_expires_after_its_window():
    intent = build(expiry_seconds=60)
    assert intent.created_at == NOW
    assert intent.expires_at == "2026-10-17T12:01:00+00:00"


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": "sideways"},
        {"size": 0},
        {"size": -5},
        {"stop": 0},
        {"target": -1},
        {"leverage": 0},
    ],
)
def test_rejects_invalid_fields(overrides):
    with pytest.raises(OrderIntentError):
        build(**overrides)


def test_record_intent_round_trips_every_field():
    db = connection()
    intent = build(leverage=10.0, gate_eligible=True, gate_reason="cleared the gate")
    intent_id = record_intent(db, intent)
    row = db.execute("SELECT * FROM order_intents WHERE id = ?", (intent_id,)).fetchone()
    assert row["instrument"] == "BTC-USD"
    assert row["predictor"] == "p"
    assert row["side"] == "long"
    assert row["size"] == 100.0
    assert row["stop"] == 95.0
    assert row["target"] == 110.0
    assert row["leverage"] == 10.0
    assert row["source_decision_id"] == 1
    assert row["gate_eligible"] == 1
    assert row["gate_reason"] == "cleared the gate"


def test_default_intent_leverage_cannot_drift_from_either_execution_path():
    """One leverage, three places. An intent built without an explicit leverage
    must ask for exactly what the sender sends with and what the dry-run
    executor requires, or a default-built intent gets refused downstream."""
    from andy_trader import bybit_sender, dry_run_executor

    assert DEFAULT_INTENT_LEVERAGE == bybit_sender.LEVERAGE
    assert DEFAULT_INTENT_LEVERAGE == dry_run_executor.REQUIRED_LEVERAGE


def test_leverage_defaults_to_the_constant_in_the_dataclass_builder_and_schema():
    db = connection()
    intent = build()
    assert intent.leverage == DEFAULT_INTENT_LEVERAGE

    row = db.execute(
        "SELECT * FROM order_intents WHERE id = ?", (record_intent(db, intent),)
    ).fetchone()
    assert row["leverage"] == DEFAULT_INTENT_LEVERAGE

    # The column default stands on its own, for any writer that omits it.
    db.execute(
        "INSERT INTO order_intents (instrument, predictor, side, size, stop, target, "
        "source_decision_id, created_at, expires_at, gate_eligible, gate_reason) "
        "VALUES ('BTC-USD', 'p', 'long', 1.0, 95.0, 110.0, 1, ?, ?, 0, '')",
        (NOW, "2026-10-17T12:05:00+00:00"),
    )
    assert db.execute(
        "SELECT leverage FROM order_intents ORDER BY id DESC LIMIT 1"
    ).fetchone()["leverage"] == DEFAULT_INTENT_LEVERAGE


def test_fetch_unexpired_intents_excludes_expired_and_orders_by_id():
    db = connection()
    expired = record_intent(db, build(instrument="A-USD", expiry_seconds=1))
    later = record_intent(db, build(instrument="B-USD", expiry_seconds=3600))
    earlier = record_intent(db, build(instrument="C-USD", expiry_seconds=3600))

    rows = fetch_unexpired_intents(db, now_iso="2026-10-17T12:30:00+00:00")

    assert [row["id"] for row in rows] == [later, earlier]
    assert expired not in [row["id"] for row in rows]
