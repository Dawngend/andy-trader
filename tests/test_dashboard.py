"""Tests for the dashboard's read-only state projection."""

from datetime import UTC, datetime, timedelta
import json
import sqlite3

from andy_trader.dashboard import _collector_health, _json_safe, _latest_prices, _portfolios, _registry
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


def test_collector_health_uses_active_configuration_not_retired_history() -> None:
    connection = _conn()
    now = datetime.now(UTC)
    record_observations(
        connection,
        [
            Candle(
                instrument="ETH-USD", venue="binance", interval="1m",
                open_time=(now - timedelta(days=10)).isoformat(),
                open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0,
            )
        ],
        observed_at=(now - timedelta(days=10)).isoformat(),
    )
    record_observations(
        connection,
        [
            Candle(
                instrument="BTC-USD", venue="binance", interval="1m",
                open_time=now.isoformat(),
                open=101.0, high=101.0, low=101.0, close=101.0, volume=1.0,
            )
        ],
        observed_at=now.isoformat(),
    )

    health = _collector_health(
        connection, now, expected_series=(("BTC-USD", "1m"),)
    )

    assert health["healthy"] is True
    assert [(row["instrument"], row["interval"]) for row in health["series"]] == [
        ("BTC-USD", "1m")
    ]


def test_collector_health_marks_a_missing_active_series_unhealthy() -> None:
    health = _collector_health(
        _conn(),
        datetime.now(UTC),
        expected_series=(("BTC-USD", "1m"),),
    )

    assert health["healthy"] is False
    assert health["series"][0]["missing"] is True


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
    # The scoreboard row carries the non-overlapping count the dashboard shows.
    assert report["independent_count"] == 1

    # This must not raise -- the real historical bug was that build_dashboard_state
    # itself was fine; only json.dumps of its output silently produced invalid JSON.
    json.dumps(_json_safe(state))


def test_settlement_quality_reads_back_the_price_moment_each_call_used() -> None:
    """The dashboard shows how settlements were made, so a collector that starts
    delivering late prices or tripping the fallback is visible."""
    from andy_trader.dashboard import _settlement_quality

    connection = _conn()
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    notes = [
        ("2026-09-28T10:00:00+00:00", "tv 1h close at 2026-09-28T10:00:00+00:00, price as of 2026-09-28T10:02:00+00:00"),
        ("2026-09-28T10:15:00+00:00", "tv 1h close at 2026-09-28T10:00:00+00:00, price as of 2026-09-28T10:17:00+00:00"),
        ("2026-09-28T10:30:00+00:00", "tv 1h close at 2026-09-28T10:00:00+00:00, price as of 2026-09-28T10:40:00+00:00"),
        ("2026-09-28T09:30:00+00:00", "tv 1h close at 2026-09-28T09:00:00+00:00, price as of "
                                      "2026-09-28T09:20:00+00:00 (latest price before 2026-09-28T09:30:00+00:00; "
                                      "nothing was captured after it)"),
        ("2026-09-28T08:00:00+00:00", "kraken 1h close at 2026-09-28T08:00:00+00:00"),
    ]
    for index, (resolves_at, note) in enumerate(notes):
        connection.execute(
            "INSERT INTO crypto_predictions (predictor, instrument, horizon, probability_up, reference_price, "
            "mode, features_json, created_at, resolves_at, settled_at, settle_price, outcome_up, settle_note) "
            "VALUES ('baseline:x', 'BTC-USD', '1h', 0.5, 100, 'live', '{}', ?, ?, ?, 100, 1, ?)",
            (f"2026-09-28T0{index}:00:00+00:00", resolves_at, "2026-09-28T11:00:00+00:00", note),
        )

    quality = _settlement_quality(connection, now)

    assert quality["settled"] == 5
    assert quality["fallback"] == 1
    assert quality["legacy_notes"] == 1
    assert quality["early"] == 0
    assert quality["median_lag_minutes"] == 2.0
    assert quality["max_lag_minutes"] == 10.0


