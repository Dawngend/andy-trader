from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from andy_trader.store import (
    Candle,
    CryptoStoreError,
    Prediction,
    close_price_at,
    connect,
    fetch_settled,
    horizon_delta,
    record_observations,
    record_prediction,
    settle_due_predictions,
)


def _candle(**overrides) -> Candle:
    base = {
        "instrument": "BTC-USD",
        "venue": "kraken",
        "interval": "1h",
        "open_time": "2026-09-04T00:00:00+00:00",
        "open": 100.0,
        "high": 110.0,
        "low": 95.0,
        "close": 105.0,
        "volume": 12.5,
    }
    base.update(overrides)
    return Candle(**base)


def test_content_hash_is_stable_for_identical_values() -> None:
    assert _candle().content_hash() == _candle().content_hash()


def test_content_hash_changes_when_a_price_changes() -> None:
    assert _candle().content_hash() != _candle(close=106.0).content_hash()


def test_degraded_flag_is_part_of_identity() -> None:
    ok = _candle(open=None, high=None, low=None, close=None, volume=None)
    degraded = _candle(
        open=None, high=None, low=None, close=None, volume=None,
        degraded=True, degraded_reason="URLError: timed out",
    )
    assert ok.content_hash() != degraded.content_hash()


def test_repeat_observation_bumps_times_seen_without_duplicating(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, [_candle()])
        inserted, seen = record_observations(connection, [_candle()])
        assert (inserted, seen) == (0, 1)
        row = connection.execute("SELECT times_seen FROM crypto_observations").fetchone()
        assert row["times_seen"] == 2
        count = connection.execute("SELECT COUNT(*) AS n FROM crypto_observations").fetchone()
        assert count["n"] == 1


def test_revised_candle_lands_as_a_second_row(tmp_path: Path) -> None:
    """A venue revising a bar must not overwrite what we already saw."""

    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, [_candle()])
        record_observations(connection, [_candle(close=999.0)])
        count = connection.execute("SELECT COUNT(*) AS n FROM crypto_observations").fetchone()
        assert count["n"] == 2


def test_prediction_rejects_out_of_range_probability() -> None:
    with pytest.raises(CryptoStoreError):
        Prediction(
            predictor="p", instrument="BTC-USD", horizon="1h", probability_up=1.4,
            reference_price=100.0, created_at="2026-09-04T00:00:00+00:00",
            resolves_at="2026-09-04T01:00:00+00:00",
        )


def test_prediction_rejects_unknown_horizon() -> None:
    with pytest.raises(CryptoStoreError):
        Prediction(
            predictor="p", instrument="BTC-USD", horizon="7h", probability_up=0.5,
            reference_price=100.0, created_at="2026-09-04T00:00:00+00:00",
            resolves_at="2026-09-04T07:00:00+00:00",
        )


def test_prediction_rejects_non_positive_reference_price() -> None:
    with pytest.raises(CryptoStoreError):
        Prediction(
            predictor="p", instrument="BTC-USD", horizon="1h", probability_up=0.5,
            reference_price=0.0, created_at="2026-09-04T00:00:00+00:00",
            resolves_at="2026-09-04T01:00:00+00:00",
        )


def test_horizon_delta_known_and_unknown() -> None:
    assert horizon_delta("4h") == timedelta(hours=4)
    with pytest.raises(CryptoStoreError):
        horizon_delta("13m")


def _prediction(**overrides) -> Prediction:
    base = {
        "predictor": "baseline:momentum",
        "instrument": "BTC-USD",
        "horizon": "1h",
        "probability_up": 0.62,
        "reference_price": 100.0,
        "created_at": "2026-09-04T00:00:00+00:00",
        "resolves_at": "2026-09-04T01:00:00+00:00",
        "features": {"last_return": 0.004},
    }
    base.update(overrides)
    return Prediction(**base)


