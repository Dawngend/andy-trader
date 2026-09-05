"""Tests for CT-08 paper portfolio accounting: cash conservation, costs, decisions."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from andy_trader.portfolio import (
    DEFAULT_FLAT_THRESHOLD,
    DEFAULT_LONG_THRESHOLD,
    PortfolioError,
    decide_side,
    execute_paper_trade,
    fetch_equity_curve,
    fetch_recent_trades,
    get_or_create_state,
    initialize_portfolio,
    main,
    mark_to_market,
    paper_trade_once,
    run_paper_cycle,
)
from andy_trader.store import Candle, Prediction, connect, record_observations, record_prediction


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_portfolio(connection)
    return connection


def test_decide_side_enters_long_only_above_long_threshold() -> None:
    assert decide_side(0.9, current_side="flat") == "long"
    assert decide_side(DEFAULT_LONG_THRESHOLD, current_side="flat") == "long"
    assert decide_side(DEFAULT_LONG_THRESHOLD - 0.001, current_side="flat") == "flat"
    assert decide_side(0.1, current_side="flat") == "flat"


def test_decide_side_hysteresis_holds_long_between_the_two_thresholds() -> None:
    """A probability sitting between flat and long thresholds should not force
    an exit just because it is no longer high enough to have entered fresh --
    that is the whole point of hysteresis: it avoids paying costs on churn."""
    midpoint = (DEFAULT_LONG_THRESHOLD + DEFAULT_FLAT_THRESHOLD) / 2
    assert decide_side(midpoint, current_side="long") == "long"
    assert decide_side(midpoint, current_side="flat") == "flat"
    assert decide_side(DEFAULT_FLAT_THRESHOLD, current_side="long") == "flat"
    assert decide_side(DEFAULT_FLAT_THRESHOLD + 0.001, current_side="long") == "long"


def test_decide_side_rejects_out_of_range_probability() -> None:
    with pytest.raises(PortfolioError):
        decide_side(1.5)
    with pytest.raises(PortfolioError):
        decide_side(-0.1)


def test_new_state_starts_flat_with_full_starting_cash() -> None:
    connection = _conn()
    state = get_or_create_state(
        connection, predictor="baseline:momentum", instrument="BTC-USD", now_iso="2026-09-05T00:00:00+00:00"
    )
    assert state.position_qty == 0.0
    assert state.starting_cash == 10_000.0
    assert state.cash == 10_000.0
    assert state.avg_entry_price is None


def test_opening_long_deducts_fee_and_slippage_from_deployed_cash() -> None:
    connection = _conn()
    trade = execute_paper_trade(
        connection,
        predictor="baseline:momentum",
        instrument="BTC-USD",
        target_side="long",
        price=100.0,
        now_iso="2026-09-05T00:00:00+00:00",
        reason="test",
        fee_bps=10.0,
        slippage_bps=5.0,
        starting_cash=10_000.0,
    )
    assert trade is not None
    assert trade.side == "long"
    # cost_rate = 15bps = 0.0015; deployable = 10000 * (1 - 0.0015) = 9985.0
    assert trade.qty == pytest.approx(9985.0 / 100.0)
    assert trade.cash_after == 0.0
    assert trade.fee_cost == pytest.approx(10_000.0 * 0.0010)
    assert trade.slippage_cost == pytest.approx(10_000.0 * 0.0005)


def test_no_trade_when_already_at_target_side() -> None:
    connection = _conn()
    first = execute_paper_trade(
        connection, predictor="p", instrument="BTC-USD", target_side="long",
        price=100.0, now_iso="2026-09-05T00:00:00+00:00", reason="open",
    )
    assert first is not None
    second = execute_paper_trade(
        connection, predictor="p", instrument="BTC-USD", target_side="long",
        price=105.0, now_iso="2026-09-05T01:00:00+00:00", reason="stay long",
    )
    assert second is None  # no churn, no cost, when nothing changed

    trades = fetch_recent_trades(connection, predictor="p", instrument="BTC-USD")
    assert len(trades) == 1  # only the opening trade was ever recorded


def test_round_trip_conserves_value_minus_costs_only() -> None:
    """Buy then immediately sell flat at the same price: only cost is the two-way fee/slippage."""
    connection = _conn()
    starting_cash = 10_000.0
    execute_paper_trade(
        connection, predictor="p", instrument="BTC-USD", target_side="long",
        price=100.0, now_iso="2026-09-05T00:00:00+00:00", reason="open",
        starting_cash=starting_cash,
    )
    close_trade = execute_paper_trade(
        connection, predictor="p", instrument="BTC-USD", target_side="flat",
        price=100.0, now_iso="2026-09-05T01:00:00+00:00", reason="close",
        starting_cash=starting_cash,
    )
    assert close_trade is not None
    state = get_or_create_state(
        connection, predictor="p", instrument="BTC-USD", now_iso="2026-09-05T01:00:00+00:00"
    )
    # Costs compound across the two legs (exit cost is charged on the
    # already-cost-reduced notional from entry, not on the original starting
    # cash again) -- that is the realistic behavior, not double-counting.
    cost_rate = (10.0 + 5.0) / 10_000.0
    expected_cash = starting_cash * (1.0 - cost_rate) * (1.0 - cost_rate)
    assert state.cash == pytest.approx(expected_cash, rel=1e-9)
    assert state.cash < starting_cash  # a round trip at an unchanged price must never profit
    assert state.position_qty == 0.0


def test_mark_to_market_records_equity_curve_point() -> None:
    connection = _conn()
    execute_paper_trade(
        connection, predictor="p", instrument="BTC-USD", target_side="long",
        price=100.0, now_iso="2026-09-05T00:00:00+00:00", reason="open",
    )
    equity = mark_to_market(
        connection, predictor="p", instrument="BTC-USD", price=110.0, now_iso="2026-09-05T01:00:00+00:00"
    )
    curve = fetch_equity_curve(connection, predictor="p", instrument="BTC-USD")
    assert len(curve) == 1
    assert curve[0]["equity"] == pytest.approx(equity)
    # Price rose 10% on a fully-deployed position: equity should have risen close to 10%
    # (minus the entry cost already paid), not stayed flat and not gone down.
    assert equity > 10_000.0 * 0.98


def test_run_paper_cycle_opens_on_high_confidence_and_marks_to_market() -> None:
    connection = _conn()
    result = run_paper_cycle(
        connection,
        predictor="p",
        instrument="BTC-USD",
        probability_up=0.9,
        price=100.0,
        now_iso="2026-09-05T00:00:00+00:00",
    )
    assert result.trade is not None
    assert result.trade.side == "long"
    assert result.equity == pytest.approx(result.trade.qty * 100.0)
    assert result.risk_allowed is True
    assert result.forced_exit is False


def test_run_paper_cycle_stays_flat_below_threshold() -> None:
    connection = _conn()
    result = run_paper_cycle(
        connection,
        predictor="p",
        instrument="BTC-USD",
        probability_up=0.5,
        price=100.0,
        now_iso="2026-09-05T00:00:00+00:00",
    )
    assert result.trade is None
    assert result.equity == pytest.approx(10_000.0)


def test_cannot_trade_at_nonpositive_price() -> None:
    connection = _conn()
    with pytest.raises(PortfolioError):
        execute_paper_trade(
            connection, predictor="p", instrument="BTC-USD", target_side="long",
            price=0.0, now_iso="2026-09-05T00:00:00+00:00", reason="bad price",
        )


def test_separate_predictors_and_instruments_have_isolated_state() -> None:
    connection = _conn()
    execute_paper_trade(
        connection, predictor="a", instrument="BTC-USD", target_side="long",
        price=100.0, now_iso="2026-09-05T00:00:00+00:00", reason="a",
    )
    b_state = get_or_create_state(
        connection, predictor="b", instrument="BTC-USD", now_iso="2026-09-05T00:00:00+00:00"
    )
    assert b_state.cash == 10_000.0  # untouched by "a" trading
    assert b_state.position_qty == 0.0


def test_existing_portfolio_schema_is_migrated_with_a_starting_cash_denominator() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE paper_portfolio_state (
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            cash REAL NOT NULL,
            position_qty REAL NOT NULL,
            avg_entry_price REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (predictor, instrument)
        )
        """
    )
    connection.execute(
        "INSERT INTO paper_portfolio_state VALUES (?, ?, ?, ?, ?, ?)",
        ("p", "BTC-USD", 10_000.0, 0.0, None, "2026-09-05T00:00:00+00:00"),
    )

    initialize_portfolio(connection)

    state = get_or_create_state(
        connection,
        predictor="p",
        instrument="BTC-USD",
        now_iso="2026-09-05T00:00:00+00:00",
    )
    assert state.starting_cash == 10_000.0


