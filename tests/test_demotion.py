"""Regression tests for append-only live-performance model demotion."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from andy_trader.calibration import evaluate
from andy_trader.registry import ModelRegistryEntry, is_demoted, record_registry_entry
from andy_trader.store import Prediction, connect, record_prediction
from andy_trader.training import (
    LIVE_MODEL_PREDICTOR_NAME,
    MultimodalTorchPredictor,
    check_live_performance_and_demote,
    load_promoted_model,
    predict_with_promoted_model,
)


def _promote(
    connection: sqlite3.Connection,
    *,
    model_id: str = "promoted_model",
    weights_path: str | None = None,
) -> ModelRegistryEntry:
    entry = ModelRegistryEntry(
        model_id=model_id,
        trained_at="2026-09-05T00:00:00+00:00",
        instrument="BTC-USD",
        interval="1h",
        horizon="1h",
        train_start_time="2026-08-28T00:00:00+00:00",
        train_end_time="2026-09-04T00:00:00+00:00",
        train_bars=168,
        holdout_start_time="2026-09-04T01:00:00+00:00",
        holdout_end_time="2026-09-05T00:00:00+00:00",
        holdout_bars=24,
        hyperparameters={},
        holdout_brier=0.24,
        holdout_brier_reference=0.25,
        holdout_brier_skill=0.04,
        base_rate_brier_skill=0.0,
        promoted=True,
        promotion_reason="cleared holdout gate",
        weights_path=weights_path,
    )
    record_registry_entry(connection, entry)
    return entry


def _settled_calls(
    connection: sqlite3.Connection,
    *,
    model_id: str,
    outcomes: list[int],
    model_probabilities: list[float],
    base_rate_probabilities: list[float],
    start: datetime | None = None,
) -> None:
    start = start or datetime(2026, 9, 5, tzinfo=UTC)
    for index, (outcome, model_probability, base_rate_probability) in enumerate(
        zip(outcomes, model_probabilities, base_rate_probabilities, strict=True)
    ):
        created_at = (start + timedelta(hours=index)).isoformat()
        resolves_at = (start + timedelta(hours=index + 1)).isoformat()
        for predictor, probability, features in (
            (
                LIVE_MODEL_PREDICTOR_NAME,
                model_probability,
                {"underlying_model_id": model_id, "interval": "1h"},
            ),
            ("baseline:base_rate", base_rate_probability, {"interval": "1h"}),
        ):
            prediction_id = record_prediction(
                connection,
                Prediction(
                    predictor=predictor,
                    instrument="BTC-USD",
                    horizon="1h",
                    probability_up=probability,
                    reference_price=100.0,
                    created_at=created_at,
                    resolves_at=resolves_at,
                    features=features,
                ),
            )
            connection.execute(
                """
                UPDATE crypto_predictions
                SET settled_at = ?, settle_price = ?, outcome_up = ?, settle_note = 'test'
                WHERE id = ?
                """,
                (resolves_at, 101.0 if outcome else 99.0, outcome, prediction_id),
            )
    connection.commit()


def _alternating_outcomes(count: int) -> list[int]:
    return [index % 2 for index in range(count)]


def _correct_probabilities(outcomes: list[int]) -> list[float]:
    return [0.9 if outcome else 0.1 for outcome in outcomes]


def _wrong_probabilities(outcomes: list[int]) -> list[float]:
    return [0.1 if outcome else 0.9 for outcome in outcomes]


def test_no_promoted_model_is_a_safe_noop(tmp_path: Path) -> None:
    with connect(tmp_path / "none.db") as connection:
        result = check_live_performance_and_demote(connection, instrument="BTC-USD")

    assert result == {
        "demoted": False,
        "status": "no_promoted_model",
        "reason": "no promoted model for this pair yet",
    }


def test_live_model_that_still_beats_base_rate_is_not_demoted(tmp_path: Path) -> None:
    with connect(tmp_path / "holding.db") as connection:
        entry = _promote(connection)

        # Old calls share the stable predictor name but belong to another model.
        # They are deliberately awful; blending them would wrongly demote the
        # current model even though its own 30-call live record is strong.
        old_outcomes = _alternating_outcomes(40)
        _settled_calls(
            connection,
            model_id="older_model",
            outcomes=old_outcomes,
            model_probabilities=_wrong_probabilities(old_outcomes),
            base_rate_probabilities=_correct_probabilities(old_outcomes),
            start=datetime(2026, 9, 1, tzinfo=UTC),
        )
        outcomes = _alternating_outcomes(30)
        _settled_calls(
            connection,
            model_id=entry.model_id,
            outcomes=outcomes,
            model_probabilities=[0.65 if outcome else 0.35 for outcome in outcomes],
            base_rate_probabilities=[0.5] * len(outcomes),
            start=datetime(2026, 10, 1, tzinfo=UTC),
        )

        result = check_live_performance_and_demote(connection, instrument="BTC-USD")

        assert result["status"] == "still_clears_bar"
        assert result["live_call_count"] == 30
        assert result["live_skill"] > result["base_rate_live_skill"]
        assert is_demoted(connection, model_id=entry.model_id) is False


def test_failing_model_is_demoted_once_and_cannot_be_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    weights_path = tmp_path / "promoted.pt"
    weights_path.touch()
    with connect(tmp_path / "failing.db") as connection:
        entry = _promote(connection, weights_path=str(weights_path))
        outcomes = _alternating_outcomes(36)
        _settled_calls(
            connection,
            model_id=entry.model_id,
            outcomes=outcomes,
            model_probabilities=_wrong_probabilities(outcomes),
            base_rate_probabilities=[0.5] * len(outcomes),
        )

        result = check_live_performance_and_demote(
            connection,
            instrument="BTC-USD",
            now_iso="2026-09-06T12:00:00+00:00",
        )

        assert result["status"] == "newly_demoted"
        assert result["demoted"] is True
        row = connection.execute("SELECT * FROM model_demotions").fetchone()
        assert row is not None
        assert row["model_id"] == entry.model_id
        assert row["live_call_count"] == 36
        assert row["live_skill"] == pytest.approx(result["live_skill"])
        assert row["base_rate_live_skill"] == pytest.approx(result["base_rate_live_skill"])

        def fail_if_loaded(path: Path) -> MultimodalTorchPredictor:
            raise AssertionError(f"demoted weights must not be loaded: {path}")

        monkeypatch.setattr(MultimodalTorchPredictor, "load", fail_if_loaded)
        assert load_promoted_model(connection, instrument="BTC-USD") is None
        prediction = predict_with_promoted_model(connection, instrument="BTC-USD")
        assert prediction["predicted"] is False
        assert "demoted" in str(prediction["reason"])

        repeated = check_live_performance_and_demote(connection, instrument="BTC-USD")
        assert repeated["status"] == "already_demoted"
        assert connection.execute("SELECT COUNT(*) FROM model_demotions").fetchone()[0] == 1

        registry_row = connection.execute(
            "SELECT promoted FROM model_registry WHERE model_id = ?", (entry.model_id,)
        ).fetchone()
        assert registry_row["promoted"] == 1


def test_too_few_bad_calls_do_not_demote(tmp_path: Path) -> None:
    with connect(tmp_path / "too_few.db") as connection:
        entry = _promote(connection)
        outcomes = _alternating_outcomes(29)
        _settled_calls(
            connection,
            model_id=entry.model_id,
            outcomes=outcomes,
            model_probabilities=_wrong_probabilities(outcomes),
            base_rate_probabilities=[0.5] * len(outcomes),
        )

        result = check_live_performance_and_demote(connection, instrument="BTC-USD")

        assert result["status"] == "not_enough_calls"
        assert result["live_call_count"] == 29
        assert is_demoted(connection, model_id=entry.model_id) is False


def test_degenerate_live_sample_does_not_demote(tmp_path: Path) -> None:
    with connect(tmp_path / "degenerate.db") as connection:
        entry = _promote(connection)
        outcomes = [1] * 30
        _settled_calls(
            connection,
            model_id=entry.model_id,
            outcomes=outcomes,
            model_probabilities=[0.05] * len(outcomes),
            base_rate_probabilities=[1.0] * len(outcomes),
        )

        result = check_live_performance_and_demote(connection, instrument="BTC-USD")

        assert result["status"] == "degenerate"
        assert result["live_call_count"] == 30
        assert is_demoted(connection, model_id=entry.model_id) is False


def test_real_case_shape_demotes_on_computed_live_skill_relation(tmp_path: Path) -> None:
    """Regression shape: 69 settled promoted calls that trail live base rate."""
    with connect(tmp_path / "real_shape.db") as connection:
        entry = _promote(connection, model_id="btc_promoted_24_bar_winner")
        outcomes = _alternating_outcomes(69)
        model_probabilities = _wrong_probabilities(outcomes)
        base_rate_probability = sum(outcomes) / len(outcomes)
        base_rate_probabilities = [base_rate_probability] * len(outcomes)
        _settled_calls(
            connection,
            model_id=entry.model_id,
            outcomes=outcomes,
            model_probabilities=model_probabilities,
            base_rate_probabilities=base_rate_probabilities,
        )

        result = check_live_performance_and_demote(connection, instrument="BTC-USD")
        expected_model = evaluate(model_probabilities, outcomes)
        expected_base_rate = evaluate(base_rate_probabilities, outcomes)
        row = connection.execute(
            "SELECT * FROM model_demotions WHERE model_id = ?", (entry.model_id,)
        ).fetchone()

        assert result["demoted"] is True
        assert row is not None
        assert row["live_call_count"] == 69
        assert row["live_skill"] == pytest.approx(expected_model.brier_skill_score)
        assert row["base_rate_live_skill"] == pytest.approx(
            expected_base_rate.brier_skill_score
        )
        assert row["live_skill"] <= row["base_rate_live_skill"]