def test_duplicate_prediction_returns_the_same_id(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        first = record_prediction(connection, _prediction())
        second = record_prediction(connection, _prediction())
        assert first == second
        count = connection.execute("SELECT COUNT(*) AS n FROM crypto_predictions").fetchone()
        assert count["n"] == 1


def test_close_price_ignores_degraded_rows(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(
            connection,
            [
                _candle(
                    open_time="2026-09-04T01:00:00+00:00",
                    open=None, high=None, low=None, close=None, volume=None,
                    degraded=True, degraded_reason="unreachable",
                )
            ],
        )
        price, note = close_price_at(connection, "BTC-USD", "2026-09-04T01:00:00+00:00")
        assert price is None
        assert "no non-degraded" in note


def test_close_price_respects_the_tolerance_window(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, [_candle(open_time="2026-09-04T09:00:00+00:00", close=500.0)])
        price, _ = close_price_at(
            connection, "BTC-USD", "2026-09-04T01:00:00+00:00", tolerance_minutes=90
        )
        assert price is None


def _snapshots(connection, open_time: str, seen: dict[float, str], interval: str = "1h") -> None:
    """Store snapshots of one bar, each captured at the moment given."""

    for close, observed_at in seen.items():
        record_observations(
            connection, [_candle(interval=interval, open_time=open_time, close=close)], observed_at=observed_at
        )


def test_settlement_uses_the_first_price_captured_at_or_after_the_resolve_time(tmp_path: Path) -> None:
    """The live defect, 2026-09-28: snapshots of a forming bar all have
    times_seen = 1, so the old tie-break picked one arbitrarily, and 58% of 1h
    calls settled on a price captured before they resolved."""

    with connect(tmp_path / "c.db") as connection:
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {
            101.0: "2026-09-04T01:02:00+00:00",
            102.0: "2026-09-04T01:17:30+00:00",
            103.0: "2026-09-04T01:32:00+00:00",
        })

        exact, _ = close_price_at(connection, "BTC-USD", "2026-09-04T01:17:00+00:00")
        between, _ = close_price_at(connection, "BTC-USD", "2026-09-04T01:10:00+00:00")

        assert exact == 102.0
        assert between == 102.0


def test_settlement_waits_while_only_earlier_prices_exist(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {101.0: "2026-09-04T01:02:00+00:00"})

        price, note = close_price_at(
            connection, "BTC-USD", "2026-09-04T01:17:00+00:00", now_iso="2026-09-04T01:20:00+00:00"
        )

        assert price is None
        assert "waiting" in note


def test_settlement_falls_back_only_after_the_grace_period(tmp_path: Path) -> None:
    """From Codex's reviews: a call must not stay pending forever, but it must
    not give up the moment the tolerance passes either -- a collector
    recovering from an outage refetches the missing bar. Only after the grace
    does it settle on the latest earlier price, and it says so."""

    with connect(tmp_path / "c.db") as connection:
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {
            100.0: "2026-09-04T01:02:00+00:00",
            101.0: "2026-09-04T01:10:00+00:00",
        })
        at = "2026-09-04T01:17:00+00:00"

        # Still inside the refetch window (500 hourly bars): keep waiting.
        early, _ = close_price_at(connection, "BTC-USD", at, now_iso="2026-09-10T00:00:00+00:00")
        late, note = close_price_at(connection, "BTC-USD", at, now_iso="2026-10-01T00:00:00+00:00")

        assert early is None
        assert late == 101.0
        assert "latest price before" in note


def test_a_capture_from_before_the_bar_opened_is_not_evidence(tmp_path: Path) -> None:
    """From Codex's third review: clamping a future-dated bar's capture to its
    open invented a price at the open that nobody observed."""

    with connect(tmp_path / "c.db") as connection:
        # A 02:00 bar that a venue reported early, captured at 01:20.
        _snapshots(connection, "2026-09-04T02:00:00+00:00", {150.0: "2026-09-04T01:20:00+00:00"})

        price, note = close_price_at(
            connection, "BTC-USD", "2026-09-04T01:17:00+00:00", now_iso="2026-09-04T02:30:00+00:00"
        )

        assert price is None
        assert "waiting" in note


def test_an_out_of_order_refetch_never_moves_last_seen_backwards(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {100.0: "2026-09-04T01:40:00+00:00"})
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {100.0: "2026-09-04T01:10:00+00:00"})

        row = connection.execute("SELECT last_seen_at FROM crypto_observations").fetchone()

        assert row["last_seen_at"] == "2026-09-04T01:40:00+00:00"


def test_an_unchanged_refetch_proves_the_price_still_held(tmp_path: Path) -> None:
    """From Codex's second review: re-fetching an identical bar only updates
    last_seen_at, which still proves the close was the price at that moment."""

    with connect(tmp_path / "c.db") as connection:
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {100.0: "2026-09-04T01:02:00+00:00"})
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {100.0: "2026-09-04T01:32:00+00:00"})

        price, note = close_price_at(
            connection, "BTC-USD", "2026-09-04T01:17:00+00:00", now_iso="2026-09-04T01:33:00+00:00"
        )

        assert price == 100.0
        assert "01:32:00" in note


