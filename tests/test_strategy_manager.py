"""Tests for Strategy Manager."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import pytest

from andy_trader.bond_book import (
    PRICE_TARGET_NO,
    PRICE_TARGET_YES,
    UP_DOWN,
)
from andy_trader.strategy_manager import (
    DEFAULT_CONFIG,
    DEFAULT_TIMEOUT,
    evaluate_strategy,
    load_config,
    parse_timestamp,
    read_positions,
    run_strategy_manager_pass,
)
import run_strategy_manager as runner

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate_default_paths(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "strategy_manager_config.json"
    out = tmp_path / "research" / "strategy_manager.md"
    monkeypatch.setattr("andy_trader.strategy_manager.DEFAULT_CONFIG_PATH", cfg)
    monkeypatch.setattr("andy_trader.strategy_manager.DEFAULT_OUTPUT_PATH", out)
    monkeypatch.setattr("run_strategy_manager.DEFAULT_CONFIG_PATH", cfg)
    monkeypatch.setattr("run_strategy_manager.DEFAULT_OUTPUT_PATH", out)


def create_bond_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bond_book_positions (
            market_id TEXT NOT NULL, event_id TEXT NOT NULL, title TEXT NOT NULL,
            side TEXT NOT NULL, entry_band TEXT NOT NULL, fill_price REAL NOT NULL,
            fee REAL NOT NULL, shares REAL NOT NULL, opened_at TEXT NOT NULL,
            ends_at TEXT NOT NULL, settled_at TEXT, win INTEGER, pnl REAL,
            UNIQUE (market_id, side)
        )
    """)


def create_price_target_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_target_book_positions (
            market_id TEXT NOT NULL, event_id TEXT NOT NULL, title TEXT NOT NULL,
            side TEXT NOT NULL, entry_band TEXT NOT NULL, fill_price REAL NOT NULL,
            fee REAL NOT NULL, shares REAL NOT NULL, opened_at TEXT NOT NULL,
            ends_at TEXT NOT NULL, settled_at TEXT, win INTEGER, pnl REAL,
            UNIQUE (market_id, side)
        )
    """)


def create_copy_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS copy_book_positions (
            wallet_name TEXT NOT NULL,
            wallet TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            token_id TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            title TEXT NOT NULL,
            outcome TEXT NOT NULL,
            their_price REAL NOT NULL,
            their_timestamp INTEGER NOT NULL,
            fill_price REAL NOT NULL,
            fee REAL NOT NULL,
            shares REAL NOT NULL,
            opened_at TEXT NOT NULL,
            delay_seconds REAL NOT NULL,
            settled_at TEXT,
            win INTEGER,
            pnl REAL,
            UNIQUE (wallet, tx_hash, token_id)
        )
    """)


