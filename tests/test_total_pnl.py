"""Tests for Total Paper P&L Report across all books."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from andy_trader.store import connect
from andy_trader.total_pnl import (
    collect_all_groups,
    collect_crypto_portfolios,
    collect_complete_set,
    collect_polymarket_books,
    collect_total,
    compute_return_pct,
    compute_pnl_changes,
    create_snapshot_table,
    format_pnl_change_line,
    format_report,
    format_table,
    record_snapshot,
)
import run_total_pnl as runner

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def setup_test_database(conn):
    """Set up a database with fake rows across groups, including a missing group

    and a portfolio pair with no equity row yet.
    """
    # 1. Crypto paper portfolios: paper_portfolio_state and paper_equity_curve
    conn.execute(
        """
        CREATE TABLE paper_portfolio_state (
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            starting_cash REAL NOT NULL,
            cash REAL NOT NULL,
            position_qty REAL NOT NULL,
            avg_entry_price REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (predictor, instrument)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE paper_equity_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            cash REAL NOT NULL,
            position_qty REAL NOT NULL,
            mark_price REAL NOT NULL,
            position_value REAL NOT NULL,
            equity REAL NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """
    )

    # probe has two instruments: BTC-USD (has equity curve) and ETH-USD (no equity curve yet)
    conn.execute(
        "INSERT INTO paper_portfolio_state VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("probe", "BTC-USD", 1000.0, 950.0, 0.05, 1000.0, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO paper_portfolio_state VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("probe", "ETH-USD", 1000.0, 1000.0, 0.0, None, NOW.isoformat()),
    )
    # latest equity for probe BTC-USD: 1100.0 (+100 gain)
    conn.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("probe", "BTC-USD", 950.0, 0.05, 3000.0, 150.0, 1100.0, (NOW - timedelta(minutes=10)).isoformat()),
    )

    # trend40 has one instrument: BTC-USD (has equity curve: 450.0, -50 loss)
    conn.execute(
        "INSERT INTO paper_portfolio_state VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("trend40", "BTC-USD", 500.0, 500.0, 0.0, None, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("trend40", "BTC-USD", 450.0, 0.0, 0.0, 0.0, 450.0, (NOW - timedelta(minutes=5)).isoformat()),
    )

    # 2. Complete set: complete_set_paper_account and complete_set_paper_trades
    conn.execute(
        """
        CREATE TABLE complete_set_paper_account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            starting_cash REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE complete_set_paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            total_debit REAL NOT NULL,
            settled_at TEXT,
            pnl REAL
        )
        """
    )
    # Starting cash 200, current cash 180 (30 held in open trade, 20 spent on settled trade which paid 30)
    conn.execute(
        "INSERT INTO complete_set_paper_account VALUES (1, 180.0, 200.0, ?)",
        (NOW.isoformat(),),
    )
    # 1 open trade holding 30.0 cash
    conn.execute(
        "INSERT INTO complete_set_paper_trades (round_id, opened_at, total_debit, settled_at, pnl) "
        "VALUES ('r_open', ?, 30.0, NULL, NULL)",
        ((NOW - timedelta(minutes=15)).isoformat(),),
    )
    # 1 settled trade (+10.0 pnl)
    conn.execute(
        "INSERT INTO complete_set_paper_trades (round_id, opened_at, total_debit, settled_at, pnl) "
        "VALUES ('r_settled', ?, 20.0, ?, 10.0)",
        ((NOW - timedelta(hours=1)).isoformat(), (NOW - timedelta(minutes=30)).isoformat()),
    )

    # 3. Polymarket paper books:
    # bond_book exists: 1 settled (+15.0 pnl), 1 open (fill_price=0.90, shares=10, fee=1.0 -> open_cost 10.0)
    conn.execute(
        """
        CREATE TABLE bond_book_positions (
            market_id TEXT NOT NULL, event_id TEXT NOT NULL, title TEXT NOT NULL,
            side TEXT NOT NULL, entry_band TEXT NOT NULL, fill_price REAL NOT NULL,
            fee REAL NOT NULL, shares REAL NOT NULL, opened_at TEXT NOT NULL,
            ends_at TEXT NOT NULL, settled_at TEXT, win INTEGER, pnl REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO bond_book_positions VALUES ('m1', 'e1', 'T1', 'Yes', 'b', 0.90, 1.0, 10, ?, ?, ?, 1, 15.0)",
        (NOW.isoformat(), NOW.isoformat(), (NOW - timedelta(hours=2)).isoformat()),
    )
    conn.execute(
        "INSERT INTO bond_book_positions VALUES ('m2', 'e2', 'T2', 'No', 'b', 0.90, 1.0, 10, ?, ?, NULL, NULL, NULL)",
        ((NOW - timedelta(minutes=20)).isoformat(), NOW.isoformat()),
    )

    # copy_book exists: 1 settled (-5.0 pnl), 0 open
    conn.execute(
        """
        CREATE TABLE copy_book_positions (
            wallet_name TEXT NOT NULL, wallet TEXT NOT NULL, tx_hash TEXT NOT NULL,
            token_id TEXT NOT NULL, condition_id TEXT NOT NULL, title TEXT NOT NULL,
            outcome TEXT NOT NULL, their_price REAL NOT NULL, their_timestamp INTEGER NOT NULL,
            fill_price REAL NOT NULL, fee REAL NOT NULL, shares REAL NOT NULL,
            opened_at TEXT NOT NULL, delay_seconds REAL NOT NULL, settled_at TEXT, win INTEGER, pnl REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO copy_book_positions VALUES ('w1', '0x1', '0xtx', 'tok', 'c1', 'T3', 'Yes', 0.8, 100, 0.8, 0.5, 10, ?, 2.0, ?, 0, -5.0)",
        (NOW.isoformat(), (NOW - timedelta(hours=1)).isoformat()),
    )

    # Note: price_target_book_positions is intentionally omitted (missing group)
    conn.commit()


def test_group_sums_and_total_row(tmp_path):
    db_path = tmp_path / "test_total_pnl.db"
    with connect(db_path) as conn:
        setup_test_database(conn)
        groups = collect_all_groups(conn)

    by_name = {g.name: g for g in groups}

    # Verify probe: BTC-USD marked at 1100.0, ETH-USD counted at starting cash (1000.0) -> value 2100.0
    probe = by_name["probe"]
    assert probe.starting_cash == 2000.0
    assert probe.value == 2100.0
    assert probe.pnl == 100.0
    assert probe.open_cost == 0.0
    assert "no equity row yet" in probe.note
    assert "ETH-USD" in probe.note
    assert probe.newest_time is not None

    # Verify trend40: BTC-USD marked at 450.0 -> value 450.0, starting 500.0 -> pnl -50.0
    trend40 = by_name["trend40"]
    assert trend40.starting_cash == 500.0
    assert trend40.value == 450.0
    assert trend40.pnl == -50.0
    assert trend40.open_cost == 0.0
    assert trend40.note == ""

    # Verify complete_set: cash 180 + open_cost 30 -> value 210.0, starting 200.0 -> pnl +10.0
    cs = by_name["complete_set"]
    assert cs.starting_cash == 200.0
    assert cs.value == 210.0
    assert cs.pnl == 10.0
    assert cs.open_cost == 30.0
    assert cs.settled_count == 1
    assert cs.open_count == 1

    # Verify bond_book: starting_cash and value are None; settled pnl = 15.0, open_cost = 0.90*10 + 1.0 = 10.0
    bb = by_name["bond_book"]
    assert bb.starting_cash is None
    assert bb.value is None
    assert bb.pnl == 15.0
    assert bb.open_cost == 10.0
    assert bb.settled_count == 1
    assert bb.open_count == 1

    # Verify copy_book: settled pnl = -5.0, open_cost = 0.0
    cb = by_name["copy_book"]
    assert cb.starting_cash is None
    assert cb.value is None
    assert cb.pnl == -5.0
    assert cb.open_cost == 0.0
    assert cb.settled_count == 1
    assert cb.open_count == 0

    # Verify missing price_target_book: zeros and "no table yet"
    pt = by_name["price_target_book"]
    assert pt.starting_cash is None
    assert pt.value is None
    assert pt.pnl == 0.0
    assert pt.open_cost == 0.0
    assert pt.settled_count == 0
    assert pt.open_count == 0
    assert pt.note == "no table yet"

    # Verify TOTAL row:
    # Groups with starting cash: probe (2000), trend40 (500), complete_set (200) -> 2700.0
    # Total pnl across ALL groups: 100 (probe) - 50 (trend40) + 10 (cs) + 15 (bb) - 5 (cb) + 0 (pt) = 70.0
    # Total value across groups with value: 2100 + 450 + 210 = 2760.0
    # Total open cost: 30 (cs) + 10 (bb) = 40.0
    total = by_name["TOTAL"]
    assert total.starting_cash == 2700.0
    assert total.value == 2760.0
    assert total.pnl == 70.0
    assert total.open_cost == 40.0
    assert total.settled_count == 3
    assert total.open_count == 2


def test_percent_return_uses_only_groups_with_starting_cash(tmp_path):
    db_path = tmp_path / "test_ret_pct.db"
    with connect(db_path) as conn:
        setup_test_database(conn)
        groups = collect_all_groups(conn)

    # Covered groups: probe (pnl 100, start 2000), trend40 (pnl -50, start 500), complete_set (pnl 10, start 200)
    # Sum of pnl for covered groups = 100 - 50 + 10 = 60.0
    # Total starting cash = 2000 + 500 + 200 = 2700.0
    # Expected return = (60.0 / 2700.0) * 100 = 2.2222...%
    ret_pct = compute_return_pct(groups)
    assert ret_pct == pytest.approx(60.0 / 2700.0 * 100)

    total = next(g for g in groups if g.name == "TOTAL")
    assert "+2.22%" in total.note
    assert "probe" in total.note
    assert "trend40" in total.note
    assert "complete_set" in total.note
    # Polymarket books should not be listed as covered by starting cash
    assert "bond_book" not in total.note


def test_three_hour_change_from_two_stored_snapshots(tmp_path):
    db_path = tmp_path / "test_history.db"
    with connect(db_path) as conn:
        setup_test_database(conn)
        create_snapshot_table(conn)

        # Snapshot 1 recorded 3 hours ago: total pnl was 50.0
        t1 = (NOW - timedelta(hours=3)).isoformat()
        conn.execute(
            """
            INSERT INTO total_pnl_snapshots
                (recorded_at, group_name, starting_cash, value, pnl, open_cost)
            VALUES (?, 'TOTAL', 2700.0, 2750.0, 50.0, 40.0)
            """,
            (t1,),
        )

        # Snapshot 2 recorded now: total pnl is 70.0
        t2 = NOW.isoformat()
        conn.execute(
            """
            INSERT INTO total_pnl_snapshots
                (recorded_at, group_name, starting_cash, value, pnl, open_cost)
            VALUES (?, 'TOTAL', 2700.0, 2760.0, 70.0, 40.0)
            """,
            (t2,),
        )
        conn.commit()

        changes = compute_pnl_changes(conn, current_pnl=70.0, current_time=NOW)

    # 3-hour change: 70.0 - 50.0 = 20.0
    assert changes["3h"] == pytest.approx(20.0)
    # The 3-hour-old snapshot is also at or before the 30m cutoff
    assert changes["30m"] == pytest.approx(20.0)
    # 24 hours has no snapshot at or before its cutoff
    assert changes["24h"] is None

    line = format_pnl_change_line(changes)
    assert "last 30 minutes: +$20.00" in line
    assert "last 3 hours: +$20.00" in line
    assert "last 24 hours: not enough history" in line


def test_change_uses_nearest_snapshot_at_or_before_cutoff(tmp_path):
    db_path = tmp_path / "test_lookbacks.db"
    with connect(db_path) as conn:
        create_snapshot_table(conn)

        # 1. Snapshot only 15 minutes ago (newer than 30m, 3h, 24h cutoffs)
        t_15m = (NOW - timedelta(minutes=15)).isoformat()
        conn.execute(
            "INSERT INTO total_pnl_snapshots VALUES (?, 'TOTAL', 100.0, 110.0, 10.0, 0.0)",
            (t_15m,),
        )
        conn.commit()

        # All windows should report not enough history because 15m is newer than all cutoffs
        changes = compute_pnl_changes(conn, current_pnl=100.0, current_time=NOW)
        assert changes["30m"] is None
        assert changes["3h"] is None
        assert changes["24h"] is None

        # 2. Add snapshot 45 minutes ago (pnl = 30.0)
        t_45m = (NOW - timedelta(minutes=45)).isoformat()
        conn.execute(
            "INSERT INTO total_pnl_snapshots VALUES (?, 'TOTAL', 100.0, 130.0, 30.0, 0.0)",
            (t_45m,),
        )
        # 3. Add snapshot 4 hours ago (pnl = 20.0)
        t_4h = (NOW - timedelta(hours=4)).isoformat()
        conn.execute(
            "INSERT INTO total_pnl_snapshots VALUES (?, 'TOTAL', 100.0, 120.0, 20.0, 0.0)",
            (t_4h,),
        )
        # 4. Add snapshot 30 hours ago (pnl = 10.0)
        t_30h = (NOW - timedelta(hours=30)).isoformat()
        conn.execute(
            "INSERT INTO total_pnl_snapshots VALUES (?, 'TOTAL', 100.0, 110.0, 10.0, 0.0)",
            (t_30h,),
        )
        conn.commit()

        changes = compute_pnl_changes(conn, current_pnl=100.0, current_time=NOW)
        # 30m uses 45m snapshot (nearest at or before 30m cutoff): 100 - 30 = 70
        assert changes["30m"] == pytest.approx(70.0)
        # 3h uses 4h snapshot (nearest at or before 3h cutoff): 100 - 20 = 80
        assert changes["3h"] == pytest.approx(80.0)
        # 24h uses 30h snapshot (nearest at or before 24h cutoff): 100 - 10 = 90
        assert changes["24h"] == pytest.approx(90.0)


def test_missing_paper_equity_curve_shows_no_table_yet(tmp_path):
    db_path = tmp_path / "test_missing_equity.db"
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE paper_portfolio_state (
                predictor TEXT NOT NULL,
                instrument TEXT NOT NULL,
                starting_cash REAL NOT NULL,
                cash REAL NOT NULL,
                position_qty REAL NOT NULL,
                avg_entry_price REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (predictor, instrument)
            )
            """
        )
        conn.execute(
            "INSERT INTO paper_portfolio_state VALUES ('probe', 'BTC-USD', 1000.0, 1000.0, 0.0, NULL, ?)",
            (NOW.isoformat(),),
        )
        conn.commit()

        # paper_equity_curve is not created
        results = collect_crypto_portfolios(conn)

    assert len(results) == 1
    probe = results[0]
    assert probe.name == "probe"
    assert probe.starting_cash == 1000.0
    assert probe.value == 1000.0
    assert probe.pnl == 0.0
    assert "no table yet" in probe.note
    assert "paper_equity_curve" in probe.note


