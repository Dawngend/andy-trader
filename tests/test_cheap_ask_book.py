"""Tests for the cheap ask paper book."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from andy_trader.cheap_ask_book import (
    FEE_PER_CONTRACT,
    MAX_ASK,
    SHARES,
    bet_profit,
    contract_profit,
    format_report,
    initialize_cheap_ask_book,
    round_id_from_round_start,
    run_once,
)
from andy_trader.store import initialize_database
import run_cheap_ask_book

NOW = datetime(2026, 10, 25, 12, 10, 0, tzinfo=UTC)
ROUND_START = datetime(2026, 10, 25, 12, 5, 0, tzinfo=UTC)
ROUND_ID = round_id_from_round_start(ROUND_START)
PRED_CREATED = datetime(2026, 10, 25, 12, 8, 0, tzinfo=UTC)


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    initialize_cheap_ask_book(connection)
    yield connection
    connection.close()


def insert_prediction(
    connection: sqlite3.Connection,
    *,
    prediction_id: int = 1,
    predictor: str = "fast:continuation",
    instrument: str = "BTC-USD",
    probability_up: float = 0.8,
    reference_price: float = 100_000.0,
    created_at: str = PRED_CREATED.isoformat(),
    resolves_at: str = (ROUND_START + timedelta(minutes=5)).isoformat(),
    round_start: str = ROUND_START.isoformat(),
    outcome_up: int | None = None,
    settled_at: str | None = None,
) -> None:
    features_json = json.dumps({"round_start": round_start})
    connection.execute(
        """
        INSERT INTO crypto_predictions (
            id, predictor, instrument, horizon, probability_up, reference_price,
            mode, features_json, created_at, resolves_at, settled_at, outcome_up
        ) VALUES (?, ?, ?, '2m', ?, ?, 'advisory', ?, ?, ?, ?, ?)
        """,
        (
            prediction_id,
            predictor,
            instrument,
            probability_up,
            reference_price,
            features_json,
            created_at,
            resolves_at,
            settled_at,
            outcome_up,
        ),
    )


def insert_observation(
    connection: sqlite3.Connection,
    *,
    round_id: str = ROUND_ID,
    observed_at: str = (PRED_CREATED + timedelta(seconds=2)).isoformat(),
    up_best_ask: float | None = 0.40,
    down_best_ask: float | None = 0.60,
    up_depth: float | None = 50.0,
    down_depth: float | None = 50.0,
) -> None:
    connection.execute(
        """
        INSERT INTO complete_set_observations (
            round_id, observed_at, target_notional,
            up_best_ask, down_best_ask,
            up_best_ask_depth_shares, down_best_ask_depth_shares,
            up_fill_shares, down_fill_shares
        ) VALUES (?, ?, 10.0, ?, ?, ?, ?, 10.0, 10.0)
        """,
        (round_id, observed_at, up_best_ask, down_best_ask, up_depth, down_depth),
    )


def test_qualifying_ask_and_depth_gives_bet(db: sqlite3.Connection) -> None:
    """(a) An ask of 0.40 with 50 shares of depth gives bet."""
    insert_prediction(db, prediction_id=1, probability_up=0.8)
    insert_observation(db, up_best_ask=0.40, up_depth=50.0)

    run_once(db, now=NOW)

    row = db.execute("SELECT * FROM cheap_ask_decisions WHERE prediction_id = 1").fetchone()
    assert row is not None
    assert row["decision"] == "bet"
    assert row["ask"] == 0.40
    assert row["depth"] == 50.0
    assert row["side"] == "up"
    assert row["round_id"] == ROUND_ID


def test_ask_too_high(db: sqlite3.Connection) -> None:
    """(b) An ask of 0.75 gives ask_too_high."""
    insert_prediction(db, prediction_id=1, probability_up=0.8)
    insert_observation(db, up_best_ask=0.75, up_depth=50.0)

    run_once(db, now=NOW)

    row = db.execute("SELECT * FROM cheap_ask_decisions WHERE prediction_id = 1").fetchone()
    assert row is not None
    assert row["decision"] == "ask_too_high"
    assert row["ask"] == 0.75
    assert row["depth"] == 50.0


def test_thin_book(db: sqlite3.Connection) -> None:
    """(c) An ask of 0.40 with 5 shares gives thin_book."""
    insert_prediction(db, prediction_id=1, probability_up=0.8)
    insert_observation(db, up_best_ask=0.40, up_depth=5.0)

    run_once(db, now=NOW)

    row = db.execute("SELECT * FROM cheap_ask_decisions WHERE prediction_id = 1").fetchone()
    assert row is not None
    assert row["decision"] == "thin_book"
    assert row["ask"] == 0.40
    assert row["depth"] == 5.0


def test_no_order_book_within_20_seconds_gives_no_book(db: sqlite3.Connection) -> None:
    """(d) A prediction with no order book within 20 seconds gives no_book."""
    insert_prediction(db, prediction_id=1, probability_up=0.8)
    # Observation is 25 seconds later, which is beyond the 20-second threshold.
    late_obs = (PRED_CREATED + timedelta(seconds=25)).isoformat()
    insert_observation(db, observed_at=late_obs, up_best_ask=0.40, up_depth=50.0)

    run_once(db, now=NOW)

    row = db.execute("SELECT * FROM cheap_ask_decisions WHERE prediction_id = 1").fetchone()
    assert row is not None
    assert row["decision"] == "no_book"
    assert row["ask"] is None
    assert row["depth"] is None
    assert row["side"] == "up"


def test_down_side_uses_down_ask_and_depth(db: sqlite3.Connection) -> None:
    """(e) The down side uses the down ask and down depth."""
    insert_prediction(db, prediction_id=1, probability_up=0.2)
    insert_observation(
        db,
        up_best_ask=0.85,
        up_depth=100.0,
        down_best_ask=0.35,
        down_depth=25.0,
    )

    run_once(db, now=NOW)

    row = db.execute("SELECT * FROM cheap_ask_decisions WHERE prediction_id = 1").fetchone()
    assert row is not None
    assert row["side"] == "down"
    assert row["ask"] == 0.35
    assert row["depth"] == 25.0
    assert row["decision"] == "bet"


def test_prediction_already_decided_is_not_decided_twice(db: sqlite3.Connection) -> None:
    """(f) A prediction already decided is not decided twice."""
    insert_prediction(db, prediction_id=1, probability_up=0.8)
    insert_observation(db, up_best_ask=0.40, up_depth=50.0)

    run_once(db, now=NOW)
    assert db.execute("SELECT COUNT(*) FROM cheap_ask_decisions").fetchone()[0] == 1

    first_row = dict(db.execute("SELECT * FROM cheap_ask_decisions WHERE prediction_id = 1").fetchone())

    # Add a newer observation with a different ask, then run again
    insert_observation(
        db,
        observed_at=(PRED_CREATED + timedelta(seconds=5)).isoformat(),
        up_best_ask=0.99,
        up_depth=100.0,
    )
    run_once(db, now=NOW + timedelta(minutes=1))

    assert db.execute("SELECT COUNT(*) FROM cheap_ask_decisions").fetchone()[0] == 1
    second_row = dict(db.execute("SELECT * FROM cheap_ask_decisions WHERE prediction_id = 1").fetchone())
    assert first_row == second_row


def test_settlement(db: sqlite3.Connection) -> None:
    """(g) Settlement sets win correctly for an up win, an up loss and a down win,

    and leaves unsettled predictions alone.
    """
    # 1. Up win: probability_up > 0.5 (side='up'), outcome_up=1 -> win=1
    insert_prediction(
        db,
        prediction_id=1,
        probability_up=0.8,
        created_at=(PRED_CREATED - timedelta(minutes=5)).isoformat(),
        outcome_up=1,
    )
    # 2. Up loss: probability_up > 0.5 (side='up'), outcome_up=0 -> win=0
    insert_prediction(
        db,
        prediction_id=2,
        probability_up=0.9,
        created_at=(PRED_CREATED - timedelta(minutes=4)).isoformat(),
        outcome_up=0,
    )
    # 3. Down win: probability_up <= 0.5 (side='down'), outcome_up=0 -> win=1
    insert_prediction(
        db,
        prediction_id=3,
        probability_up=0.1,
        created_at=(PRED_CREATED - timedelta(minutes=3)).isoformat(),
        outcome_up=0,
    )
    # 4. Unsettled: outcome_up is None -> win is None, settled_at is None
    insert_prediction(
        db,
        prediction_id=4,
        probability_up=0.85,
        created_at=(PRED_CREATED - timedelta(minutes=2)).isoformat(),
        outcome_up=None,
    )

    insert_observation(db, up_best_ask=0.40, down_best_ask=0.40, up_depth=50.0, down_depth=50.0)

    run_once(db, now=NOW)

    row1 = db.execute("SELECT win, settled_at FROM cheap_ask_decisions WHERE prediction_id = 1").fetchone()
    assert row1["win"] == 1
    assert row1["settled_at"] is not None

    row2 = db.execute("SELECT win, settled_at FROM cheap_ask_decisions WHERE prediction_id = 2").fetchone()
    assert row2["win"] == 0
    assert row2["settled_at"] is not None

    row3 = db.execute("SELECT win, settled_at FROM cheap_ask_decisions WHERE prediction_id = 3").fetchone()
    assert row3["win"] == 1
    assert row3["settled_at"] is not None

    row4 = db.execute("SELECT win, settled_at FROM cheap_ask_decisions WHERE prediction_id = 4").fetchone()
    assert row4["win"] is None
    assert row4["settled_at"] is None


def test_report_profit_calculation(db: sqlite3.Connection) -> None:
    """(h) The report profit for a winning bet at ask 0.40 is 10 * 0.60 - 10 * 0.02."""
    expected_profit = 10 * 0.60 - 10 * 0.02
    calculated = bet_profit(win=1, ask=0.40)
    assert calculated == pytest.approx(expected_profit)
    assert contract_profit(win=1, ask=0.40) == pytest.approx(0.60 - 0.02)

    insert_prediction(db, prediction_id=1, probability_up=0.8, outcome_up=1)
    insert_observation(db, up_best_ask=0.40, up_depth=50.0)
    run_once(db, now=NOW)

    report_text = format_report(db)
    assert "Average profit per contract: +0.5800" in report_text
    assert "Bets so far: 1 of 100 needed for a first verdict" in report_text


def test_running_twice_in_a_row_changes_nothing(db: sqlite3.Connection) -> None:
    """(i) Running twice in a row changes nothing."""
    insert_prediction(db, prediction_id=1, probability_up=0.8, created_at=PRED_CREATED.isoformat(), outcome_up=1)
    insert_prediction(
        db,
        prediction_id=2,
        probability_up=0.2,
        created_at=(PRED_CREATED - timedelta(minutes=1)).isoformat(),
        outcome_up=0,
    )
    insert_observation(db, up_best_ask=0.40, down_best_ask=0.40, up_depth=50.0, down_depth=50.0)

    run_once(db, now=NOW)
    rows_after_first = [
        tuple(r) for r in db.execute("SELECT * FROM cheap_ask_decisions ORDER BY prediction_id").fetchall()
    ]

    run_once(db, now=NOW)
    rows_after_second = [
        tuple(r) for r in db.execute("SELECT * FROM cheap_ask_decisions ORDER BY prediction_id").fetchall()
    ]

    assert rows_after_first == rows_after_second


def test_cli_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    db_file = tmp_path / "test_cli.db"
    with sqlite3.connect(db_file) as connection:
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        insert_prediction(connection, prediction_id=1, probability_up=0.8, outcome_up=1)
        insert_observation(connection, up_best_ask=0.40, up_depth=50.0)
        connection.commit()

    monkeypatch.setattr(run_cheap_ask_book, "LOCK_PATH", tmp_path / ".test.lock")
    monkeypatch.setattr(run_cheap_ask_book, "JOURNAL_PATH", tmp_path / ".test.jsonl")

    exit_code = run_cheap_ask_book.main(["--database", str(db_file), "--report"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Cheap Ask Paper Book Report" in captured.out
    assert "Bets so far: 1 of 100 needed for a first verdict" in captured.out
