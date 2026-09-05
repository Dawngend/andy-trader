"""Tests for walk-forward retraining, model registry persistence, and scheduling."""

import json
from pathlib import Path
import random
from typing import Sequence

import pytest

pytest.importorskip("torch")

from andy_trader.features import SignalObservation
from andy_trader.registry import (
    ModelRegistryEntry,
    fetch_latest_promoted_model,
    fetch_registry_entries,
    initialize_registry,
    record_registry_entry,
)
from andy_trader.signals import (
    FEAR_GREED,
    FUNDING_RATE,
    LONG_RATIO,
    OPEN_INTEREST,
    Signal,
    record_signals,
)
from andy_trader.store import Candle, connect, record_observations
from andy_trader.training import (
    MultimodalTorchPredictor,
    run_retrain_window,
    run_walk_forward_retraining,
    should_retrain,
)


def _synthetic_candles(count: int = 150) -> list[Candle]:
    rng = random.Random(1729)
    val = 100.0
    candles = []
    for i in range(count):
        iso = f"2026-08-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00"
        move = 0.003 if rng.random() > 0.48 else -0.003
        val *= 1.0 + move
        candles.append(
            Candle(
                instrument="BTC-USD",
                venue="bybit",
                interval="1h",
                open_time=iso,
                open=val,
                high=val + 0.5,
                low=val - 0.5,
                close=val,
                volume=10.0,
            )
        )
    return candles


def _synthetic_signals(candles: Sequence[Candle]) -> list[Signal]:
    signals = []
    for c in candles:
        signals.append(
            Signal(
                signal=FUNDING_RATE,
                source="bybit",
                instrument="BTC-USD",
                value=0.0001,
                observed_time=c.open_time,
            )
        )
        signals.append(
            Signal(
                signal=LONG_RATIO,
                source="bybit",
                instrument="BTC-USD",
                value=0.52,
                observed_time=c.open_time,
            )
        )
        signals.append(
            Signal(
                signal=OPEN_INTEREST,
                source="bybit",
                instrument="BTC-USD",
                value=50000.0,
                observed_time=c.open_time,
            )
        )
        signals.append(
            Signal(
                signal=FEAR_GREED,
                source="alternative.me",
                instrument=None,
                value=60.0,
                observed_time=c.open_time,
            )
        )
    return signals


def test_registry_schema_and_persistence(tmp_path: Path) -> None:
    db_file = tmp_path / "reg.db"
    with connect(db_file) as conn:
        initialize_registry(conn)

        entry = ModelRegistryEntry(
            model_id="torch_test_01",
            trained_at="2026-09-05T00:00:00+00:00",
            instrument="BTC-USD",
            interval="1h",
            horizon="1h",
            train_start_time="2026-08-01T00:00:00+00:00",
            train_end_time="2026-08-07T00:00:00+00:00",
            train_bars=168,
            holdout_start_time="2026-08-07T01:00:00+00:00",
            holdout_end_time="2026-08-08T00:00:00+00:00",
            holdout_bars=24,
            hyperparameters={"seed": 1729, "lr": 0.01},
            holdout_brier=0.2450,
            holdout_brier_reference=0.2500,
            holdout_brier_skill=0.0200,
            base_rate_brier_skill=0.0050,
            promoted=True,
            promotion_reason="Passed test gate",
        )

        pk = record_registry_entry(conn, entry)
        assert pk > 0

        entries = fetch_registry_entries(conn, instrument="BTC-USD")
        assert len(entries) == 1
        assert entries[0].model_id == "torch_test_01"
        assert entries[0].promoted is True
        assert entries[0].hyperparameters["seed"] == 1729

        promoted = fetch_latest_promoted_model(conn, "BTC-USD", "1h")
        assert promoted is not None
        assert promoted.model_id == "torch_test_01"

        # None promoted for different instrument
        assert fetch_latest_promoted_model(conn, "ETH-USD", "1h") is None