def test_second_run_appends_without_touching_first(tmp_path):
    db_path = tmp_path / "test_append.db"
    with connect(db_path) as conn:
        setup_test_database(conn)
        groups = collect_all_groups(conn)

        t1 = (NOW - timedelta(minutes=5)).isoformat()
        record_snapshot(conn, groups, recorded_at=t1)

        cur = conn.execute("SELECT COUNT(*) FROM total_pnl_snapshots")
        count_first = cur.fetchone()[0]
        assert count_first > 0

        # Run 2 at NOW
        t2 = NOW.isoformat()
        record_snapshot(conn, groups, recorded_at=t2)

        cur = conn.execute("SELECT COUNT(*) FROM total_pnl_snapshots")
        count_second = cur.fetchone()[0]
        assert count_second == 2 * count_first

        # Ensure first snapshot rows are intact
        cur = conn.execute("SELECT COUNT(*) FROM total_pnl_snapshots WHERE recorded_at = ?", (t1,))
        assert cur.fetchone()[0] == count_first

        cur = conn.execute("SELECT COUNT(*) FROM total_pnl_snapshots WHERE recorded_at = ?", (t2,))
        assert cur.fetchone()[0] == count_first


def test_missing_all_tables_returns_zeros_and_no_table_yet(tmp_path):
    db_path = tmp_path / "empty.db"
    with connect(db_path) as conn:
        groups = collect_all_groups(conn)

    by_name = {g.name: g for g in groups}
    assert "crypto_portfolios" in by_name
    assert by_name["crypto_portfolios"].note == "no table yet"
    assert by_name["crypto_portfolios"].pnl == 0.0

    assert "complete_set" in by_name
    assert by_name["complete_set"].note == "no table yet"

    assert "bond_book" in by_name
    assert by_name["bond_book"].note == "no table yet"

    total = by_name["TOTAL"]
    assert total.pnl == 0.0


