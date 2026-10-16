"""Volatility sizing and partial paper position accounting."""

import math

import pytest

from andy_trader.portfolio import execute_paper_trade, get_or_create_state, mark_to_market, run_paper_cycle
from andy_trader.sizing import realized_volatility, size_fraction
from andy_trader.store import Candle, initialize_database, record_observations

from test_portfolio import _conn


def test_realized_volatility_uses_last_24_closes() -> None:
    assert realized_volatility([100.0] * 5) is None
    assert realized_volatility([100.0] * 6) == 0.0
    closes = [100.0] * 30
    closes[-1] = 110.0
    assert realized_volatility(closes) == pytest.approx(realized_volatility(closes[-24:]))


def test_fraction_clamps_and_falls_with_volatility(monkeypatch) -> None:
    monkeypatch.delenv("CRYPTO_TARGET_VOL", raising=False)
    assert size_fraction(0.005) == 1.0
    assert size_fraction(0.01) == 1.0
    assert size_fraction(0.02) == 0.5
    assert size_fraction(0.05) == pytest.approx(0.2)
    assert size_fraction(0.2) == 0.1
    assert size_fraction(None) == 0.1
    assert size_fraction(0.0) == 0.1
    assert size_fraction(0.001, cap=0.7) == 0.7
    assert size_fraction(1.0, floor=0.2) == 0.2
    monkeypatch.setenv("CRYPTO_TARGET_VOL", "0.02")
    assert size_fraction(0.04) == 0.5


@pytest.mark.parametrize("side,mark,expected", [
    ("long", 110.0, 10_000.0 + 0.5 * 10_000.0 * 0.1),
    ("short", 90.0, 10_000.0 + 0.5 * 10_000.0 * 0.1),
])
def test_partial_position_keeps_idle_cash_and_equity(side, mark, expected) -> None:
    connection = _conn()
    trade = execute_paper_trade(
        connection, predictor="p", instrument="BTC-USD", target_side=side,
        price=100.0, now_iso="2026-09-05T00:00:00+00:00", reason="open",
        size_fraction=0.5, fee_bps=0, slippage_bps=0,
    )
    state = get_or_create_state(
        connection, predictor="p", instrument="BTC-USD", now_iso="2026-09-05T00:00:00+00:00"
    )
    assert trade.qty == 50.0
    assert state.cash == (5_000.0 if side == "long" else 15_000.0)
    assert mark_to_market(
        connection, predictor="p", instrument="BTC-USD", price=mark,
        now_iso="2026-09-05T01:00:00+00:00",
    ) == pytest.approx(expected)


def test_default_open_still_uses_all_cash() -> None:
    connection = _conn()
    trade = execute_paper_trade(
        connection, predictor="p", instrument="BTC-USD", target_side="long",
        price=100.0, now_iso="2026-09-05T00:00:00+00:00", reason="open",
    )
    assert trade.cash_after == 0.0
    assert trade.qty == pytest.approx(99.85)


def test_cycle_sizes_from_instruments_hourly_closes() -> None:
    connection = _conn()
    initialize_database(connection)
    closes = [100.0]
    for change in [0.04, -0.04] * 12:
        closes.append(closes[-1] * math.exp(change))
    record_observations(connection, [
        Candle(
            instrument="BTC-USD", venue="bybit", interval="1h",
            open_time=f"2026-09-{day:02d}T00:00:00+00:00",
            open=close, high=close, low=close, close=close, volume=1.0,
        )
        for day, close in enumerate(closes, start=1)
    ])
    result = run_paper_cycle(
        connection, predictor="p", instrument="BTC-USD", probability_up=0.9,
        price=100.0, now_iso="2026-10-01T00:00:00+00:00",
        fee_bps=0, slippage_bps=0,
    )
    assert result.trade is not None
    assert result.trade.qty < 100.0
    assert "size " in result.trade.reason
    assert "hourly vol" in result.trade.reason
    assert result.equity == pytest.approx(10_000.0)