def test_multimodal_predictor_seed_reproducibility() -> None:
    history = [
        (f"2026-08-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00", 100.0 + i * 0.1)
        for i in range(40)
    ]
    signals = {
        FUNDING_RATE: [SignalObservation(FUNDING_RATE, history[0][0], 0.0001)],
        LONG_RATIO: [SignalObservation(LONG_RATIO, history[0][0], 0.52)],
    }

    m1 = MultimodalTorchPredictor(seed=123, lookback=12, epochs=5)
    m2 = MultimodalTorchPredictor(seed=123, lookback=12, epochs=5)

    m1.fit(history[:-1], signals)
    m2.fit(history[:-1], signals)

    p1 = m1.predict_one(history, signals)
    p2 = m2.predict_one(history, signals)

    assert p1 == pytest.approx(p2, abs=1e-12)
    assert 0.0 <= p1 <= 1.0


def test_should_retrain_schedule_evaluation(tmp_path: Path) -> None:
    db_file = tmp_path / "sched.db"
    with connect(db_file) as conn:
        candles = _synthetic_candles(60)
        record_observations(conn, candles[:30])

        # Initially, no model trained yet -> should retrain
        assert should_retrain(conn, instrument="BTC-USD", horizon="1h") is True

        # Record a dummy training entry covering up to candle 30
        last_time = candles[29].open_time
        entry = ModelRegistryEntry(
            model_id="m_dummy",
            trained_at="2026-09-01T00:00:00+00:00",
            instrument="BTC-USD",
            interval="1h",
            horizon="1h",
            train_start_time=candles[0].open_time,
            train_end_time=candles[20].open_time,
            train_bars=20,
            holdout_start_time=candles[21].open_time,
            holdout_end_time=last_time,
            holdout_bars=9,
            hyperparameters={},
            holdout_brier=0.25,
            holdout_brier_reference=0.25,
            holdout_brier_skill=0.0,
            base_rate_brier_skill=0.0,
            promoted=False,
            promotion_reason="None",
        )
        record_registry_entry(conn, entry)

        # Immediately after, no new bars -> False
        assert should_retrain(conn, instrument="BTC-USD", min_new_settled_bars=24) is False

        # Add 10 new bars -> still < 24 -> False
        record_observations(conn, candles[30:40])
        assert should_retrain(conn, instrument="BTC-USD", min_new_settled_bars=24) is False

        # Add 20 more bars (total 30 new bars >= 24) -> True
        record_observations(conn, candles[40:60])
        assert should_retrain(conn, instrument="BTC-USD", min_new_settled_bars=24) is True


def test_run_retrain_window_end_to_end(tmp_path: Path) -> None:
    db_file = tmp_path / "train.db"
    candles = _synthetic_candles(100)
    signals = _synthetic_signals(candles)

    with connect(db_file) as conn:
        record_observations(conn, candles)
        record_signals(conn, signals)

        entry = run_retrain_window(
            conn,
            instrument="BTC-USD",
            train_bars=50,
            holdout_bars=24,
            lookback=12,
            epochs=5,
            seed=7,
        )

        assert entry.train_bars == 50
        assert entry.holdout_bars == 24
        assert 0.0 <= entry.holdout_brier <= 1.0
        assert isinstance(entry.promoted, bool)
        assert len(entry.promotion_reason) > 0

        # Verify entry exists in DB
        rows = fetch_registry_entries(conn, instrument="BTC-USD")
        assert len(rows) == 1
        assert rows[0].model_id == entry.model_id


def test_walk_forward_retraining_rolls_across_history(tmp_path: Path) -> None:
    db_file = tmp_path / "wf.db"
    candles = _synthetic_candles(120)
    signals = _synthetic_signals(candles)

    with connect(db_file) as conn:
        record_observations(conn, candles)
        record_signals(conn, signals)

        entries = run_walk_forward_retraining(
            conn,
            instrument="BTC-USD",
            train_bars=40,
            holdout_bars=24,
            roll_bars=24,
            lookback=12,
            epochs=5,
            seed=1729,
        )

        # Total bars = 120. min_bars = 40 + 24 + 1 = 65.
        # Windows: 65, 89, 113 -> at least 3 windows
        assert len(entries) >= 3
        for e in entries:
            assert e.train_bars == 40
            assert e.holdout_bars == 24
