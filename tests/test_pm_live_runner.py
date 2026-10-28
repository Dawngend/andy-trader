"""Tests for pm_live_runner."""
from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest

from andy_trader.env import REPO_ROOT
from andy_trader import pm_live_runner
from andy_trader.polymarket_sender import Refused
import run_pm_live_runner


NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE bond_book_positions (
            market_id TEXT NOT NULL, event_id TEXT NOT NULL, title TEXT NOT NULL,
            side TEXT NOT NULL, entry_band TEXT NOT NULL, fill_price REAL NOT NULL,
            fee REAL NOT NULL, shares REAL NOT NULL, opened_at TEXT NOT NULL,
            ends_at TEXT NOT NULL, settled_at TEXT, win INTEGER, pnl REAL,
            UNIQUE (market_id, side)
        )
    """)
    connection.execute("""
        CREATE TABLE price_target_book_positions (
            market_id TEXT NOT NULL, event_id TEXT NOT NULL, title TEXT NOT NULL,
            side TEXT NOT NULL, entry_band TEXT NOT NULL, fill_price REAL NOT NULL,
            fee REAL NOT NULL, shares REAL NOT NULL, opened_at TEXT NOT NULL,
            ends_at TEXT NOT NULL, settled_at TEXT, win INTEGER, pnl REAL,
            UNIQUE (market_id, side)
        )
    """)
    connection.execute("""
        CREATE TABLE live_strategy_list (
            strategy_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            since TEXT NOT NULL,
            reason TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE pm_ai_gate_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book TEXT NOT NULL,
            market_id TEXT NOT NULL,
            side TEXT NOT NULL,
            verdict TEXT NOT NULL,
            reason TEXT NOT NULL,
            model TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            UNIQUE (book, market_id, side)
        )
    """)
    connection.commit()


def fake_market_reader(market_id: str) -> dict:
    return {
        "id": market_id,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"tok_yes_{market_id}", f"tok_no_{market_id}"]),
    }


def fake_book_reader(token_id: str) -> dict:
    return {
        "asks": [{"price": "0.50"}],
        "tick_size": "0.01",
    }


def test_empty_database_runs_without_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    calls = []

    def mock_send(intent: dict, **kwargs: Any) -> dict:
        calls.append(intent)
        return {"id": 1, "status": "dry_run"}

    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    assert summary["candidates_seen"] == 0
    assert summary["reserved"] == 0
    assert summary["forwarded"] == 0
    assert len(calls) == 0


def test_three_checks_missing_skips_position(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)

    # Strategy live row
    conn.execute(
        "INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')",
        (NOW.isoformat(),),
    )
    # AI Gate approve row
    conn.execute(
        "INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) "
        "VALUES ('bond_book', 'm1', 'Yes', 'approve', 'Looks promising', 'test-model', ?)",
        (NOW.isoformat(),),
    )
    # Fresh position
    opened_at = (NOW - timedelta(minutes=20)).isoformat()
    ends_at = (NOW + timedelta(days=2)).isoformat()
    conn.execute(
        "INSERT INTO bond_book_positions VALUES "
        "('m1', 'e1', 'Will BTC stay above 90k?', 'Yes', '0.80 to 0.90', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)",
        (opened_at, ends_at),
    )
    conn.commit()
    conn.close()

    sent = []

    def mock_send(intent: dict, **kwargs: Any) -> dict:
        sent.append(intent)
        return {"id": 42, "status": "dry_run"}

    # 1. Base case: all 3 checks hold -> forwarded!
    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    assert summary["forwarded"] == 1
    assert len(sent) == 1
    assert sent[0]["token_id"] == "tok_yes_m1"
    assert sent[0]["shares"] == Decimal("100")
    assert sent[0]["price"] == Decimal("0.50")
    assert "bond_book" in sent[0]["reason"]
    assert "bond:above_below" in sent[0]["reason"]
    assert "Looks promising" in sent[0]["reason"]

    # Now verify each check failure in a separate DB
    # a) Strategy not live
    db_a = tmp_path / "test_a.db"
    conn = sqlite3.connect(db_a)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'demoted', ?, 'pnl drop')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()
    s_a = pm_live_runner.run_pass(database_path=db_a, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s_a["candidates_seen"] == 0

    # b) No gate decision row
    db_b = tmp_path / "test_b.db"
    conn = sqlite3.connect(db_b)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()
    s_b = pm_live_runner.run_pass(database_path=db_b, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s_b["candidates_seen"] == 0

    # c) Gate decision verdict is skip
    db_c = tmp_path / "test_c.db"
    conn = sqlite3.connect(db_c)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'skip', 'Too risky', 'm', ?)", (NOW.isoformat(),))
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()
    s_c = pm_live_runner.run_pass(database_path=db_c, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s_c["candidates_seen"] == 0

    # d) Position too old (> 45 minutes)
    db_d = tmp_path / "test_d.db"
    conn = sqlite3.connect(db_d)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    old_opened = (NOW - timedelta(minutes=46)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (old_opened, ends_at))
    conn.commit()
    conn.close()
    s_d = pm_live_runner.run_pass(database_path=db_d, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s_d["candidates_seen"] == 0

    # e) Future opened_at
    db_e = tmp_path / "test_e.db"
    conn = sqlite3.connect(db_e)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    future_opened = (NOW + timedelta(minutes=5)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (future_opened, ends_at))
    conn.commit()
    conn.close()
    s_e = pm_live_runner.run_pass(database_path=db_e, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s_e["candidates_seen"] == 0

    # f) Position already settled
    db_f = tmp_path / "test_f.db"
    conn = sqlite3.connect(db_f)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    settled_at = (NOW - timedelta(minutes=5)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, ?, 1, 10.0)", (opened_at, ends_at, settled_at))
    conn.commit()
    conn.close()
    s_f = pm_live_runner.run_pass(database_path=db_f, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s_f["candidates_seen"] == 0

    # g) Position already ended
    db_g = tmp_path / "test_g.db"
    conn = sqlite3.connect(db_g)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    past_ends = (NOW - timedelta(minutes=1)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, past_ends))
    conn.commit()
    conn.close()
    s_g = pm_live_runner.run_pass(database_path=db_g, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s_g["candidates_seen"] == 0


def test_token_matching_and_missing_token(tmp_path: Path) -> None:
    db_path = tmp_path / "token.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    opened_at = (NOW - timedelta(minutes=10)).isoformat()
    ends_at = (NOW + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()

    def missing_token_market_reader(market_id: str) -> dict:
        return {
            "outcomes": json.dumps(["Up", "Down"]),
            "clobTokenIds": json.dumps(["tok_up", "tok_down"]),
        }

    calls = []
    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=lambda intent, **kwargs: calls.append(intent),
        clock=lambda: NOW,
        read_market=missing_token_market_reader,
        read_book=fake_book_reader,
    )
    assert summary["forwarded"] == 0
    assert summary["reasons"].get("no_token") == 1
    assert len(calls) == 0

    with sqlite3.connect(db_path) as c:
        rows = c.execute("SELECT * FROM pm_live_runner_log").fetchall()
        assert len(rows) == 0


def test_price_out_of_order_and_tick_rounding_and_bands(tmp_path: Path) -> None:
    db_path = tmp_path / "price.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    opened_at = (NOW - timedelta(minutes=10)).isoformat()
    ends_at = (NOW + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.90, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()

    # 1. Minimum over an out-of-order book: asks 0.95, 0.925, 0.94 -> best ask = 0.925
    # tick = 0.01 -> ceiling(0.925 / 0.01) * 0.01 = 0.93. Passed as Decimal("0.93").
    captured_intents = []

    def mock_send(intent: dict, **kwargs: Any) -> dict:
        captured_intents.append(intent)
        return {"id": 1, "status": "dry_run"}

    def out_of_order_book(token: str) -> dict:
        return {
            "asks": [{"price": "0.95"}, {"price": "0.925"}, {"price": "0.94"}],
            "tick_size": "0.01",
        }

    pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=out_of_order_book,
    )
    assert len(captured_intents) == 1
    price = captured_intents[0]["price"]
    assert isinstance(price, Decimal)
    assert price == Decimal("0.93")
    assert price % Decimal("0.01") == Decimal("0")

    # 2. Ask 0.93 and tick 0.01 stays 0.93
    captured_intents.clear()
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM pm_live_runner_log")
        c.commit()

    def exact_tick_book(token: str) -> dict:
        return {
            "asks": [{"price": "0.93"}],
            "tick_size": "0.01",
        }

    pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=exact_tick_book,
    )
    assert len(captured_intents) == 1
    assert captured_intents[0]["price"] == Decimal("0.93")

    # 3. Rounded limit price more than 0.03 above fill_price skips even when raw best ask is within 0.03
    # fill_price = 0.90. best_ask = 0.925 (within 0.025 <= 0.03). tick = 0.02 -> rounded = 0.94 (diff = 0.04 > 0.03).
    captured_intents.clear()
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM pm_live_runner_log")
        c.commit()

    def rounded_above_band_book(token: str) -> dict:
        return {
            "asks": [{"price": "0.925"}],
            "tick_size": "0.02",
        }

    s3 = pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=rounded_above_band_book,
    )
    assert len(captured_intents) == 0
    assert s3["reasons"].get("price_moved") == 1

    # 4. Limit price more than 0.03 below fill_price skips
    # fill_price = 0.90, ask = 0.86, tick = 0.01 -> limit = 0.86 (diff = -0.04 < -0.03) -> skip!
    captured_intents.clear()

    def below_band_book(token: str) -> dict:
        return {
            "asks": [{"price": "0.86"}],
            "tick_size": "0.01",
        }

    s4 = pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=below_band_book,
    )
    assert len(captured_intents) == 0
    assert s4["reasons"].get("price_moved") == 1

    # 5. Exactly 0.03 away on either side is allowed!
    # +0.03: 0.93
    captured_intents.clear()
    s5_high = pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=lambda t: {"asks": [{"price": "0.93"}], "tick_size": "0.01"},
    )
    assert len(captured_intents) == 1
    assert captured_intents[0]["price"] == Decimal("0.93")

    # -0.03: 0.87
    captured_intents.clear()
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM pm_live_runner_log")
        c.commit()

    s5_low = pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=lambda t: {"asks": [{"price": "0.87"}], "tick_size": "0.01"},
    )
    assert len(captured_intents) == 1
    assert captured_intents[0]["price"] == Decimal("0.87")


def test_deduplication_mode_isolation(tmp_path: Path) -> None:
    db_path = tmp_path / "dedup.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    opened_at = (NOW - timedelta(minutes=10)).isoformat()
    ends_at = (NOW + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()

    sent = []

    def mock_send(intent: dict, *, live: bool = False, **kwargs: Any) -> dict:
        sent.append((live, intent))
        return {"id": len(sent), "status": "sent" if live else "dry_run"}

    # Pass 1: Dry run
    s1 = pm_live_runner.run_pass(database_path=db_path, live=False, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s1["forwarded"] == 1
    assert len(sent) == 1
    assert sent[0][0] is False

    # Pass 2: Dry run again -> skipped!
    s2 = pm_live_runner.run_pass(database_path=db_path, live=False, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s2["forwarded"] == 0
    assert len(sent) == 1

    # Pass 3: Live run -> dry row does NOT block later live row!
    s3 = pm_live_runner.run_pass(database_path=db_path, live=True, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s3["forwarded"] == 1
    assert len(sent) == 2
    assert sent[1][0] is True

    # Pass 4: Live run again -> skipped!
    s4 = pm_live_runner.run_pass(database_path=db_path, live=True, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s4["forwarded"] == 0
    assert len(sent) == 2


def test_failing_check_gets_no_log_row_and_forwards_later(tmp_path: Path) -> None:
    db_path = tmp_path / "later.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    # Strategy initially not live
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'paused', ?, 'temp pause')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    opened_at = (NOW - timedelta(minutes=10)).isoformat()
    ends_at = (NOW + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()

    sent = []

    def mock_send(intent: dict, **kwargs: Any) -> dict:
        sent.append(intent)
        return {"id": 1, "status": "dry_run"}

    # Pass 1: strategy is paused
    s1 = pm_live_runner.run_pass(database_path=db_path, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s1["forwarded"] == 0
    assert len(sent) == 0

    with sqlite3.connect(db_path) as c:
        rows = c.execute("SELECT * FROM pm_live_runner_log").fetchall()
        assert len(rows) == 0

    # Turn strategy to live
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE live_strategy_list SET status = 'live'")
        c.commit()

    # Pass 2: now it passes and is forwarded!
    s2 = pm_live_runner.run_pass(database_path=db_path, send=mock_send, clock=lambda: NOW, read_market=fake_market_reader, read_book=fake_book_reader)
    assert s2["forwarded"] == 1
    assert len(sent) == 1

    with sqlite3.connect(db_path) as c:
        rows = c.execute("SELECT * FROM pm_live_runner_log").fetchall()
        assert len(rows) == 1
        assert rows[0][6] == "forwarded"


def test_reserved_and_error_count_toward_cap_and_block_resend(tmp_path: Path) -> None:
    db_path = tmp_path / "cap_counts.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    pm_live_runner.initialize_runner(conn)

    ends_at_utc = (NOW + timedelta(days=1)).isoformat()
    # 1. Existing reserved row for m1
    conn.execute(
        "INSERT INTO pm_live_runner_log (book, market_id, side, strategy_key, mode, action, ends_at_utc, decided_at) "
        "VALUES ('bond_book', 'm1', 'Yes', 'bond:above_below', 'dry', 'reserved', ?, ?)",
        (ends_at_utc, NOW.isoformat()),
    )
    # 2. Existing error row for m2
    conn.execute(
        "INSERT INTO pm_live_runner_log (book, market_id, side, strategy_key, mode, action, ends_at_utc, decided_at) "
        "VALUES ('bond_book', 'm2', 'Yes', 'bond:above_below', 'dry', 'error', ?, ?)",
        (ends_at_utc, NOW.isoformat()),
    )
    # 3. Existing skipped row for m_skip (should NOT count toward cap)
    conn.execute(
        "INSERT INTO pm_live_runner_log (book, market_id, side, strategy_key, mode, action, ends_at_utc, decided_at) "
        "VALUES ('bond_book', 'm_skip', 'Yes', 'bond:above_below', 'dry', 'skipped', ?, ?)",
        (ends_at_utc, NOW.isoformat()),
    )

    # Add m1 to bond positions -> should be blocked by reserved row
    opened_at = (NOW - timedelta(minutes=10)).isoformat()
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm3', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at_utc))
    # Add m3 to bond positions -> will try to reserve, but cap is 2 (reserved m1 + error m2 = 2) -> same_end_time!
    conn.execute("INSERT INTO bond_book_positions VALUES ('m3', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at_utc))
    conn.commit()
    conn.close()

    sent = []
    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=lambda intent, **kwargs: sent.append(intent),
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    # m1 is skipped because a row already exists in pm_live_runner_log
    # m3 is skipped with same_end_time because 2 rows (reserved + error) exist
    assert summary["forwarded"] == 0
    assert summary["reasons"].get("same_end_time") == 1
    assert len(sent) == 0


def test_same_window_cap_stops_third_order(tmp_path: Path) -> None:
    db_path = tmp_path / "cap.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))

    ends_at = (NOW + timedelta(days=1)).isoformat()
    for i in range(1, 4):
        mid = f"m{i}"
        opened_at = (NOW - timedelta(minutes=10 - i)).isoformat()
        conn.execute(
            "INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) "
            "VALUES ('bond_book', ?, 'Yes', 'approve', 'ok', 'm', ?)",
            (mid, NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO bond_book_positions VALUES "
            "(?, 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)",
            (mid, opened_at, ends_at),
        )
    conn.commit()
    conn.close()

    sent = []

    def mock_send(intent: dict, **kwargs: Any) -> dict:
        sent.append(intent)
        return {"id": len(sent), "status": "dry_run"}

    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    # Default cap is 2
    assert summary["forwarded"] == 2
    assert summary["reasons"].get("same_end_time") == 1
    assert len(sent) == 2


def test_config_fallbacks(tmp_path: Path) -> None:
    cfg_file = tmp_path / "test_config.json"

    # boolean
    cfg_file.write_text(json.dumps({"max_orders_per_end_time": True}), encoding="utf-8")
    assert pm_live_runner.load_runner_config(cfg_file)["max_orders_per_end_time"] == 2

    # string
    cfg_file.write_text(json.dumps({"max_orders_per_end_time": "5"}), encoding="utf-8")
    assert pm_live_runner.load_runner_config(cfg_file)["max_orders_per_end_time"] == 2

    # zero
    cfg_file.write_text(json.dumps({"max_orders_per_end_time": 0}), encoding="utf-8")
    assert pm_live_runner.load_runner_config(cfg_file)["max_orders_per_end_time"] == 2

    # negative
    cfg_file.write_text(json.dumps({"max_orders_per_end_time": -3}), encoding="utf-8")
    assert pm_live_runner.load_runner_config(cfg_file)["max_orders_per_end_time"] == 2

    # missing key
    cfg_file.write_text(json.dumps({}), encoding="utf-8")
    assert pm_live_runner.load_runner_config(cfg_file)["max_orders_per_end_time"] == 2

    # valid integer
    cfg_file.write_text(json.dumps({"max_orders_per_end_time": 7}), encoding="utf-8")
    assert pm_live_runner.load_runner_config(cfg_file)["max_orders_per_end_time"] == 7


def test_cli_flags_and_live_flag_database_path(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    opened_at = (NOW - timedelta(minutes=10)).isoformat()
    ends_at = (NOW + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()

    call_records = []

    def mock_send(intent: dict, *, live: bool = False, database_path: Any = None, **kwargs: Any) -> dict:
        call_records.append({"live": live, "database_path": database_path})
        return {"id": 1, "status": "sent" if live else "dry_run"}

    # 1. Without --live
    run_pm_live_runner.main(
        ["--database", str(db_path)],
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    assert len(call_records) == 1
    assert call_records[0]["live"] is False
    assert Path(call_records[0]["database_path"]) == db_path

    # 2. With --live
    call_records.clear()
    run_pm_live_runner.main(
        ["--database", str(db_path), "--live"],
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    assert len(call_records) == 1
    assert call_records[0]["live"] is True
    assert Path(call_records[0]["database_path"]) == db_path


def test_refused_recorded_as_skipped_and_error_does_not_stop_pass(tmp_path: Path) -> None:
    db_path = tmp_path / "errors.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))

    ends_at = (NOW + timedelta(days=1)).isoformat()
    for i in range(1, 4):
        mid = f"m{i}"
        opened = (NOW - timedelta(minutes=20 - i)).isoformat()
        conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', ?, 'Yes', 'approve', 'ok', 'm', ?)", (mid, NOW.isoformat()))
        conn.execute("INSERT INTO bond_book_positions VALUES (?, 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (mid, opened, ends_at))
    conn.commit()
    conn.close()

    call_count = 0

    def mock_send(intent: dict, **kwargs: Any) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Refused("cap exceeded: max_usdc_per_day")
        if call_count == 2:
            raise RuntimeError("unexpected network boom")
        return {"id": 100, "status": "dry_run"}

    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    assert summary["reserved"] == 3
    assert summary["skipped"] == 1
    assert summary["error"] == 1
    assert summary["forwarded"] == 1

    with sqlite3.connect(db_path) as c:
        rows = c.execute("SELECT market_id, action, reason FROM pm_live_runner_log ORDER BY id ASC").fetchall()
        assert len(rows) == 3
        # Row 1: skipped with Refused reason
        assert rows[0][0] == "m1"
        assert rows[0][1] == "skipped"
        assert "cap exceeded" in rows[0][2]
        # Row 2: error with exception reason
        assert rows[1][0] == "m2"
        assert rows[1][1] == "error"
        assert "unexpected network boom" in rows[1][2]
        # Row 3: forwarded
        assert rows[2][0] == "m3"
        assert rows[2][1] == "forwarded"


def test_limit_ten_counts_only_reserved(tmp_path: Path) -> None:
    db_path = tmp_path / "limit10.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))

    ends_at = (NOW + timedelta(days=1)).isoformat()
    # 12 older positions that fail because gate verdict is skip
    for i in range(1, 13):
        mid = f"m_fail_{i}"
        opened = (NOW - timedelta(minutes=40 - i)).isoformat()
        conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', ?, 'Yes', 'skip', 'bad', 'm', ?)", (mid, NOW.isoformat()))
        conn.execute("INSERT INTO bond_book_positions VALUES (?, 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (mid, opened, ends_at))

    # 1 newer position that passes all checks
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm_pass', 'Yes', 'approve', 'good', 'm', ?)", (NOW.isoformat(),))
    conn.execute("INSERT INTO bond_book_positions VALUES ('m_pass', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", ((NOW - timedelta(minutes=2)).isoformat(), ends_at))
    conn.commit()
    conn.close()

    sent = []
    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=lambda intent, **kwargs: sent.append(intent) or {"id": 1, "status": "dry_run"},
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    # The 12 older failing positions do not consume slots, so the passing newer one is forwarded!
    assert summary["forwarded"] == 1
    assert len(sent) == 1
    assert sent[0]["token_id"] == "tok_yes_m_pass"


def test_strategy_demoted_or_position_expires_before_reservation(tmp_path: Path) -> None:
    db_path = tmp_path / "race.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    opened_at = (NOW - timedelta(minutes=10)).isoformat()
    ends_at = (NOW + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()

    # During read_book (between initial check and reservation), demote the strategy!
    def mutating_book_reader(token: str) -> dict:
        with sqlite3.connect(db_path) as c:
            c.execute("UPDATE live_strategy_list SET status = 'demoted'")
            c.commit()
        return {"asks": [{"price": "0.50"}], "tick_size": "0.01"}

    sent = []
    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=lambda intent, **kwargs: sent.append(intent) or {"id": 1, "status": "dry_run"},
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=mutating_book_reader,
    )
    # Reservation check should catch that strategy is no longer live, abort transaction without log row
    assert summary["forwarded"] == 0
    assert summary["reserved"] == 0
    assert len(sent) == 0

    with sqlite3.connect(db_path) as c:
        rows = c.execute("SELECT * FROM pm_live_runner_log").fetchall()
        assert len(rows) == 0


def test_same_instant_different_offsets_share_window(tmp_path: Path) -> None:
    db_path = tmp_path / "tz_window.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))

    # All 3 represent 2026-10-31T12:00:00 UTC
    times = [
        "2026-10-31T14:00:00+02:00",
        "2026-10-31T12:00:00+00:00",
        "2026-10-31T07:00:00-05:00",
    ]
    for i, ends in enumerate(times, start=1):
        mid = f"m{i}"
        opened = (NOW - timedelta(minutes=10 - i)).isoformat()
        conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', ?, 'Yes', 'approve', 'ok', 'm', ?)", (mid, NOW.isoformat()))
        conn.execute("INSERT INTO bond_book_positions VALUES (?, 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (mid, opened, ends))
    conn.commit()
    conn.close()

    sent = []
    summary = pm_live_runner.run_pass(
        database_path=db_path,
        send=lambda intent, **kwargs: sent.append(intent) or {"id": len(sent) + 1, "status": "dry_run"},
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    # Positions 1 and 2 are forwarded, Position 3 hits same-window cap of 2
    assert summary["forwarded"] == 2
    assert summary["reasons"].get("same_end_time") == 1
    assert len(sent) == 2


def test_sender_return_statuses(tmp_path: Path) -> None:
    db_path = tmp_path / "statuses.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))

    cases = [
        ("m1", {"id": 11, "status": "dry_run"}, "forwarded", "sender status: dry_run"),
        ("m2", {"id": 12, "status": "sent"}, "forwarded", "sender status: sent"),
        ("m3", {"id": 13, "status": "filled"}, "forwarded", "sender status: filled"),
        ("m4", {"id": 14, "status": "rejected"}, "skipped", "sender rejected"),
        ("m5", {"id": 15, "status": "unknown"}, "forwarded", "sender status: unknown, check the sender ledger"),
        ("m6", {"id": 16, "status": "pending"}, "forwarded", "sender status: unknown, check the sender ledger"),
        ("m7", {"id": 17, "status": "cleared_by_operator"}, "forwarded", "sender status: cleared_by_operator, check the sender ledger"),
    ]

    for mid, ret, exp_act, exp_reason in cases:
        opened = (NOW - timedelta(minutes=5)).isoformat()
        ends = (NOW + timedelta(days=2)).isoformat()
        conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', ?, 'Yes', 'approve', 'ok', 'm', ?)", (mid, NOW.isoformat()))
        conn.execute("INSERT INTO bond_book_positions VALUES (?, 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (mid, opened, ends))
    conn.commit()
    conn.close()

    idx = 0

    def mock_send(intent: dict, **kwargs: Any) -> dict:
        nonlocal idx
        res = cases[idx][1]
        idx += 1
        return res

    # Pass with max_orders_per_end_time high enough
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"max_orders_per_end_time": 10}), encoding="utf-8")

    pm_live_runner.run_pass(
        database_path=db_path,
        config_path=cfg,
        send=mock_send,
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )

    with sqlite3.connect(db_path) as c:
        rows = c.execute("SELECT market_id, action, sender_row_id, reason FROM pm_live_runner_log ORDER BY id ASC").fetchall()
        assert len(rows) == len(cases)
        for i, (mid, ret, exp_act, exp_reason) in enumerate(cases):
            assert rows[i][0] == mid
            assert rows[i][1] == exp_act
            assert rows[i][2] == ret["id"]
            assert rows[i][3] == exp_reason


def test_no_credentials_read_and_no_limits_file_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Assert module source contains neither POLYMARKET_ nor limits
    source_path = REPO_ROOT / "andy_trader" / "pm_live_runner.py"
    source_text = source_path.read_text(encoding="utf-8")
    assert "POLYMARKET_" not in source_text, "Found POLYMARKET_ in pm_live_runner.py source!"
    assert "limits" not in source_text, "Found limits in pm_live_runner.py source!"

    # 2. Set environment variables to sentinels
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "sentinel-key")
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "sentinel-funder")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "0")

    accessed_env = []
    original_getenv = os.environ.get

    def tracking_getenv(key: str, default: Any = None) -> Any:
        accessed_env.append(key)
        return original_getenv(key, default)

    monkeypatch.setattr(os.environ, "get", tracking_getenv)

    opened_files = []
    original_open = builtins.open

    def tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened_files.append(str(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)

    db_path = tmp_path / "safe.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("INSERT INTO live_strategy_list VALUES ('bond:above_below', 'live', ?, 'ok')", (NOW.isoformat(),))
    conn.execute("INSERT INTO pm_ai_gate_decisions (book, market_id, side, verdict, reason, model, decided_at) VALUES ('bond_book', 'm1', 'Yes', 'approve', 'ok', 'm', ?)", (NOW.isoformat(),))
    opened_at = (NOW - timedelta(minutes=10)).isoformat()
    ends_at = (NOW + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'BTC above 90k', 'Yes', 'b', 0.50, 0.01, 100, ?, ?, NULL, NULL, NULL)", (opened_at, ends_at))
    conn.commit()
    conn.close()

    sent = []
    pm_live_runner.run_pass(
        database_path=db_path,
        send=lambda intent, **kwargs: sent.append(intent) or {"id": 1, "status": "dry_run"},
        clock=lambda: NOW,
        read_market=fake_market_reader,
        read_book=fake_book_reader,
    )
    assert len(sent) == 1

    # Assert nothing related to credentials was accessed
    for k in accessed_env:
        assert not k.startswith("POLYMARKET_"), f"Credential env var accessed: {k}"

    # Assert no limits file was opened
    for f in opened_files:
        assert "limits" not in f.lower(), f"Limits file opened: {f}"


def test_status_flag_prints_counts_reserved_rows_and_recent_rows(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "status.db"
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    pm_live_runner.initialize_runner(conn)

    # Seed rows
    now_str = NOW.isoformat()
    reserved_time = (NOW - timedelta(minutes=12, seconds=30)).isoformat()
    conn.execute(
        "INSERT INTO pm_live_runner_log (book, market_id, side, strategy_key, mode, action, ends_at_utc, decided_at) "
        "VALUES ('bond_book', 'm_res', 'Yes', 'bond:above_below', 'dry', 'reserved', ?, ?)",
        (now_str, reserved_time),
    )
    conn.execute(
        "INSERT INTO pm_live_runner_log (book, market_id, side, strategy_key, mode, action, sender_row_id, reason, ends_at_utc, decided_at) "
        "VALUES ('bond_book', 'm_fwd', 'Yes', 'bond:above_below', 'dry', 'forwarded', 101, 'sender status: dry_run', ?, ?)",
        (now_str, now_str),
    )
    conn.execute(
        "INSERT INTO pm_live_runner_log (book, market_id, side, strategy_key, mode, action, sender_row_id, reason, ends_at_utc, decided_at) "
        "VALUES ('bond_book', 'm_skip', 'Yes', 'bond:above_below', 'dry', 'skipped', NULL, 'sender rejected', ?, ?)",
        (now_str, now_str),
    )
    conn.execute(
        "INSERT INTO pm_live_runner_log (book, market_id, side, strategy_key, mode, action, sender_row_id, reason, ends_at_utc, decided_at) "
        "VALUES ('bond_book', 'm_err', 'Yes', 'bond:above_below', 'dry', 'error', NULL, 'connection timeout', ?, ?)",
        (now_str, now_str),
    )
    conn.commit()
    conn.close()

    def mock_send(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("send should never be called when --status is passed!")

    rc = run_pm_live_runner.main(
        ["--status", "--database", str(db_path)],
        send=mock_send,
        clock=lambda: NOW,
    )
    assert rc == 0

    captured = capsys.readouterr().out
    # Check counts
    assert "forwarded=1" in captured
    assert "skipped=1" in captured
    assert "error=1" in captured
    # Check reserved row with age (12m 30s = 750s)
    assert "m_res" in captured
    assert "750s" in captured
    # Check recent rows
    assert "m_fwd" in captured
    assert "m_skip" in captured
    assert "m_err" in captured
