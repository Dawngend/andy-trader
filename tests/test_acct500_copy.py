from __future__ import annotations

import pytest

from andy_trader import acct500_copy as acct
from andy_trader.copy_book import SHARES, initialize_copy_book
from andy_trader.store import connect

AFTER = "2026-11-17T16:00:00+00:00"
LATER = "2026-11-17T17:00:00+00:00"
BEFORE = "2026-11-17T10:00:00+00:00"


def row(token="t1", fill=0.5, fee=1.0, opened=AFTER, settled=None, pnl=None, wallet="dromedaryy"):
    return {"wallet_name": wallet, "tx_hash": token, "token_id": token, "title": "T", "outcome": "Yes",
            "fill_price": fill, "fee": fee, "opened_at": opened, "settled_at": settled, "pnl": pnl}


def book(bid):
    return lambda token: {"bids": [{"price": str(bid), "size": "100"}, {"price": "0.01", "size": "5"}], "asks": []}


def test_each_position_costs_25_dollars():
    account = acct.build_account([row(fill=0.5, fee=1.0)], book(0.5))
    shares = 25 / 0.5
    fee = 1.0 * shares / SHARES
    assert account.cash == pytest.approx(500 - 25 - fee)
    assert account.positions[0]["shares"] == pytest.approx(50)


def test_open_position_is_valued_at_best_bid():
    account = acct.build_account([row(fill=0.5)], book(0.6))
    assert account.open_value == pytest.approx(0.6 * 50)
    assert account.equity == pytest.approx(account.cash + 30)


def test_failed_book_read_holds_position_at_cost_with_a_note():
    def broken(token):
        raise OSError("down")

    account = acct.build_account([row(fill=0.5, fee=1.0)], broken)
    fee = 1.0 * 50 / SHARES
    assert account.open_value == pytest.approx(25 + fee)
    assert "book read failed" in account.notes[0]


def test_empty_book_holds_position_at_cost_with_a_note():
    account = acct.build_account([row(fill=0.5)], lambda token: {"bids": [], "asks": []})
    assert account.open_value == pytest.approx(25 + 1.0 * 50 / SHARES)
    assert "no bid" in account.notes[0]


def test_settled_win_and_loss_change_equity_by_scaled_pnl():
    win = row("w", fill=0.5, settled=LATER, pnl=50.0)
    loss = row("l", fill=0.5, settled=LATER, pnl=-52.0)
    account = acct.build_account([win, loss], book(0.5))
    scale = 50 / SHARES
    assert account.equity == pytest.approx(500 + (50.0 - 52.0) * scale)
    assert account.settled_count == 2 and account.open_count == 0


def test_cash_runs_out_and_later_positions_are_skipped():
    rows = [row(f"t{i}", fill=0.5, opened=f"2026-11-17T16:{i:02d}:00+00:00") for i in range(30)]
    account = acct.build_account(rows, book(0.5))
    assert len(account.positions) == 19
    assert account.cash < 26


def test_money_from_settled_positions_can_be_used_again():
    # 500 dollars buys 19 positions of 25.50 with no settlement; a settled winner frees enough for a 20th.
    first = row("a", settled="2026-11-17T16:10:00+00:00", pnl=10.0, opened="2026-11-17T16:00:00+00:00")
    rows = [first] + [row(f"t{i}", opened=f"2026-11-17T16:{20 + i:02d}:00+00:00") for i in range(20)]
    account = acct.build_account(rows, book(0.5))
    assert len(account.positions) == 20


def test_same_rows_always_give_the_same_equity():
    rows = [row("a"), row("b", settled=LATER, pnl=5.0)]
    assert acct.build_account(rows, book(0.55)).equity == acct.build_account(rows, book(0.55)).equity


def test_old_positions_and_paused_wallets_are_left_out(tmp_path):
    with connect(tmp_path / "c.db") as connection:
        initialize_copy_book(connection)
        for token, opened, wallet in (("old", BEFORE, "dromedaryy"), ("new", AFTER, "dromedaryy"),
                                      ("paused", AFTER, "sharky6999")):
            connection.execute(
                "INSERT INTO copy_book_positions (wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome, "
                "their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds) "
                "VALUES (?, 'w', ?, ?, 'c', 't', 'Yes', 0.5, 1, 0.5, 1.0, 100, ?, 5)",
                (wallet, token, token, opened),
            )
        tokens = [r["token_id"] for r in acct.eligible_positions(connection)]
    assert tokens == ["new"]


def test_snapshot_is_recorded_and_reported(tmp_path):
    with connect(tmp_path / "c.db") as connection:
        account = acct.build_account([row(fill=0.5)], book(0.6))
        acct.record_snapshot(connection, account)
        report = acct.format_account(connection)
    assert "acct500_copy" in report and f"{account.equity:.2f}" in report
