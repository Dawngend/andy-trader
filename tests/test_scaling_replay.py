"""Stake rule replay uses settled paper evidence and bounded stakes."""

import sqlite3

import pytest

from andy_trader.scaling_replay import Call, compare, load_calls, replay
from andy_trader.store import initialize_database


def test_loads_only_settled_directional_paper_calls_in_time_order():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    calls = [
        ("2026-10-02T00:00:00+00:00", "paper", 0.8, 110.0, 1),
        ("2026-10-01T00:00:00+00:00", "paper", 0.2, 90.0, 0),
        ("2026-10-03T00:00:00+00:00", "advisory", 0.8, 110.0, 1),
        ("2026-10-04T00:00:00+00:00", "paper", 0.5, 100.0, 0),
        ("2026-10-05T00:00:00+00:00", "paper", 0.8, 110.0, None),
    ]
    for created, mode, probability, price, outcome in calls:
        connection.execute("""
            INSERT INTO crypto_predictions
            (predictor, instrument, horizon, probability_up, reference_price,
             mode, created_at, resolves_at, settled_at, settle_price, outcome_up)
            VALUES ('p', 'BTC-USD', '1h', ?, 100, ?, ?, ?, ?, ?, ?)
        """, (probability, mode, created, created,
              created if outcome is not None else None, price, outcome))
    loaded = load_calls(connection)
    assert [call.direction for call in loaded] == [-1, 1]
    assert [call.price_return for call in loaded] == pytest.approx([-0.1, 0.1])
    assert [call.fraction for call in loaded] == [0.1, 0.1]


def test_flat_and_compound_use_different_cash_bases_and_charge_both_legs():
    calls = [Call(1, 0.1, 0.5), Call(1, 0.1, 0.5)]
    flat = replay(calls, "flat", 100, 0.0015)
    compound = replay(calls, "compound", 100, 0.0015)
    per_dollar = (1 - 0.0015) ** 2 * 1.1 - 1
    assert flat.equity == pytest.approx(100 + 100 * per_dollar)
    assert compound.equity == pytest.approx(100 * (1 + 0.5 * per_dollar) ** 2)
    assert flat.trades == compound.trades == 2


def test_streaks_reset_after_net_loss_and_never_exceed_equity():
    calls = [Call(1, 0.1, 0.4), Call(1, 0.1, 0.4),
             Call(1, -0.1, 0.4), Call(1, 0.1, 0.4)]
    win_streak = replay(calls, "win_streak", 100, 0)
    doubled = replay(calls, "double", 100, 0)
    assert win_streak.equity == pytest.approx(106.47)
    assert doubled.equity == pytest.approx(105.13152)
    assert win_streak.max_drawdown == pytest.approx(0.0625)
    assert replay([Call(1, -0.1, 1)], "flat", 100, 0).equity == pytest.approx(90)
    wiped_out = replay([Call(-1, 2.0, 1), Call(1, 0.1, 1)], "double", 100, 0)
    assert wiped_out == type(wiped_out)(0.0, 1.0, 1)


def test_shuffle_summary_is_seeded_and_has_same_trade_count():
    calls = [Call(1, change, 0.4) for change in (0.1, 0.1, -0.1, 0.1)]
    first = compare(calls, 100, shuffles=200, seed=42)
    assert first == compare(calls, 100, shuffles=200, seed=42)
    assert all(actual.trades == shuffled.trades == 4 for actual, shuffled in first.values())
    assert first["double"][0].equity != first["double"][1].equity
