from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from andy_trader import bond_book as bond
from andy_trader import copy_book as copy
from andy_trader import price_target_book as pt
from andy_trader.store import connect
from andy_trader.strategy_board import format_board
import run_strategy_board as runner

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def _market(mid: str = "m1", question: str = "Will Bitcoin reach 150,000?", **kwargs):
    return {
        "id": mid,
        "question": question,
        "active": True,
        "closed": False,
        "endDate": (NOW + timedelta(days=1)).isoformat(),
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{mid}y", "{mid}n"]',
        **kwargs,
    }


def test_board_with_two_bond_slices_and_price_target_table(tmp_path):
    db_path = tmp_path / "board_test.db"
    with connect(db_path) as conn:
        bond.initialize_bond_book(conn)
        pt.initialize_price_target_book(conn)

        reach_q = "Will Bitcoin reach 150,000?"
        updown_q = "Bitcoin Up or Down on October 5?"
        above_q = "Bitcoin above 100,000?"

        # Bond book slice 1: price_target_no (2 settled: 1 win, 1 loss)
        bond.open_position(conn, "e1", _market("m1", question=reach_q), "No", 0.90, 0.63, NOW)
        bond.open_position(conn, "e2", _market("m2", question=reach_q), "No", 0.90, 0.63, NOW)
        # Bond book slice 2: up_down (1 settled: 1 win)
        bond.open_position(conn, "e3", _market("m3", question=updown_q), "No", 0.90, 0.63, NOW)
        # Bond book slice 3: above_below (1 open, 0 settled)
        bond.open_position(conn, "e4", _market("m4", question=above_q), "Yes", 0.90, 0.63, NOW)

        # Settle m1 (win), m2 (loss), m3 (win)
        with conn:
            conn.execute(
                "UPDATE bond_book_positions SET win=1, pnl=9.37, settled_at=? WHERE market_id='m1'",
                (NOW.isoformat(),),
            )
            conn.execute(
                "UPDATE bond_book_positions SET win=0, pnl=-90.63, settled_at=? WHERE market_id='m2'",
                (NOW.isoformat(),),
            )
            conn.execute(
                "UPDATE bond_book_positions SET win=1, pnl=9.37, settled_at=? WHERE market_id='m3'",
                (NOW.isoformat(),),
            )

        # Price target book: 1 open position
        pt.open_position(conn, "e5", _market("m5", question=reach_q), "No", 0.90, 0.63, NOW)

        board = format_board(conn)

    # Check header columns exist
    assert "Strategy | Settled | Open | Wins | Win rate | Average entry price" in board
    assert "Average pnl per position after fees" in board
    assert "95 percent bootstrap interval of the average pnl per position | Status" in board

    lines = [line.strip() for line in board.splitlines() if line.strip() and not line.startswith("---")]
    rows_by_strat = {}
    for line in lines[1:]:
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            rows_by_strat[parts[0]] = parts

    # 4 strategies expected: bond_book, price_target_no, up_down, price_target_no_book
    assert set(rows_by_strat.keys()) == {"bond_book", "price_target_no", "up_down", "price_target_no_book"}
    assert "above_below" not in rows_by_strat

    # bond_book overall: 3 settled, 1 open, 2 wins, collecting
    bb = rows_by_strat["bond_book"]
    assert bb[1] == "3"  # settled
    assert bb[2] == "1"  # open
    assert bb[3] == "2"  # wins
    assert bb[4] == "66.67%"  # win rate
    assert bb[5] == "0.9000"  # avg entry price
    assert bb[8] == "collecting"

    # price_target_no slice: 2 settled, 0 open, 1 win, collecting
    pt_slice = rows_by_strat["price_target_no"]
    assert pt_slice[1] == "2"
    assert pt_slice[2] == "0"
    assert pt_slice[3] == "1"
    assert pt_slice[4] == "50.00%"
    assert pt_slice[8] == "collecting"

    # up_down slice: 1 settled, 0 open, 1 win, collecting
    ud_slice = rows_by_strat["up_down"]
    assert ud_slice[1] == "1"
    assert ud_slice[2] == "0"
    assert ud_slice[3] == "1"
    assert ud_slice[4] == "100.00%"
    assert ud_slice[8] == "collecting"

    # price_target_no_book: 0 settled, 1 open, 0 wins, collecting
    pt_book = rows_by_strat["price_target_no_book"]
    assert pt_book[1] == "0"
    assert pt_book[2] == "1"
    assert pt_book[3] == "0"
    assert pt_book[4] == "n/a"
    assert pt_book[8] == "collecting"


def test_board_without_price_target_table(tmp_path):
    db_path = tmp_path / "no_pt.db"
    with connect(db_path) as conn:
        bond.initialize_bond_book(conn)
        reach_q = "Will Bitcoin reach 150,000?"
        bond.open_position(conn, "e1", _market("m1", question=reach_q), "No", 0.90, 0.63, NOW)
        with conn:
            conn.execute(
                "UPDATE bond_book_positions SET win=1, pnl=9.37, settled_at=? WHERE market_id='m1'",
                (NOW.isoformat(),),
            )
        board = format_board(conn)

    assert "bond_book" in board
    assert "price_target_no" in board
    assert "price_target_no_book" not in board


