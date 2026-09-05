"""Tests for the dashboard's read-only state projection."""

from datetime import UTC, datetime, timedelta
import sqlite3

from andy_trader.dashboard import _collector_health, _latest_prices, _portfolios
from andy_trader.portfolio import run_paper_cycle
from andy_trader.risk import initialize_risk
from andy_trader.store import Candle, initialize_database, record_observations


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def _candle(*, venue: str, open_time: str, close: float | None, degraded: bool = False) -> Candle:
    return Candle(
        instrument="BTC-USD",
        venue=venue,
        interval="1h",
        open_time=open_time,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0 if close is not None else None,
        degraded=degraded,
        degraded_reason="blocked" if degraded else None,
    )


def test_degraded_attempt_never_makes_collector_health_green() -> None:
    connection = _conn()
    now = datetime.now(UTC)
    record_observations(
        connection,
        [_candle(venue="bybit", open_time=now.isoformat(), close=None, degraded=True)],
    )

    health = _collector_health(connection, now)

    assert health["series"] == []
    assert not health["healthy"]


def test_collector_health_separately_flags_an_old_usable_bar() -> None:
    connection = _conn()
    now = datetime.now(UTC)
    old_bar = (now - timedelta(hours=3)).isoformat()
    record_observations(connection, [_candle(venue="bybit", open_time=old_bar, close=100.0)])

    health = _collector_health(connection, now)

    assert not health["series"][0]["stale"]
    assert health["series"][0]["data_stale"]
    assert not health["healthy"]


def test_latest_price_uses_the_same_repeat_then_venue_tiebreak_as_prediction() -> None:
    connection = _conn()
    stamp = datetime.now(UTC).isoformat()
    coinbase = _candle(venue="coinbase", open_time=stamp, close=101.0)
    kraken = _candle(venue="kraken", open_time=stamp, close=102.0)
    record_observations(connection, [coinbase, kraken])
    record_observations(connection, [coinbase])

    prices = _latest_prices(connection)

    assert prices[0]["close"] == 101.0


def test_paper_return_includes_the_first_entry_cost() -> None:
    connection = _conn()
    run_paper_cycle(
        connection,
        predictor="p",
        instrument="BTC-USD",
        probability_up=0.60,
        price=100.0,
        now_iso="2026-09-05T00:00:00+00:00",
    )

    portfolios = _portfolios(connection)

    assert portfolios[0]["return_pct"] < 0.0
    assert portfolios[0]["risk"] == {"tripped": False, "severity": None, "reason": None}


def test_portfolio_surfaces_a_tripped_risk_state() -> None:
    connection = _conn()
    initialize_risk(connection)
    run_paper_cycle(
        connection, predictor="p", instrument="BTC-USD", probability_up=0.60,
        price=100.0, now_iso="2026-09-05T00:00:00+00:00",
    )
    connection.execute(
        "INSERT INTO risk_kill_switch (predictor, instrument, tripped, severity, tripped_at, tripped_reason) "
        "VALUES ('p', 'BTC-USD', 1, 'hard', '2026-09-05T01:00:00+00:00', 'total loss exceeded')"
    )
    connection.commit()

    portfolios = _portfolios(connection)

    assert portfolios[0]["risk"]["tripped"] is True
    assert portfolios[0]["risk"]["severity"] == "hard"
    assert portfolios[0]["risk"]["reason"] == "total loss exceeded"
