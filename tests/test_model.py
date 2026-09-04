import json
from pathlib import Path
import random
from typing import Sequence

import pytest

pytest.importorskip("torch")

from andy_trader.backtest import run_backtest
from andy_trader.baselines import default_baselines
from andy_trader.model import FEATURE_NAMES, TorchPredictor, engineer_features, record_live_prediction
from andy_trader.store import Candle, connect, record_observations


def _prices(count: int = 70) -> list[float]:
    rng = random.Random(1)
    values = [100.0]
    for _ in range(count - 1):
        direction = rng.choice((-1.0, 1.0))
        values.append(values[-1] * (1.0 + direction * (0.001 + rng.random() * 0.004)))
    return values


def _series(closes: Sequence[float]) -> list[Candle]:
    return [
        Candle(
            instrument="BTC-USD",
            venue="kraken",
            interval="1h",
            open_time=f"2026-08-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00",
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1.0,
        )
        for index, close in enumerate(closes)
    ]


def test_feature_engineering_has_stable_shape_and_finite_values() -> None:
    rows = engineer_features(_prices(40), lookback=12)
    assert len(rows) == 28
    assert all(len(row.values) == len(FEATURE_NAMES) for row in rows)
    assert all(value == pytest.approx(value) for row in rows for value in row.values)


def test_feature_engineering_never_changes_past_rows_when_future_arrives() -> None:
    original = _prices(40)
    prefix = engineer_features(original[:30], lookback=12)
    changed_future = original[:30] + [999.0] * 10
    full = engineer_features(changed_future, lookback=12)
    assert full[:len(prefix)] == prefix


def test_torch_predictor_is_reproducible_and_emits_a_probability() -> None:
    prices = _prices()
    first = TorchPredictor(seed=7, lookback=12, epochs=10)
    second = TorchPredictor(seed=7, lookback=12, epochs=10)
    first.fit(prices[:-1])
    second.fit(prices[:-1])
    first_probability = first(prices)
    second_probability = second(prices)
    assert first_probability == pytest.approx(second_probability, abs=1e-12)
    assert 0.0 <= first_probability <= 1.0


def test_live_model_prediction_uses_the_existing_audit_store(tmp_path: Path) -> None:
    predictor = TorchPredictor(seed=23, lookback=12, epochs=5)
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(_prices()))
        prediction_id = record_live_prediction(
            connection,
            instrument="BTC-USD",
            predictor=predictor,
            now_iso="2026-09-04T00:00:00+00:00",
        )
        row = connection.execute(
            "SELECT * FROM crypto_predictions WHERE id = ?", (prediction_id,)
        ).fetchone()

    features = json.loads(row["features_json"])
    assert row["predictor"] == "model:torch_mlp"
    assert row["reference_price"] == pytest.approx(_prices()[-1])
    assert 0.0 <= row["probability_up"] <= 1.0
    assert features["seed"] == 23
    assert features["feature_names"] == list(FEATURE_NAMES)


def test_model_comparison_records_the_honest_out_of_sample_loss(tmp_path: Path) -> None:
    """The candidate is not promoted merely because PyTorch training completed."""

    model = TorchPredictor(seed=1729, lookback=6, epochs=8)
    baselines = default_baselines(seed=1729)
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(_prices(55)))
        results = run_backtest(
            connection,
            instrument="BTC-USD",
            predictors=baselines + (model,),
            minimum_train_bars=24,
        )

    candidate = next(result for result in results if result.predictor == model.name)
    best_baseline = max(
        (result for result in results if result.predictor != model.name),
        key=lambda result: result.report.brier_skill_score,
    )
    assert candidate.report.brier_skill_score <= best_baseline.report.brier_skill_score
    assert candidate.net_return <= best_baseline.net_return