def test_settlement_quality_survives_naive_timestamps_and_uses_a_true_median() -> None:
    """From Codex's review: a naive resolves_at minus an aware price moment
    raised TypeError and broke the whole dashboard state, and an even number of
    lags reported the upper-middle value instead of the median."""
    from andy_trader.dashboard import _settlement_quality

    connection = _conn()
    rows = [
        ("2026-09-28T10:00:00", "tv 1h close at x, price as of 2026-09-28T10:02:00+00:00"),  # naive resolves_at
        ("2026-09-28T10:15:00+00:00", "tv 1h close at x, price as of 2026-09-28T10:19:00+00:00"),
    ]
    for index, (resolves_at, note) in enumerate(rows):
        connection.execute(
            "INSERT INTO crypto_predictions (predictor, instrument, horizon, probability_up, reference_price, "
            "mode, features_json, created_at, resolves_at, settled_at, settle_price, outcome_up, settle_note) "
            "VALUES ('baseline:x', 'BTC-USD', '1h', 0.5, 100, 'live', '{}', ?, ?, ?, 100, 1, ?)",
            (f"2026-09-28T0{index}:00:00+00:00", resolves_at, "2026-09-28T11:00:00+00:00", note),
        )

    quality = _settlement_quality(connection, datetime(2026, 9, 28, 12, 0, tzinfo=UTC))

    assert quality["median_lag_minutes"] == 3.0  # mean of 2 and 4
    assert quality["max_lag_minutes"] == 4.0


def test_registry_shows_a_demoted_model_as_demoted_not_still_promoted() -> None:
    """The original promotion row is an honest historical fact and must never
    be rewritten -- but the dashboard still needs to show that a later,
    separate fact (demotion) happened, or it would keep displaying a model
    as live-serving after it was actually pulled."""
    from andy_trader.registry import ModelRegistryEntry, record_demotion, record_registry_entry

    connection = _conn()
    entry = ModelRegistryEntry(
        model_id="btc_model_1", trained_at="2026-09-05T00:00:00+00:00",
        instrument="BTC-USD", interval="1h", horizon="1h",
        train_start_time="2026-09-01T00:00:00+00:00", train_end_time="2026-09-04T00:00:00+00:00",
        train_bars=72, holdout_start_time="2026-09-04T00:00:00+00:00",
        holdout_end_time="2026-09-05T00:00:00+00:00", holdout_bars=24,
        hyperparameters={}, holdout_brier=0.24, holdout_brier_reference=0.25,
        holdout_brier_skill=0.05, base_rate_brier_skill=0.0,
        promoted=True, promotion_reason="cleared the gate", weights_path=None,
    )
    record_registry_entry(connection, entry)
    record_demotion(
        connection, model_id="btc_model_1", instrument="BTC-USD", horizon="1h", interval="1h",
        demoted_at="2026-09-06T01:32:29+00:00", live_skill=-0.0796,
        live_call_count=70, base_rate_live_skill=-0.0057,
        reason="live Brier skill -0.079600 no longer beats baseline:base_rate -0.005700",
    )

    rows = _registry(connection)

    assert len(rows) == 1
    assert rows[0]["promoted"] == 1  # the original fact, unchanged
    assert rows[0]["demoted"] is True
    assert rows[0]["live_skill_at_demotion"] == -0.0796
    assert "no longer beats" in rows[0]["demotion_reason"]


def test_registry_shows_an_undemoted_promoted_model_as_still_promoted() -> None:
    from andy_trader.registry import ModelRegistryEntry, record_registry_entry

    connection = _conn()
    entry = ModelRegistryEntry(
        model_id="btc_model_2", trained_at="2026-09-05T00:00:00+00:00",
        instrument="BTC-USD", interval="1h", horizon="1h",
        train_start_time="2026-09-01T00:00:00+00:00", train_end_time="2026-09-04T00:00:00+00:00",
        train_bars=72, holdout_start_time="2026-09-04T00:00:00+00:00",
        holdout_end_time="2026-09-05T00:00:00+00:00", holdout_bars=24,
        hyperparameters={}, holdout_brier=0.24, holdout_brier_reference=0.25,
        holdout_brier_skill=0.05, base_rate_brier_skill=0.0,
        promoted=True, promotion_reason="cleared the gate", weights_path=None,
    )
    record_registry_entry(connection, entry)

    rows = _registry(connection)

    assert rows[0]["demoted"] is False
