import sqlite3

import pytest

from andy_trader.llm_ledger import initialize_ledger, record_decision
from andy_trader.rank_bias import boundary_ratio, main, selection_by_rank


def test_selection_shares_and_boundary_ratio():
    with sqlite3.connect(":memory:") as connection:
        initialize_ledger(connection)
        for arm, position, convictions in (
            ("ranked", 3, 8), ("ranked", 4, 4),
            ("shuffled", 3, 6), ("shuffled", 4, 5),
        ):
            for index in range(10):
                record_decision(connection, list_arm=arm, list_position=position,
                                status="ok", p_up=0.55 if index < convictions else 0.5)
        record_decision(connection, list_arm="ranked", list_position=3, status="replay", p_up=1)
        record_decision(connection, list_arm="shuffled", list_position=4, status="ablation", p_up=1)
        assert selection_by_rank(connection) == [
            ("ranked", 3, 10, 0.8), ("ranked", 4, 10, 0.4),
            ("shuffled", 3, 10, 0.6), ("shuffled", 4, 10, 0.5),
        ]
        assert boundary_ratio(connection, "ranked") == pytest.approx(2)
        assert boundary_ratio(connection, "shuffled") == pytest.approx(1.2)
        assert boundary_ratio(connection, "ranked", cut=2) is None


def test_boundary_ratio_needs_ten_decisions_on_both_sides():
    with sqlite3.connect(":memory:") as connection:
        initialize_ledger(connection)
        for position, count in ((3, 10), (4, 9)):
            for _ in range(count):
                record_decision(connection, list_arm="ranked", list_position=position,
                                status="ok", p_up=0.6)
        assert boundary_ratio(connection, "ranked") is None


def test_report_prints_comparison(monkeypatch, tmp_path, capsys):
    database = tmp_path / "report.db"
    with sqlite3.connect(database) as connection:
        initialize_ledger(connection)
        for arm, position, convictions in (
            ("ranked", 3, 8), ("ranked", 4, 4),
            ("shuffled", 3, 6), ("shuffled", 4, 5),
        ):
            for index in range(10):
                record_decision(connection, list_arm=arm, list_position=position,
                                status="ok", p_up=0.6 if index < convictions else 0.5)
    monkeypatch.setattr("andy_trader.store.default_database_path", lambda: database)
    main()
    output = capsys.readouterr().out
    assert "ranked" in output and "shuffled" in output
    assert "80.0%" in output and "50.0%" in output
    assert "ranked arm drops at the cut more" in output
