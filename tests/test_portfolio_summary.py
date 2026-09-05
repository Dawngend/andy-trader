"""Tests for cross-pair paper portfolio accounting."""

import sqlite3

import pytest

from andy_trader.portfolio import (
    PortfolioSummary,
    get_or_create_state,
    initialize_portfolio,
    portfolio_summary,
)


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_portfolio(connection)
    return connection


def _add_portfolio(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    starting_cash: float,
    cash: float,
    equities: list[tuple[str, float]],
    trade_count: int = 0,
) -> None:
    connection.execute(
        """
        INSERT INTO paper_portfolio_state
        (predictor, instrument, starting_cash, cash, position_qty, avg_entry_price, updated_at)
        VALUES (?, ?, ?, ?, 0.0, NULL, ?)
        """,
        (predictor, instrument, starting_cash, cash, "2026-09-05T00:00:00+00:00"),
    )
    for recorded_at, equity in equities:
        connection.execute(
            """
            INSERT INTO paper_equity_curve
            (predictor, instrument, cash, position_qty, mark_price,
             position_value, equity, recorded_at)
            VALUES (?, ?, ?, 0.0, 1.0, 0.0, ?, ?)
            """,
            (predictor, instrument, cash, equity, recorded_at),
        )
    for index in range(trade_count):
        connection.execute(
            """
            INSERT INTO paper_trades
            (predictor, instrument, side, qty, price, fee_cost,
             slippage_cost, cash_after, executed_at, reason)
            VALUES (?, ?, 'long', 1.0, 1.0, 0.0, 0.0, ?, ?, 'test')
            """,
            (
                predictor,
                instrument,
                cash,
                f"2026-09-05T00:{index:02d}:00+00:00",
            ),
        )
    connection.commit()


def test_empty_database_returns_zero_summary() -> None:
    connection = sqlite3.connect(":memory:")

    assert portfolio_summary(connection) == PortfolioSummary(
        total_starting_cash=0.0,
        total_equity=0.0,
        total_return_pct=0.0,
        total_trade_count=0,
        winners=0,
        losers=0,
        best=None,
        worst=None,
    )
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall() == []


def test_single_portfolio_matches_its_own_return_exactly() -> None:
    connection = _conn()
    starting_cash = 10_000.0
    latest_equity = 12_345.67
    expected_return_pct = (latest_equity / starting_cash - 1.0) * 100.0
    _add_portfolio(
        connection,
        predictor="baseline:momentum",
        instrument="BTC-USD",
        starting_cash=starting_cash,
        cash=0.0,
        equities=[("2026-09-05T01:00:00+00:00", latest_equity)],
        trade_count=1,
    )

    summary = portfolio_summary(connection)

    assert summary.total_return_pct == expected_return_pct
    assert summary.best == ("baseline:momentum", "BTC-USD", expected_return_pct)
    assert summary.worst == ("baseline:momentum", "BTC-USD", expected_return_pct)


def test_multiple_portfolios_aggregate_each_own_bankroll_and_latest_mark() -> None:
    connection = _conn()
    _add_portfolio(
        connection,
        predictor="baseline:momentum",
        instrument="BTC-USD",
        starting_cash=10_000.0,
        cash=100.0,
        equities=[
            ("2026-09-05T01:00:00+00:00", 9_000.0),
            ("2026-09-05T03:00:00+00:00", 12_000.0),
        ],
        trade_count=2,
    )
    _add_portfolio(
        connection,
        predictor="baseline:momentum",
        instrument="ETH-USD",
        starting_cash=20_000.0,
        cash=500.0,
        equities=[("2026-09-05T02:00:00+00:00", 18_000.0)],
        trade_count=1,
    )
    _add_portfolio(
        connection,
        predictor="signal:crowd",
        instrument="DOGE-USD",
        starting_cash=5_000.0,
        cash=50.0,
        equities=[("2026-09-05T04:00:00+00:00", 5_250.0)],
        trade_count=3,
    )

    summary = portfolio_summary(connection)

    expected_starting_cash = 10_000.0 + 20_000.0 + 5_000.0
    expected_equity = 12_000.0 + 18_000.0 + 5_250.0
    expected_total_return_pct = (
        expected_equity / expected_starting_cash - 1.0
    ) * 100.0
    assert summary.total_starting_cash == expected_starting_cash
    assert summary.total_equity == expected_equity
    assert summary.total_return_pct == pytest.approx(expected_total_return_pct)
    assert summary.total_trade_count == 2 + 1 + 3
    assert summary.winners == 2
    assert summary.losers == 1
    assert summary.best is not None
    assert summary.best[:2] == ("baseline:momentum", "BTC-USD")
    assert summary.best[2] == pytest.approx(20.0)
    assert summary.worst is not None
    assert summary.worst[:2] == ("baseline:momentum", "ETH-USD")
    assert summary.worst[2] == pytest.approx(-10.0)


def test_portfolio_without_equity_curve_falls_back_to_cash() -> None:
    connection = _conn()
    get_or_create_state(
        connection,
        predictor="baseline:momentum",
        instrument="LINK-USD",
        starting_cash=7_000.0,
        now_iso="2026-09-05T00:00:00+00:00",
    )
    connection.execute(
        """
        UPDATE paper_portfolio_state
        SET cash = 6_500.0, position_qty = 3.0
        WHERE predictor = 'baseline:momentum' AND instrument = 'LINK-USD'
        """
    )
    connection.commit()

    summary = portfolio_summary(connection)

    expected_return_pct = (6_500.0 / 7_000.0 - 1.0) * 100.0
    assert summary.total_equity == 6_500.0
    assert summary.total_return_pct == expected_return_pct
    assert summary.worst == ("baseline:momentum", "LINK-USD", expected_return_pct)