def insert_pt_positions(
    conn: sqlite3.Connection,
    count: int,
    pnl: float,
    settled_at: datetime | str,
    start_id: int = 1,
) -> None:
    iso = settled_at.isoformat() if isinstance(settled_at, datetime) else settled_at
    for i in range(start_id, start_id + count):
        conn.execute(
            """
            INSERT INTO price_target_book_positions
            (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at, settled_at, win, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"pt_m_{i}",
                f"pt_e_{i}",
                "Will Bitcoin reach 100,000?",
                "No",
                "0.90 to 0.95",
                0.90,
                0.0,
                10.0,
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
                iso,
                1 if pnl > 0 else 0,
                pnl,
            ),
        )


def test_promote_at_exactly_100_settled_and_not_at_99(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # 99 settled winning positions
    insert_pt_positions(conn, 99, 10.0, T0, start_id=1)

    decisions, _ = run_strategy_manager_pass(db_path, now=T0)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.strategy_key == "price_target"
    assert d.new_status == "candidate"
    assert d.action == "new"

    # Add 1 more to reach exactly 100 settled positions
    insert_pt_positions(conn, 1, 10.0, T0, start_id=100)
    decisions, _ = run_strategy_manager_pass(db_path, now=T0 + timedelta(hours=1))
    d = decisions[0]
    assert d.strategy_key == "price_target"
    assert d.new_status == "live"
    assert d.action == "promote"
    conn.close()


def test_no_promote_when_lower_bound_zero_or_below(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # 100 settled positions with 0 profit
    insert_pt_positions(conn, 100, 0.0, T0, start_id=1)

    decisions, _ = run_strategy_manager_pass(db_path, now=T0)
    d = decisions[0]
    assert d.strategy_key == "price_target"
    assert d.new_status == "candidate"
    assert d.action == "new"
    conn.close()


def test_demote_only_with_50_settled_in_last_30_days_and_negative_upper_bound(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # Start with 100 winning positions older than 30 days so they promote to live on lifetime,
    # but do not pollute the 30-day demotion window.
    t_win = T0 - timedelta(days=35)
    insert_pt_positions(conn, 100, 10.0, t_win, start_id=1)
    run_strategy_manager_pass(db_path, now=T0)

    # Move 25 hours forward (beyond 24h cooldown)
    t1 = T0 + timedelta(hours=25)

    # 49 losing positions in last 30 days
    insert_pt_positions(conn, 49, -10.0, t1 - timedelta(days=5), start_id=101)
    # 1 losing position older than 30 days (must not count for demotion)
    insert_pt_positions(conn, 1, -10.0, t1 - timedelta(days=35), start_id=150)

    decisions, _ = run_strategy_manager_pass(db_path, now=t1)
    d = decisions[0]
    assert d.new_status == "live"
    assert not d.is_change

    # Add 1 more losing position within 30 days (total 50 within 30 days)
    insert_pt_positions(conn, 1, -10.0, t1 - timedelta(days=2), start_id=151)
    decisions, _ = run_strategy_manager_pass(db_path, now=t1)
    d = decisions[0]
    assert d.new_status == "removed"
    assert d.action == "demote"
    conn.close()


def test_one_change_per_day_rule_live_and_removed(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # Promote to live at T0 with winning positions older than 30 days
    t_win = T0 - timedelta(days=35)
    insert_pt_positions(conn, 100, 10.0, t_win, start_id=1)
    run_strategy_manager_pass(db_path, now=T0)

    # At T0 + 12 hours: add 50 losing positions in the 30-day window
    t12h = T0 + timedelta(hours=12)
    insert_pt_positions(conn, 50, -10.0, t12h - timedelta(hours=1), start_id=101)

    decisions, _ = run_strategy_manager_pass(db_path, now=t12h)
    d = decisions[0]
    # Blocked by 24h rule!
    assert d.new_status == "live"
    assert not d.is_change

    # At T0 + 25 hours: demotion should now proceed
    t25h = T0 + timedelta(hours=25)
    decisions, _ = run_strategy_manager_pass(db_path, now=t25h)
    d = decisions[0]
    assert d.new_status == "removed"
    assert d.action == "demote"

    # At T0 + 30 hours (< 24h after demotion): add 100 winning positions
    t30h = T0 + timedelta(hours=30)
    insert_pt_positions(conn, 100, 10.0, t30h - timedelta(minutes=10), start_id=200)

    decisions, _ = run_strategy_manager_pass(db_path, now=t30h)
    d = decisions[0]
    # Blocked by 24h rule from demote!
    assert d.new_status == "removed"
    assert not d.is_change

    # At T0 + 50 hours (> 24h after demotion): promotion can now proceed
    t50h = T0 + timedelta(hours=50)
    decisions, _ = run_strategy_manager_pass(db_path, now=t50h)
    d = decisions[0]
    assert d.new_status == "live"
    assert d.action == "promote"
    conn.close()


def test_candidate_first_promotion_not_blocked_by_24h_rule(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # First seen as candidate with 10 positions
    insert_pt_positions(conn, 10, 10.0, T0, start_id=1)
    decisions, _ = run_strategy_manager_pass(db_path, now=T0)
    assert decisions[0].new_status == "candidate"

    # Only 1 hour later, 90 more winning positions arrive (total 100)
    t1h = T0 + timedelta(hours=1)
    insert_pt_positions(conn, 90, 10.0, t1h, start_id=11)
    decisions, _ = run_strategy_manager_pass(db_path, now=t1h)

    # Candidate promotion MUST NOT be blocked by 24h rule
    assert decisions[0].new_status == "live"
    assert decisions[0].action == "promote"
    conn.close()


def test_removed_strategy_not_promoted_on_old_lifetime_results(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # Promote to live with 100 winning positions older than 30 days
    t_win = T0 - timedelta(days=35)
    insert_pt_positions(conn, 100, 10.0, t_win, start_id=1)
    run_strategy_manager_pass(db_path, now=T0)

    # Demote at T0 + 25h with 50 losses in 30-day window
    t_demote = T0 + timedelta(hours=25)
    insert_pt_positions(conn, 50, -10.0, t_demote - timedelta(hours=1), start_id=101)
    run_strategy_manager_pass(db_path, now=t_demote)

    # Advance 25 hours past removal
    t_check = t_demote + timedelta(hours=25)
    # Add only 20 fresh winning positions since removal
    insert_pt_positions(conn, 20, 10.0, t_demote + timedelta(hours=2), start_id=200)

    decisions, _ = run_strategy_manager_pass(db_path, now=t_check)
    # Total lifetime winning positions is 120, but only 20 are fresh since removal -> stays removed
    assert decisions[0].new_status == "removed"
    assert not decisions[0].is_change

    # Now add 80 more fresh winning positions (making 100 fresh positions since removal)
    insert_pt_positions(conn, 80, 10.0, t_demote + timedelta(hours=3), start_id=250)
    decisions, _ = run_strategy_manager_pass(db_path, now=t_check + timedelta(hours=1))
    assert decisions[0].new_status == "live"
    assert decisions[0].action == "promote"
    conn.close()


def test_failed_first_run_leaves_no_tables_and_no_rows(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    insert_pt_positions(conn, 10, 10.0, T0)
    conn.close()

    def fail_before_commit(_conn: sqlite3.Connection):
        raise RuntimeError("simulated write failure")

    with pytest.raises(RuntimeError, match="simulated write failure"):
        run_strategy_manager_pass(
            db_path,
            now=T0,
            on_before_commit=fail_before_commit,
        )

    verify_conn = sqlite3.connect(db_path)
    cur = verify_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('live_strategy_list', 'strategy_journal')"
    )
    assert cur.fetchall() == []
    verify_conn.close()


def test_position_scores_read_inside_transaction(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    insert_pt_positions(conn, 10, 10.0, T0)
    conn.close()

    original_read_positions = read_positions
    was_in_transaction = []

    def wrapped_read_positions(connection: sqlite3.Connection):
        was_in_transaction.append(connection.in_transaction)
        return original_read_positions(connection)

    monkeypatch.setattr("andy_trader.strategy_manager.read_positions", wrapped_read_positions)

    run_strategy_manager_pass(db_path, now=T0)
    assert was_in_transaction == [True]


def test_concurrent_passes_waiting_and_no_duplicate_change(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    insert_pt_positions(conn, 100, 10.0, T0)
    conn.close()

    results = []
    barrier = threading.Barrier(2)

    def worker1():
        # Holds the transaction lock
        c = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        barrier.wait()
        time.sleep(0.3)
        c.execute("""
            CREATE TABLE IF NOT EXISTS live_strategy_list (
                strategy_key TEXT PRIMARY KEY, status TEXT, since TEXT, reason TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS strategy_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT, strategy_key TEXT,
                action TEXT, settled INTEGER, avg_pnl REAL, interval_low REAL,
                interval_high REAL, reason TEXT
            )
        """)
        c.execute(
            "INSERT INTO live_strategy_list VALUES ('price_target', 'live', ?, 'promoted')",
            (T0.isoformat(),),
        )
        c.execute(
            "INSERT INTO strategy_journal (recorded_at, strategy_key, action, settled, reason) VALUES (?, 'price_target', 'promote', 100, 'promoted')",
            (T0.isoformat(),),
        )
        c.execute("COMMIT")
        c.close()
        results.append("worker1 committed")

    def worker2():
        barrier.wait()
        # Should wait for worker1 to commit
        decisions, _ = run_strategy_manager_pass(db_path, now=T0 + timedelta(hours=1), timeout=5.0)
        results.append(f"worker2 decisions: {len(decisions)} change={decisions[0].is_change}")

    t1 = threading.Thread(target=worker1)
    t2 = threading.Thread(target=worker2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert "worker1 committed" in results
    assert "worker2 decisions: 1 change=False" in results

    # Verify journal only has worker1's row, worker2 made no duplicate change
    verify_conn = sqlite3.connect(db_path)
    cur = verify_conn.execute("SELECT COUNT(*) FROM strategy_journal")
    assert cur.fetchone()[0] == 1
    verify_conn.close()


def test_report_write_failure_leaves_decisions_committed_and_exits_nonzero(
    tmp_path: Path, monkeypatch, capsys
):
    db_path = tmp_path / "test.db"
    output_path = tmp_path / "research" / "strategy_manager.md"
    config_path = tmp_path / "config.json"

    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    insert_pt_positions(conn, 100, 10.0, T0)
    conn.close()

    def failing_replace(_src, _dst):
        raise OSError("disk write error")

    monkeypatch.setattr(os, "replace", failing_replace)

    code = runner.main([
        "--database", str(db_path),
        "--output", str(output_path),
        "--config", str(config_path),
    ])
    assert code != 0

    # Ensure decisions are still committed in the database
    verify_conn = sqlite3.connect(db_path)
    cur = verify_conn.execute("SELECT status FROM live_strategy_list WHERE strategy_key='price_target'")
    row = cur.fetchone()
    assert row is not None and row[0] == "live"
    verify_conn.close()


def test_bond_slices_scored_separately(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_bond_table(conn)

    # 100 winning positions for PRICE_TARGET_NO slice
    for i in range(1, 101):
        conn.execute(
            """
            INSERT INTO bond_book_positions
            (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at, settled_at, win, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"b_m_{i}",
                f"b_e_{i}",
                "Will Bitcoin hit 100k?",
                "No",
                "0.90 to 0.95",
                0.90,
                0.0,
                10.0,
                T0.isoformat(),
                T0.isoformat(),
                T0.isoformat(),
                1,
                10.0,
            ),
        )

    # 5 positions for UP_DOWN slice
    for i in range(101, 106):
        conn.execute(
            """
            INSERT INTO bond_book_positions
            (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at, settled_at, win, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"b_m_{i}",
                f"b_e_{i}",
                "Bitcoin Up or Down on October 5?",
                "No",
                "0.90 to 0.95",
                0.90,
                0.0,
                10.0,
                T0.isoformat(),
                T0.isoformat(),
                T0.isoformat(),
                1,
                10.0,
            ),
        )
    conn.close()

    decisions, _ = run_strategy_manager_pass(db_path, now=T0)
    by_key = {d.strategy_key: d for d in decisions}
    assert f"bond:{PRICE_TARGET_NO}" in by_key
    assert f"bond:{UP_DOWN}" in by_key

    # bond:price_target_no reached 100 -> live
    assert by_key[f"bond:{PRICE_TARGET_NO}"].new_status == "live"
    # bond:up_down only has 5 -> candidate
    assert by_key[f"bond:{UP_DOWN}"].new_status == "candidate"


def test_missing_config_created_with_defaults(tmp_path: Path):
    db_path = tmp_path / "test.db"
    config_path = tmp_path / "custom_config.json"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    conn.close()

    assert not config_path.exists()
    run_strategy_manager_pass(db_path, config_path=config_path, now=T0)
    assert config_path.is_file()

    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    assert loaded == DEFAULT_CONFIG


def test_dry_run_writes_nothing(tmp_path: Path):
    db_path = tmp_path / "test.db"
    config_path = tmp_path / "missing_config.json"
    output_path = tmp_path / "report.md"

    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    insert_pt_positions(conn, 100, 10.0, T0)
    conn.close()

    decisions, report = run_strategy_manager_pass(
        db_path,
        config_path=config_path,
        output_path=output_path,
        dry_run=True,
        now=T0,
    )
    assert len(decisions) == 1
    assert decisions[0].strategy_key == "price_target"

    # Verify nothing was written
    assert not config_path.exists()
    assert not output_path.exists()

    verify_conn = sqlite3.connect(db_path)
    cur = verify_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('live_strategy_list', 'strategy_journal')"
    )
    assert cur.fetchall() == []
    verify_conn.close()


def test_journal_append_only_and_no_row_for_unchanged_hold(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    insert_pt_positions(conn, 10, 10.0, T0)
    conn.close()

    # Pass 1: candidate created
    run_strategy_manager_pass(db_path, now=T0)
    verify_conn = sqlite3.connect(db_path)
    cur = verify_conn.execute("SELECT COUNT(*) FROM strategy_journal")
    assert cur.fetchone()[0] == 1

    # Pass 2: still candidate (hold)
    run_strategy_manager_pass(db_path, now=T0 + timedelta(hours=1))
    cur = verify_conn.execute("SELECT COUNT(*) FROM strategy_journal")
    assert cur.fetchone()[0] == 1
    verify_conn.close()


def test_dry_run_space_in_path(tmp_path: Path):
    space_dir = tmp_path / "Folder With Spaces"
    space_dir.mkdir(parents=True, exist_ok=True)
    db_path = space_dir / "my trader.db"

    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    insert_pt_positions(conn, 50, 1.0, T0)
    conn.close()

    decisions, report = run_strategy_manager_pass(db_path, dry_run=True, now=T0)
    assert len(decisions) == 1
    assert decisions[0].strategy_key == "price_target"


def test_settled_with_null_or_invalid_pnl_ignored(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # 1 valid settled position
    insert_pt_positions(conn, 1, 10.0, T0, start_id=1)

    # 1 row with settled_at set but pnl NULL
    conn.execute(
        """
        INSERT INTO price_target_book_positions
        (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at, settled_at, win, pnl)
        VALUES ('m_null', 'e_null', 'Title', 'No', 'band', 0.9, 0.0, 10, '2026-01-01', '2026-01-02', ?, 1, NULL)
        """,
        (T0.isoformat(),),
    )
    # 1 row with settled_at set but non-numeric pnl
    conn.execute(
        """
        INSERT INTO price_target_book_positions
        (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at, settled_at, win, pnl)
        VALUES ('m_bad', 'e_bad', 'Title', 'No', 'band', 0.9, 0.0, 10, '2026-01-01', '2026-01-02', ?, 1, 'not_a_number')
        """,
        (T0.isoformat(),),
    )
    conn.close()

    decisions, report = run_strategy_manager_pass(db_path, now=T0)
    assert decisions[0].settled == 1
    assert "Ignored Positions**: 2 positions were ignored" in report


def test_unparseable_or_empty_settled_at_ignored(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # 1 valid settled position
    insert_pt_positions(conn, 1, 10.0, T0, start_id=1)

    # 1 row with empty string settled_at
    conn.execute(
        """
        INSERT INTO price_target_book_positions
        (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at, settled_at, win, pnl)
        VALUES ('m_empty', 'e_empty', 'Title', 'No', 'band', 0.9, 0.0, 10, '2026-01-01', '2026-01-02', '', 1, 10.0)
        """
    )
    # 1 row with garbage settled_at
    conn.execute(
        """
        INSERT INTO price_target_book_positions
        (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at, settled_at, win, pnl)
        VALUES ('m_garbage', 'e_garbage', 'Title', 'No', 'band', 0.9, 0.0, 10, '2026-01-01', '2026-01-02', 'not-a-datetime', 1, 10.0)
        """
    )
    conn.close()

    decisions, report = run_strategy_manager_pass(db_path, now=T0)
    assert decisions[0].settled == 1
    assert "Ignored Positions**: 2 positions were ignored" in report


def test_naive_timestamp_treated_as_utc():
    naive_str = "2026-10-28T12:00:00"
    parsed = parse_timestamp(naive_str)
    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed == datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def test_malformed_json_config_stops_run_with_clear_error(tmp_path: Path):
    db_path = tmp_path / "test.db"
    config_path = tmp_path / "bad_config.json"
    config_path.write_text("{malformed: json", encoding="utf-8")

    code = runner.main([
        "--database", str(db_path),
        "--config", str(config_path),
    ])
    assert code != 0


def test_invalid_config_values_fallback_to_defaults(tmp_path: Path):
    db_path = tmp_path / "test.db"
    config_path = tmp_path / "invalid_vals_config.json"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    conn.close()

    # promote_min_settled is a bool (invalid), demote_min_settled is 0 (invalid), demote_window_days is negative
    config_path.write_text(
        json.dumps({
            "promote_min_settled": True,
            "demote_min_settled": 0,
            "demote_window_days": -10,
        }),
        encoding="utf-8",
    )

    decisions, report = run_strategy_manager_pass(db_path, config_path=config_path, now=T0)
    assert "Config key 'promote_min_settled' is invalid or missing" in report
    assert "Config key 'demote_min_settled' is invalid or missing" in report
    assert "Config key 'demote_window_days' is invalid or missing" in report


def test_missing_book_table_skipped(tmp_path: Path):
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.close()

    decisions, report = run_strategy_manager_pass(db_path, now=T0)
    assert decisions == []


def test_dry_run_no_database_prints_and_exits_nonzero(tmp_path: Path, capsys):
    db_path = tmp_path / "nonexistent.db"
    code = runner.main(["--database", str(db_path), "--dry-run"])
    assert code != 0
    captured = capsys.readouterr()
    assert "no database" in captured.err or "no database" in captured.out


def test_first_appearance_passes_promote_single_promote_journal_row(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)
    insert_pt_positions(conn, 100, 10.0, T0)
    conn.close()

    decisions, _ = run_strategy_manager_pass(db_path, now=T0)
    assert len(decisions) == 1
    assert decisions[0].new_status == "live"
    assert decisions[0].action == "promote"

    verify_conn = sqlite3.connect(db_path)
    cur = verify_conn.execute("SELECT action FROM strategy_journal")
    actions = [row[0] for row in cur.fetchall()]
    assert actions == ["promote"]
    verify_conn.close()


def test_strictly_later_than_boundaries(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # 1. Promote to live at T0 with winning positions older than 30 days
    t_win = T0 - timedelta(days=35)
    insert_pt_positions(conn, 100, 10.0, t_win, start_id=1)
    run_strategy_manager_pass(db_path, now=T0)

    # At T0 + 25h: test exact 30-day boundary for demotion
    now = T0 + timedelta(hours=25)
    cutoff_30d = now - timedelta(days=30)

    # 49 positions inside the 30-day window
    insert_pt_positions(conn, 49, -10.0, now - timedelta(days=1), start_id=101)
    # 1 position EXACTLY at cutoff_30d (strictly later than rule means this does NOT count)
    insert_pt_positions(conn, 1, -10.0, cutoff_30d, start_id=150)

    decisions, _ = run_strategy_manager_pass(db_path, now=now)
    assert decisions[0].new_status == "live"  # only 49 strictly inside window

    # Add 1 position 1 second later than cutoff_30d (making 50 strictly inside)
    insert_pt_positions(conn, 1, -10.0, cutoff_30d + timedelta(seconds=1), start_id=151)
    decisions, _ = run_strategy_manager_pass(db_path, now=now)
    assert decisions[0].new_status == "removed"  # now demoted

    # 2. Test exact since-removal boundary for re-promotion
    removal_time = now
    now_retest = removal_time + timedelta(hours=25)

    # 99 fresh positions strictly after removal_time
    insert_pt_positions(conn, 99, 10.0, removal_time + timedelta(hours=1), start_id=200)
    # 1 position EXACTLY at removal_time (strictly later than rule means this does NOT count)
    insert_pt_positions(conn, 1, 10.0, removal_time, start_id=300)

    decisions, _ = run_strategy_manager_pass(db_path, now=now_retest)
    assert decisions[0].new_status == "removed"  # only 99 counted

    # Add 1 position 1 second after removal_time (making 100 fresh positions)
    insert_pt_positions(conn, 1, 10.0, removal_time + timedelta(seconds=1), start_id=301)
    decisions, _ = run_strategy_manager_pass(db_path, now=now_retest)
    assert decisions[0].new_status == "live"
    conn.close()


def test_live_strategy_no_positions_flagged_in_report(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_price_target_table(conn)

    # 100 winning positions old (e.g. 40 days ago)
    t_old = T0 - timedelta(days=40)
    insert_pt_positions(conn, 100, 10.0, t_old, start_id=1)
    # Promote at t_old
    run_strategy_manager_pass(db_path, now=t_old)

    # Advance to T0 (40 days later, so 0 positions in the 30-day window)
    decisions, report = run_strategy_manager_pass(db_path, now=T0)
    assert decisions[0].new_status == "live"
    assert "The following live strategies have no positions settled in the last 30 days: price_target" in report
    conn.close()


def test_copy_book_positions_discovery(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    create_copy_table(conn)

    # Open position for wallet_alpha
    conn.execute(
        """
        INSERT INTO copy_book_positions
        (wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome, their_price,
         their_timestamp, fill_price, fee, shares, opened_at, delay_seconds, settled_at, win, pnl)
        VALUES ('wallet_alpha', '0x123', '0xtx1', 'tok1', 'c1', 'Title', 'Yes', 0.5, 1000, 0.5, 0.0, 10, '2026-01-01', 1.0, NULL, NULL, NULL)
        """
    )
    conn.close()

    decisions, _ = run_strategy_manager_pass(db_path, now=T0)
    assert len(decisions) == 1
    assert decisions[0].strategy_key == "copy:wallet_alpha"
    assert decisions[0].new_status == "candidate"