def test_cli_uses_the_requested_horizon_and_wall_clock_execution_time(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    bar_time = (now - timedelta(minutes=30)).isoformat()
    one_hour_call_time = (now - timedelta(minutes=2)).isoformat()
    four_hour_call_time = (now - timedelta(minutes=1)).isoformat()
    with connect(database) as connection:
        record_observations(
            connection,
            [
                Candle(
                    instrument="BTC-USD",
                    venue="bybit",
                    interval="1h",
                    open_time=bar_time,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.0,
                    volume=1.0,
                )
            ],
        )
        record_prediction(
            connection,
            Prediction(
                predictor="baseline:test",
                instrument="BTC-USD",
                horizon="1h",
                probability_up=0.5,
                reference_price=100.0,
                created_at=one_hour_call_time,
                resolves_at=(now + timedelta(minutes=58)).isoformat(),
            ),
        )
        record_prediction(
            connection,
            Prediction(
                predictor="baseline:test",
                instrument="BTC-USD",
                horizon="4h",
                probability_up=0.9,
                reference_price=100.0,
                created_at=four_hour_call_time,
                resolves_at=(now + timedelta(hours=4)).isoformat(),
            ),
        )

    assert main(
        [
            "--predictor",
            "baseline:test",
            "--instrument",
            "BTC-USD",
            "--horizon",
            "1h",
            "--database",
            str(database),
        ]
    ) == 0

    with connect(database) as connection:
        state = connection.execute("SELECT * FROM paper_portfolio_state").fetchone()
        assert state["position_qty"] == 0.0
        assert datetime.fromisoformat(state["updated_at"]) > datetime.fromisoformat(bar_time)


def test_cli_refuses_a_stale_prediction(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    bar_time = (now - timedelta(minutes=30)).isoformat()
    with connect(database) as connection:
        record_observations(
            connection,
            [
                Candle(
                    instrument="BTC-USD",
                    venue="bybit",
                    interval="1h",
                    open_time=bar_time,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.0,
                    volume=1.0,
                )
            ],
        )
        record_prediction(
            connection,
            Prediction(
                predictor="baseline:test",
                instrument="BTC-USD",
                horizon="1h",
                probability_up=0.9,
                reference_price=100.0,
                created_at=(now - timedelta(minutes=21)).isoformat(),
                resolves_at=(now + timedelta(minutes=39)).isoformat(),
            ),
        )

    assert main(
        [
            "--predictor",
            "baseline:test",
            "--database",
            str(database),
        ]
    ) == 1
    assert "refusing to trade a stale decision" in capsys.readouterr().out

    with connect(database) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'paper_trades'"
        ).fetchall()
        assert tables == []


def test_paper_trade_once_refuses_when_no_prediction_exists(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    with connect(database) as connection:
        attempt = paper_trade_once(connection, predictor="baseline:test", instrument="BTC-USD")
    assert attempt.trade is None
    assert attempt.equity is None
    assert "no 1h predictions found" in attempt.skipped_reason


def test_paper_trade_once_trades_a_fresh_prediction(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    bar_time = (now - timedelta(minutes=5)).isoformat()
    call_time = (now - timedelta(minutes=1)).isoformat()
    with connect(database) as connection:
        record_observations(
            connection,
            [
                Candle(
                    instrument="BTC-USD", venue="bybit", interval="1h", open_time=bar_time,
                    open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0,
                )
            ],
        )
        record_prediction(
            connection,
            Prediction(
                predictor="baseline:test", instrument="BTC-USD", horizon="1h",
                probability_up=0.9, reference_price=100.0,
                created_at=call_time, resolves_at=(now + timedelta(minutes=59)).isoformat(),
            ),
        )
        attempt = paper_trade_once(connection, predictor="baseline:test", instrument="BTC-USD", now=now)

    assert attempt.skipped_reason is None
    assert attempt.trade is not None
    assert attempt.trade.side == "long"
    assert attempt.equity is not None


def test_paper_trade_once_reuses_the_same_staleness_rule_the_cli_uses(tmp_path: Path) -> None:
    """The whole point of extracting paper_trade_once: one refusal rule, not two."""
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    with connect(database) as connection:
        record_observations(
            connection,
            [
                Candle(
                    instrument="BTC-USD", venue="bybit", interval="1h",
                    open_time=(now - timedelta(minutes=5)).isoformat(),
                    open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0,
                )
            ],
        )
        record_prediction(
            connection,
            Prediction(
                predictor="baseline:test", instrument="BTC-USD", horizon="1h",
                probability_up=0.9, reference_price=100.0,
                created_at=(now - timedelta(minutes=21)).isoformat(),  # > 20m default limit
                resolves_at=(now + timedelta(minutes=39)).isoformat(),
            ),
        )
        attempt = paper_trade_once(connection, predictor="baseline:test", instrument="BTC-USD", now=now)

    assert attempt.trade is None
    assert "refusing to trade a stale decision" in attempt.skipped_reason
