from pathlib import Path

import pytest

from andy_trader.analysis import (
    AnalysisError,
    format_profiles,
    profile_closes,
    profile_instruments,
    returns,
)
from andy_trader.store import Candle, connect, record_observations


def _flat_then_move(move: float, bars: int = 40) -> list[float]:
    """A series that alternates up and down by `move`, so the average move is exact."""

    price = 100.0
    closes = [price]
    for index in range(bars - 1):
        price *= (1.0 + move) if index % 2 == 0 else 1.0 / (1.0 + move)
        closes.append(price)
    return closes


def test_returns_computes_period_over_period() -> None:
    assert returns([100.0, 110.0, 99.0]) == pytest.approx([0.1, -0.1])


def test_returns_rejects_a_series_that_is_too_short() -> None:
    with pytest.raises(AnalysisError):
        returns([100.0])


def test_returns_rejects_a_non_positive_price() -> None:
    with pytest.raises(AnalysisError, match="non-positive"):
        returns([0.0, 100.0])


def test_cost_ratio_above_one_means_the_fee_exceeds_the_move() -> None:
    """A 0.1% average move cannot pay a 0.3% round trip. This is the BTC case."""

    profile = profile_closes("TINY-USD", _flat_then_move(0.001), round_trip=0.0030)
    assert profile.cost_ratio > 1.0
    assert not profile.cost_barrier_passable


def test_cost_ratio_below_one_means_a_typical_move_covers_the_trip() -> None:
    profile = profile_closes("BIG-USD", _flat_then_move(0.01), round_trip=0.0030)
    assert profile.cost_ratio < 1.0
    assert profile.cost_barrier_passable


def test_cost_ratio_scales_inversely_with_the_move() -> None:
    small = profile_closes("A", _flat_then_move(0.002), round_trip=0.0030)
    large = profile_closes("B", _flat_then_move(0.008), round_trip=0.0030)
    # Four times the move is roughly a quarter of the cost ratio.
    assert large.cost_ratio == pytest.approx(small.cost_ratio / 4, rel=0.05)


def test_a_zero_round_trip_is_always_passable() -> None:
    assert profile_closes("A", _flat_then_move(0.001), round_trip=0.0).cost_ratio == 0.0


def test_negative_round_trip_is_rejected() -> None:
    with pytest.raises(AnalysisError):
        profile_closes("A", _flat_then_move(0.01), round_trip=-0.001)


def test_too_little_history_is_refused_rather_than_estimated() -> None:
    with pytest.raises(AnalysisError, match="at least"):
        profile_closes("A", [100.0, 101.0, 100.0])


def test_a_series_that_never_moves_is_refused() -> None:
    with pytest.raises(AnalysisError, match="never moved"):
        profile_closes("FLAT-USD", [100.0] * 40)


def _series(instrument: str, closes: list[float]) -> list[Candle]:
    return [
        Candle(
            instrument=instrument, venue="bybit", interval="1h",
            open_time=f"2026-09-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00",
            open=c, high=c + 1, low=c - 1, close=c, volume=1.0,
        )
        for i, c in enumerate(closes)
    ]


def test_profiles_are_sorted_most_volatile_first(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series("CALM-USD", _flat_then_move(0.001)))
        record_observations(connection, _series("WILD-USD", _flat_then_move(0.02)))
        profiles = profile_instruments(connection, ["CALM-USD", "WILD-USD"])

    assert [p.instrument for p in profiles] == ["WILD-USD", "CALM-USD"]
    assert profiles[0].cost_barrier_passable
    assert not profiles[1].cost_barrier_passable


def test_instruments_without_enough_history_are_skipped_not_guessed(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series("WILD-USD", _flat_then_move(0.02)))
        record_observations(connection, _series("THIN-USD", [100.0, 101.0]))
        profiles = profile_instruments(connection, ["WILD-USD", "THIN-USD"])

    assert [p.instrument for p in profiles] == ["WILD-USD"]


def test_report_labels_an_unwinnable_barrier(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series("TINY-USD", _flat_then_move(0.0005)))
        text = format_profiles(profile_instruments(connection, ["TINY-USD"]))

    assert "UNWINNABLE" in text
    assert "nowhere near sufficient" in text


def test_report_handles_having_nothing_to_say() -> None:
    assert "No instrument" in format_profiles([])