def test_strategy_review_on_big_loss_and_negative_status(tmp_path):
    db_path = tmp_path / "review_test.db"
    with connect(db_path) as conn:
        bond.initialize_bond_book(conn)

        reach_q = "Will Bitcoin reach 150,000?"
        # Insert 5 small wins and 1 big loss (> 10x average win)
        for i in range(5):
            mid = f"win_{i}"
            bond.open_position(conn, f"e_{i}", _market(mid, question=reach_q), "No", 0.90, 0.63, NOW)
            with conn:
                conn.execute(
                    "UPDATE bond_book_positions SET win=1, pnl=2.0, settled_at=? WHERE market_id=?",
                    (NOW.isoformat(), mid),
                )

        # Big loss: -35.0 (magnitude 35.0 > 10 * 2.0 = 20.0)
        bond.open_position(conn, "e_loss", _market("loss_1", question=reach_q), "No", 0.90, 0.63, NOW)
        with conn:
            conn.execute(
                "UPDATE bond_book_positions SET win=0, pnl=-35.0, settled_at=? WHERE market_id='loss_1'",
                (NOW.isoformat(),),
            )

        board = format_board(conn)

    assert "review: price_target_no" in board
    assert "review: bond_book" in board


def test_strategy_review_when_negative_verdict_above_100_settled(tmp_path):
    db_path = tmp_path / "negative_test.db"
    with connect(db_path) as conn:
        pt.initialize_price_target_book(conn)
        reach_q = "Will Bitcoin reach 150,000?"
        for i in range(100):
            mid = f"pt_{i}"
            pt.open_position(conn, str(i), _market(mid, question=reach_q), "No", 0.90, 0.63, NOW)
            with conn:
                conn.execute(
                    "UPDATE price_target_book_positions SET win=0, pnl=-10.0, settled_at=? WHERE market_id=?",
                    (NOW.isoformat(), mid),
                )
        board = format_board(conn)

    assert "negative" in board
    assert "review: price_target_no_book" in board


def test_runner_writes_output_and_prints(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "runner_test.db"
    with connect(db_path) as conn:
        bond.initialize_bond_book(conn)
        reach_q = "Will Bitcoin reach 150,000?"
        bond.open_position(conn, "e1", _market("m1", question=reach_q), "No", 0.90, 0.63, NOW)
        with conn:
            conn.execute(
                "UPDATE bond_book_positions SET win=1, pnl=9.37, settled_at=? WHERE market_id='m1'",
                (NOW.isoformat(),),
            )

    output_file = tmp_path / "research" / "strategy_board.md"
    ret = runner.main(["--database", str(db_path), "--output", str(output_file)])
    assert ret == 0

    captured = capsys.readouterr().out
    assert "Strategy | Settled | Open | Wins" in captured
    assert "bond_book" in captured

    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    assert content.startswith("# Weekly Strategy Board")
    assert "Date: " in content
    assert "bond_book" in content

    # Also test default output path logic
    default_target = tmp_path / "default_target.md"
    monkeypatch.setattr(runner, "DEFAULT_OUTPUT_PATH", default_target)
    ret_default = runner.main(["--database", str(db_path)])
    assert ret_default == 0
    assert default_target.exists()


def test_board_with_copy_book_positions(tmp_path):
    db_path = tmp_path / "copy_board_test.db"
    with connect(db_path) as conn:
        copy.initialize_copy_book(conn)

        # sharky6999: 1 settled (win) and 1 open
        with conn:
            conn.execute("""
                INSERT INTO copy_book_positions (
                    wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                    their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds,
                    settled_at, win, pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "sharky6999", copy.WALLETS["sharky6999"], "0xtx1", "tok1", "c1", "Title 1", "Yes",
                0.90, 1000, 0.90, 0.63, 100, NOW.isoformat(), 5.0,
                NOW.isoformat(), 1, 9.37,
            ))
            conn.execute("""
                INSERT INTO copy_book_positions (
                    wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                    their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "sharky6999", copy.WALLETS["sharky6999"], "0xtx2", "tok2", "c2", "Title 2", "No",
                0.80, 1010, 0.85, 0.60, 100, NOW.isoformat(), 3.0,
            ))

        board = format_board(conn)

    lines = [line.strip() for line in board.splitlines() if line.strip() and not line.startswith("---")]
    rows_by_strat = {}
    for line in lines[1:]:
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            rows_by_strat[parts[0]] = parts

    assert "copy_sharky6999" in rows_by_strat
    assert "copy_dromedaryy" in rows_by_strat

    sharky = rows_by_strat["copy_sharky6999"]
    assert sharky[1] == "1"  # settled
    assert sharky[2] == "1"  # open
    assert sharky[3] == "1"  # wins
    assert sharky[4] == "100.00%"
    assert sharky[5] == "0.9000"
    assert sharky[8] == "collecting"

    dromedaryy = rows_by_strat["copy_dromedaryy"]
    assert dromedaryy[1] == "0"
    assert dromedaryy[2] == "0"
    assert dromedaryy[8] == "collecting"
