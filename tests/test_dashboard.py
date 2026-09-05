"""Tests for the dashboard's read-only state projection."""

from datetime import UTC, datetime, timedelta
import json
import sqlite3

from andy_trader.dashboard import _collector_health, _json_safe, _latest_prices, _portfolios
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


def test_json_safe_neutralizes_every_non_finite_float_python_would_otherwise_emit() -> None:
    """The real bug this guards against: a brand-new predictor with a
    degenerate holdout sample produces a real -inf skill (calibration.py's
    own deliberate design, not a bug), and Python's json.dumps happily emits
    the non-standard -Infinity token for it. A strict client-side JSON.parse
    rejects the entire payload the instant that token appears anywhere in it
    -- this broke the live dashboard for real, not hypothetically."""
    payload = {
        "skill": float("-inf"),
        "also_bad": float("inf"),
        "nan_value": float("nan"),
        "fine": 1.5,
        "nested": {"deep": float("-inf"), "list": [1.0, float("nan"), 3]},
        "untouched": {"degenerate": True, "reason": "not enough evidence"},
    }

    safe = _json_safe(payload)

    assert safe["skill"] is None
    assert safe["also_bad"] is None
    assert safe["nan_value"] is None
    assert safe["fine"] == 1.5
    assert safe["nested"]["deep"] is None
    assert safe["nested"]["list"] == [1.0, None, 3]
    assert safe["untouched"] == {"degenerate": True, "reason": "not enough evidence"}

    # The actual regression check: this must be valid, standard JSON now,
    # parseable by a strict client, not just "doesn't raise in Python."
    reparsed = json.loads(json.dumps(safe))
    assert reparsed["skill"] is None


def test_build_dashboard_state_survives_a_real_degenerate_scoreboard_report() -> None:
    """End-to-end reproduction of the actual outage: one settled call, so the
    holdout sample is degenerate and the real calibration module returns a
    genuine -inf skill score for it -- confirm the whole pipeline (not just
    the sanitizer in isolation) produces standard-JSON-safe output."""
    from andy_trader.dashboard import build_dashboard_state
    from andy_trader.store import Prediction, record_prediction, settle_due_predictions

    connection = _conn()
    record_observations(
        connection,
        [_candle(venue="bybit", open_time="2026-09-05T00:00:00+00:00", close=100.0)],
    )
    record_observations(
        connection,
        [_candle(venue="bybit", open_time="2026-09-05T01:00:00+00:00", close=101.0)],
    )
    record_prediction(
        connection,
        Prediction(
            predictor="model:promoted", instrument="BTC-USD", horizon="1h",
            probability_up=0.9, reference_price=100.0,
            created_at="2026-09-05T00:00:00+00:00", resolves_at="2026-09-05T01:00:00+00:00",
        ),
    )
    settle_due_predictions(connection, now_iso="2026-09-05T01:00:00+00:00")

    state = build_dashboard_state(connection)
    report = state["scoreboard"]["model:promoted"]
    assert report["degenerate"] is True

    # This must not raise -- the real historical bug was that build_dashboard_state
    # itself was fine; only json.dumps of its output silently produced invalid JSON.
    json.dumps(_json_safe(state))
