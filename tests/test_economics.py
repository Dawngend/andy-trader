"""Tests for the pre-model question: can this horizon be traded at all?"""

import sqlite3

import pytest

from andy_trader.economics import (
    MINIMUM_BARS,
    average_absolute_move_bps,
    evaluate_horizon,
)
from andy_trader.store import Candle, initialize_database, record_observations


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def _series(connection: sqlite3.Connection, closes: list[float], *, interval: str) -> None:
    record_observations(
        connection,
        [
            Candle(
                instrument="BTC-USD",
                venue="binance",
                interval=interval,
                open_time=f"2026-09-06T{index // 60:02d}:{index % 60:02d}:00+00:00",
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1.0,
            )
            for index, close in enumerate(closes)
        ],
    )


def test_a_horizon_whose_cost_exceeds_its_move_is_reported_as_impossible() -> None:
    """The real finding this module exists for. At 1h the round-trip cost is
    larger than the average move, so the break-even win rate exceeds 100%: a
    predictor that was right every single time would still lose money. That is
    arithmetic, not a tuning problem, and it must be stated as impossible rather
    than merely difficult."""
    connection = _conn()
    # 10 bps per bar, well under the 30 bps cost of capturing it.
    closes = [100_000.0 * (1.001 if i % 2 else 1.0) for i in range(60)]
    _series(connection, closes, interval="1h")

    result = evaluate_horizon(connection, instrument="BTC-USD", interval="1h")

    assert result is not None
    assert result.cost_share_of_move > 1.0
    assert result.break_even_win_rate > 1.0
    assert result.verdict == "impossible"


def test_a_larger_move_lowers_the_bar_for_the_same_cost() -> None:
    """Cost is flat across horizons and the move is not, which is the entire
    reason a longer horizon can be viable when a shorter one is closed."""
    connection = _conn()
    small = [100_000.0 * (1.0005 if i % 2 else 1.0) for i in range(60)]
    _series(connection, small, interval="1h")
    large = [100_000.0 * (1.02 if i % 2 else 1.0) for i in range(60)]
    _series(connection, large, interval="1d")

    tight = evaluate_horizon(connection, instrument="BTC-USD", interval="1h")
    wide = evaluate_horizon(connection, instrument="BTC-USD", interval="1d")

    assert tight is not None and wide is not None
    assert wide.average_move_bps > tight.average_move_bps
    assert wide.break_even_win_rate < tight.break_even_win_rate
    assert wide.verdict == "plausible"


def test_break_even_matches_the_stated_formula() -> None:
    connection = _conn()
    # A clean 100 bps alternating move.
    closes = [100_000.0 * (1.01 if i % 2 else 1.0) for i in range(60)]
    _series(connection, closes, interval="4h")

    result = evaluate_horizon(
        connection, instrument="BTC-USD", interval="4h", round_trip_bps=30.0
    )

    assert result is not None
    expected = 0.5 + 30.0 / (2.0 * result.average_move_bps)
    assert result.break_even_win_rate == pytest.approx(expected)


def test_too_little_history_reports_nothing_rather_than_a_confident_number() -> None:
    connection = _conn()
    _series(connection, [100_000.0, 100_100.0, 100_050.0], interval="1h")

    assert evaluate_horizon(connection, instrument="BTC-USD", interval="1h") is None


def test_a_flat_market_has_no_measurable_move() -> None:
    connection = _conn()
    _series(connection, [100_000.0] * (MINIMUM_BARS + 10), interval="1h")

    move, bars = average_absolute_move_bps(connection, instrument="BTC-USD", interval="1h")

    assert move == 0.0
    assert bars > MINIMUM_BARS
    # A market that never moves cannot be traded, and must not report a
    # break-even of 50% as though it were a coin flip worth taking.
    assert evaluate_horizon(connection, instrument="BTC-USD", interval="1h") is None
