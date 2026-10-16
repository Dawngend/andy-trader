"""Pre-order risk checks and their paper-trade report."""

import sqlite3
import sys

import pytest

from andy_trader.order_check import catch_report, check_order, main, risk_score
from andy_trader.portfolio import execute_paper_trade, initialize_portfolio, run_paper_cycle


NOW = "2026-10-16T12:00:00+00:00"


def connection():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_portfolio(db)
    return db


def checked(db, size=0.8, volatility=2.0, age=60, stops=3, now=NOW):
    return check_order(
        db, predictor="p", instrument="BTC-USD", side="long", now_iso=now,
        volatility=volatility, size_fraction=size, data_age_minutes=age,
        recent_stopouts=stops,
    )


def test_score_is_bounded_and_rises_with_each_input():
    low = risk_score(0, 0, 0, 0)
    assert low == 0
    assert risk_score(100, 10, 1000, 100) == 1
    for args in [(1, 0, 0, 0), (0, 0.5, 0, 0), (0, 0, 30, 0), (0, 0, 0, 1)]:
        assert 0 < risk_score(*args) < 1


def test_warmup_threshold_halving_and_append_only_history():
    db = connection()
    for _ in range(29):
        assert checked(db, size=0.1, volatility=0, age=0, stops=0) == 0.1
    assert checked(db, size=1) == 1  # 30th check still warms up
    assert checked(db, size=1) == 0.5
    rows = db.execute("SELECT * FROM order_checks ORDER BY id").fetchall()
    assert len(rows) == 31
    assert [row["flagged"] for row in rows] == [0] * 30 + [1]
    assert rows[-1]["size_fraction_in"] == 1
    assert rows[-1]["size_fraction_out"] == 0.5
    assert rows[-1]["reasons"]
    assert rows[0]["size_fraction_out"] == 0.1


def test_only_most_recent_200_scores_set_the_threshold():
    db = connection()
    for _ in range(200):
        checked(db, size=0.1, volatility=0, age=0, stops=0)
    for _ in range(200):
        checked(db, size=1)
    assert checked(db, size=1) == 1  # equal scores do not exceed the cutoff


def test_cycle_checks_entries_but_leaves_exits_untouched():
    db = connection()
    for _ in range(30):
        checked(db, size=0.1, volatility=0, age=0, stops=0)
    opened = run_paper_cycle(
        db, predictor="p", instrument="BTC-USD", probability_up=0.9,
        price=100, now_iso=NOW,
    )
    assert opened.trade is not None
    assert db.execute("SELECT COUNT(*) FROM order_checks").fetchone()[0] == 31
    closed = run_paper_cycle(
        db, predictor="p", instrument="BTC-USD", probability_up=0.5,
        price=100, now_iso="2026-10-16T13:00:00+00:00",
    )
    assert closed.trade is not None and closed.trade.side == "flat"
    assert closed.trade.qty == pytest.approx(opened.trade.qty)
    assert db.execute("SELECT COUNT(*) FROM order_checks").fetchone()[0] == 31


def test_report_matches_flagged_entry_to_its_bracket_stop():
    db = connection()
    for _ in range(30):
        checked(db, size=0.1, volatility=0, age=0, stops=0)
    checked(db, size=1)
    execute_paper_trade(db, predictor="p", instrument="BTC-USD", target_side="long",
                        price=100, now_iso=NOW, reason="open")
    execute_paper_trade(db, predictor="p", instrument="BTC-USD", target_side="flat",
                        price=98, now_iso="2026-10-16T13:00:00+00:00", reason="bracket stop")
    report = catch_report(db)
    assert report["bracket_stops"] == 1
    assert report["flagged_bracket_stops"] == 1
    assert report["flagged_share"] == pytest.approx(1 / 31)


def test_report_cli_handles_a_new_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["order_check", "--database", str(tmp_path / "paper.db")])
    main()
    assert "Bracket stops: 0" in capsys.readouterr().out