def test_settlement_looks_across_neighbouring_bars(tmp_path: Path) -> None:
    """From Codex's review: at 01:47 the 01:00 bar can hold a price captured at
    01:47 while the nearer-by-open-time 02:00 bar's first price comes later."""

    with connect(tmp_path / "c.db") as connection:
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {102.0: "2026-09-04T01:47:30+00:00"})
        _snapshots(connection, "2026-09-04T02:00:00+00:00", {105.0: "2026-09-04T02:02:00+00:00"})

        price, _ = close_price_at(connection, "BTC-USD", "2026-09-04T01:47:00+00:00")

        assert price == 102.0


def test_a_completed_bar_fetched_late_speaks_for_its_end_not_its_fetch_time(tmp_path: Path) -> None:
    """From Codex's review: after an outage, bars arrive long after they closed.
    A call resolving at 01:00 is settled by the bar that ENDS at 01:00 (the
    00:00 bar), not by the bar that opens at 01:00, whose close is an hour later."""

    with connect(tmp_path / "c.db") as connection:
        _snapshots(connection, "2026-09-04T00:00:00+00:00", {100.0: "2026-09-04T05:00:00+00:00"})
        _snapshots(connection, "2026-09-04T01:00:00+00:00", {107.0: "2026-09-04T05:00:00+00:00"})

        price, _ = close_price_at(connection, "BTC-USD", "2026-09-04T01:00:00+00:00")

        assert price == 100.0


def test_fast_settlement_matches_the_bar_training_uses(tmp_path: Path) -> None:
    """From Codex's review: fast training settles a round ending 12:05 on the
    12:04 bar's close. Live settlement picked the 12:05 bar (distance zero),
    one bar later. The 12:04 bar's final close is the price at 12:05."""

    with connect(tmp_path / "c.db") as connection:
        _snapshots(connection, "2026-09-06T12:04:00+00:00", {50_000.0: "2026-09-06T12:05:01+00:00"}, "1m")
        _snapshots(connection, "2026-09-06T12:05:00+00:00", {50_010.0: "2026-09-06T12:05:01+00:00"}, "1m")

        price, _ = close_price_at(
            connection, "BTC-USD", "2026-09-06T12:05:00+00:00", interval="1m", tolerance_minutes=2
        )

        assert price == 50_000.0


def test_settlement_marks_up_and_down_correctly(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(
            connection,
            [
                _candle(open_time="2026-09-04T01:00:00+00:00", close=120.0),
                _candle(instrument="ETH-USD", open_time="2026-09-04T01:00:00+00:00", close=80.0),
            ],
        )
        record_prediction(connection, _prediction())
        record_prediction(connection, _prediction(instrument="ETH-USD", predictor="baseline:coin_flip"))

        stats = settle_due_predictions(connection, now_iso="2026-09-04T02:00:00+00:00")
        assert stats == {"due": 2, "settled": 2, "unresolvable": 0}

        outcomes = {
            row["instrument"]: row["outcome_up"] for row in fetch_settled(connection)
        }
        assert outcomes == {"BTC-USD": 1, "ETH-USD": 0}


def test_settlement_leaves_unresolvable_predictions_pending(tmp_path: Path) -> None:
    """No price in the window means unsettled, never a guessed outcome."""

    with connect(tmp_path / "c.db") as connection:
        record_prediction(connection, _prediction())
        stats = settle_due_predictions(connection, now_iso="2026-09-04T02:00:00+00:00")
        assert stats == {"due": 1, "settled": 0, "unresolvable": 1}
        assert list(fetch_settled(connection)) == []


def test_settlement_ignores_predictions_that_are_not_due_yet(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, [_candle(open_time="2026-09-04T01:00:00+00:00", close=120.0)])
        record_prediction(connection, _prediction())
        stats = settle_due_predictions(connection, now_iso="2026-09-04T00:30:00+00:00")
        assert stats["due"] == 0


def test_settlement_is_idempotent(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, [_candle(open_time="2026-09-04T01:00:00+00:00", close=120.0)])
        record_prediction(connection, _prediction())
        settle_due_predictions(connection, now_iso="2026-09-04T02:00:00+00:00")
        again = settle_due_predictions(connection, now_iso="2026-09-04T03:00:00+00:00")
        assert again["due"] == 0


def test_fetch_settled_filters_by_predictor(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, [_candle(open_time="2026-09-04T01:00:00+00:00", close=120.0)])
        record_prediction(connection, _prediction())
        record_prediction(connection, _prediction(predictor="baseline:base_rate"))
        settle_due_predictions(connection, now_iso="2026-09-04T02:00:00+00:00")
        rows = fetch_settled(connection, predictor="baseline:base_rate")
        assert len(rows) == 1
        assert rows[0]["predictor"] == "baseline:base_rate"