def test_complete_set_missing_trades_table_shows_zeros_and_no_table_yet(tmp_path):
    db_path = tmp_path / "missing_trades.db"
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE complete_set_paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL,
                starting_cash REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO complete_set_paper_account VALUES (1, 150.0, 200.0, ?)",
            (NOW.isoformat(),),
        )
        conn.commit()

        res = collect_complete_set(conn)

    assert res.name == "complete_set"
    assert res.starting_cash == 0.0
    assert res.value == 0.0
    assert res.pnl == 0.0
    assert res.open_cost == 0.0
    assert res.settled_count == 0
    assert res.open_count == 0
    assert res.note == "no table yet"


def test_runner_main_writes_report_and_prints(tmp_path, capsys):
    db_path = tmp_path / "runner_test.db"
    with connect(db_path) as conn:
        setup_test_database(conn)

    out_file = tmp_path / "research" / "total_pnl.md"
    ret = runner.main(["--database", str(db_path), "--output", str(out_file)])
    assert ret == 0

    captured = capsys.readouterr().out
    assert "| Group | Starting Cash | Value | P&L |" in captured
    assert "TOTAL" in captured

    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert content.startswith("# Total Paper P&L Report")
    assert "Change in total pnl:" in content
    assert "Recent Total P&L History (last 24 snapshots)" in content
    assert "TOTAL" in content
