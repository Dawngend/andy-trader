"""Walk-forward retraining loop, promotion evaluation, and model registry recording.

This module implements CT-07:
1. Strict walk-forward rolling window training and out-of-sample holdout evaluation.
2. Multimodal feature extraction with backward as-of signal joins.
3. Pure promotion gate against baseline:base_rate over the identical holdout window.
4. Complete audit persistence into the SQLite model_registry.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

from andy_trader.backtest import _step_bars
from andy_trader.baselines import base_rate
from andy_trader.calibration import CalibrationReport, evaluate
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.features import (
    MULTIMODAL_FEATURE_NAMES,
    SignalObservation,
    engineer_multimodal_features,
    load_signals_for_series,
)
from andy_trader.model import ModelError, _torch_modules
from andy_trader.predict import load_closes
from andy_trader.promotion import DEFAULT_MINIMUM_HOLDOUT_BARS, evaluate_promotion_gate
from andy_trader.registry import (
    ModelRegistryEntry,
    fetch_latest_promoted_model,
    fetch_registry_entries,
    initialize_registry,
    record_registry_entry,
)
from andy_trader.store import connect, default_database_path, utc_now_iso

DEFAULT_SEED = 1729
DEFAULT_LOOKBACK = 24
DEFAULT_TRAIN_BARS = 168  # 7 days of 1h bars
DEFAULT_HOLDOUT_BARS = 24  # 24 hours holdout
DEFAULT_RETRAIN_INTERVAL_BARS = 24
MODELS_DIR = REPO_ROOT / "models"  # gitignored, same treatment as the .db file


@dataclass
class MultimodalTorchPredictor:
    """CPU/GPU deterministic MLP fitted on multimodal trailing features."""

    seed: int = DEFAULT_SEED
    lookback: int = DEFAULT_LOOKBACK
    epochs: int = 40
    learning_rate: float = 0.01
    name: str = "model:multimodal_mlp"
    _network: Any = field(default=None, init=False, repr=False)
    _means: tuple[float, ...] = field(default=(), init=False, repr=False)
    _scales: tuple[float, ...] = field(default=(), init=False, repr=False)
    _temperature: float = field(default=1.0, init=False, repr=False)

    @property
    def feature_count(self) -> int:
        return len(MULTIMODAL_FEATURE_NAMES)

    @property
    def temperature(self) -> float:
        return self._temperature

    def fit(
        self,
        history: Sequence[tuple[str, float]],
        signals: Mapping[str, Sequence[SignalObservation]],
    ) -> None:
        """Fit on labelled bars strictly within the provided history slice."""
        torch, nn = _torch_modules()
        rows = engineer_multimodal_features(history, signals, lookback=self.lookback)
        labelled = rows[:-1]
        if len(labelled) < 8:
            raise ModelError(
                f"Need at least 8 labelled feature rows, got {len(labelled)} from {len(history)} closes"
            )

        features = [row.values for row in labelled]
        outcomes = [
            1.0 if history[row.close_index + 1][1] > history[row.close_index][1] else 0.0
            for row in labelled
        ]

        calibration_count = max(4, len(features) // 5)
        split = len(features) - calibration_count
        train_features = features[:split]
        calibration_features = features[split:]
        train_outcomes = outcomes[:split]
        calibration_outcomes = outcomes[split:]

        columns = list(zip(*train_features))
        means = [sum(column) / len(column) for column in columns]
        scales = []
        for column, mean in zip(columns, means):
            variance = sum((value - mean) ** 2 for value in column) / len(column)
            scales.append(max(math.sqrt(variance), 1e-8))
        self._means = tuple(means)
        self._scales = tuple(scales)

        def normalise(values: Sequence[Sequence[float]]) -> list[list[float]]:
            return [
                [(value - mean) / scale for value, mean, scale in zip(row, means, scales)]
                for row in values
            ]

        torch.manual_seed(self.seed)
        torch.use_deterministic_algorithms(True)
        network = nn.Sequential(
            nn.Linear(self.feature_count, 16),
            nn.Tanh(),
            nn.Linear(16, 8),
            nn.Tanh(),
            nn.Linear(8, 1),
        )
        train_x = torch.tensor(normalise(train_features), dtype=torch.float32)
        train_y = torch.tensor(train_outcomes, dtype=torch.float32).reshape(-1, 1)
        optimizer = torch.optim.Adam(network.parameters(), lr=self.learning_rate)
        loss_fn = nn.BCEWithLogitsLoss()
        network.train()
        for _ in range(self.epochs):
            optimizer.zero_grad()
            loss = loss_fn(network(train_x), train_y)
            loss.backward()
            optimizer.step()

        network.eval()
        calibration_x = torch.tensor(normalise(calibration_features), dtype=torch.float32)
        calibration_y = torch.tensor(calibration_outcomes, dtype=torch.float32).reshape(-1, 1)
        with torch.no_grad():
            calibration_logits = network(calibration_x).detach()

        log_temperature = torch.zeros(1, requires_grad=True)
        temperature_optimizer = torch.optim.Adam([log_temperature], lr=0.05)
        for _ in range(60):
            temperature_optimizer.zero_grad()
            temperature = torch.exp(log_temperature).clamp(0.1, 10.0)
            calibration_loss = loss_fn(calibration_logits / temperature, calibration_y)
            calibration_loss.backward()
            temperature_optimizer.step()
            with torch.no_grad():
                log_temperature.clamp_(math.log(0.1), math.log(10.0))

        self._network = network
        self._temperature = float(torch.exp(log_temperature.detach()).clamp(0.1, 10.0).item())

    def predict_one(
        self,
        history: Sequence[tuple[str, float]],
        signals: Mapping[str, Sequence[SignalObservation]],
    ) -> float:
        """Predict probability up for the final close in history using trailing data."""
        if self._network is None:
            raise ModelError("MultimodalTorchPredictor must be fitted before prediction")
        rows = engineer_multimodal_features(history, signals, lookback=self.lookback)
        if not rows:
            raise ModelError(f"Need more than {self.lookback} closes to predict")

        torch, _ = _torch_modules()
        values = rows[-1].values
        normalised = [
            (value - mean) / scale
            for value, mean, scale in zip(values, self._means, self._scales)
        ]
        inputs = torch.tensor([normalised], dtype=torch.float32)
        self._network.eval()
        with torch.no_grad():
            logit = self._network(inputs) / self._temperature
            return float(torch.sigmoid(logit).item())

    def save(self, path: Path) -> None:
        """Persist weights and everything needed to reconstruct this exact predictor.

        Without this, `model_registry.weights_path` was always None and a
        promoted model could never actually be loaded and used -- the
        registry recorded that a model *would have* traded, not a model
        that *could*. Only ever called for a candidate that has already
        cleared the promotion gate; an unpromoted model's weights are
        never useful to load, so they are never saved.
        """

        torch, _ = _torch_modules()
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "state_dict": self._network.state_dict(),
                "means": self._means,
                "scales": self._scales,
                "temperature": self._temperature,
                "seed": self.seed,
                "lookback": self.lookback,
                "epochs": self.epochs,
                "learning_rate": self.learning_rate,
                "feature_count": self.feature_count,
            },
            path,
        )

    @classmethod
    def load(cls, path: Path) -> "MultimodalTorchPredictor":
        """Reconstruct a fitted predictor from a file written by `save`.

        Rebuilds the exact same architecture shape before loading the state
        dict -- PyTorch's `load_state_dict` matches by parameter name and
        shape, not by re-deriving the network topology, so the constructor
        call here must exactly mirror `fit`'s `nn.Sequential`.
        """

        torch, nn = _torch_modules()
        payload = torch.load(path, weights_only=False)
        predictor = cls(
            seed=payload["seed"],
            lookback=payload["lookback"],
            epochs=payload["epochs"],
            learning_rate=payload["learning_rate"],
        )
        network = nn.Sequential(
            nn.Linear(payload["feature_count"], 16),
            nn.Tanh(),
            nn.Linear(16, 8),
            nn.Tanh(),
            nn.Linear(8, 1),
        )
        network.load_state_dict(payload["state_dict"])
        network.eval()
        predictor._network = network
        predictor._means = tuple(payload["means"])
        predictor._scales = tuple(payload["scales"])
        predictor._temperature = float(payload["temperature"])
        return predictor


def should_retrain(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    horizon: str = "1h",
    interval: str = "1h",
    min_new_settled_bars: int = DEFAULT_RETRAIN_INTERVAL_BARS,
) -> bool:
    """Determine whether enough fresh data has settled to trigger a retraining run."""
    entries = fetch_registry_entries(
        connection,
        instrument=instrument,
        horizon=horizon,
        interval=interval,
        limit=1,
    )
    if not entries:
        # Never trained before: check if sufficient history exists
        history = load_closes(connection, instrument, interval=interval, limit=1)
        return len(history) > 0

    last_entry = entries[0]
    # Count how many non-degraded closes have open_time > last_entry.holdout_end_time
    count_row = connection.execute(
        """
        SELECT COUNT(DISTINCT open_time) AS new_bars
        FROM crypto_observations
        WHERE instrument = ? AND interval = ? AND degraded = 0 AND close IS NOT NULL
          AND open_time > ?
        """,
        (instrument, interval, last_entry.holdout_end_time),
    ).fetchone()
    new_bars = int(count_row["new_bars"]) if count_row else 0
    return new_bars >= min_new_settled_bars


def run_retrain_window(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    interval: str = "1h",
    horizon: str = "1h",
    train_bars: int = DEFAULT_TRAIN_BARS,
    holdout_bars: int = DEFAULT_HOLDOUT_BARS,
    seed: int = DEFAULT_SEED,
    epochs: int = 40,
    learning_rate: float = 0.01,
    lookback: int = DEFAULT_LOOKBACK,
    end_of_holdout_iso: str | None = None,
) -> ModelRegistryEntry:
    """Execute one training and holdout evaluation pass and write to model_registry.

    The window layout:
    [train_start ... train_end] -> [holdout_start ... holdout_end] -> [settle]
    """
    step = _step_bars(interval, horizon)
    min_bars_needed = train_bars + holdout_bars + step
    total_history = load_closes(
        connection, instrument, interval=interval, limit=2_147_483_647
    )

    if end_of_holdout_iso is not None:
        total_history = [
            (t, c) for t, c in total_history if t <= end_of_holdout_iso
        ]

    if len(total_history) < min_bars_needed:
        raise ModelError(
            f"Need at least {min_bars_needed} bars for train({train_bars}) + "
            f"holdout({holdout_bars}) + step({step}), got {len(total_history)}"
        )

    # Slice indices for the rolling window
    holdout_end_idx = len(total_history) - step
    holdout_start_idx = holdout_end_idx - holdout_bars
    train_end_idx = holdout_start_idx
    train_start_idx = train_end_idx - train_bars

    if train_start_idx < 0:
        raise ModelError(f"Insufficient history for train_bars {train_bars}")

    train_slice = total_history[train_start_idx:train_end_idx]
    holdout_slice = total_history[holdout_start_idx:holdout_end_idx]

    train_start_time = train_slice[0][0]
    train_end_time = train_slice[-1][0]
    holdout_start_time = holdout_slice[0][0]
    holdout_end_time = holdout_slice[-1][0]

    # Pre-fetch signals up to holdout_end_time
    signals = load_signals_for_series(
        connection, instrument=instrument, until_iso=holdout_end_time
    )

    # Fit candidate model on train slice
    predictor = MultimodalTorchPredictor(
        seed=seed,
        lookback=lookback,
        epochs=epochs,
        learning_rate=learning_rate,
    )
    predictor.fit(train_slice, signals)

    # Out-of-sample holdout prediction pass
    candidate_probs: list[float] = []
    base_rate_probs: list[float] = []
    outcomes: list[int] = []

    for k in range(holdout_start_idx, holdout_end_idx):
        # Anti-lookahead boundary: visible history up to k
        visible_history = total_history[train_start_idx : k + 1]
        ref_price = total_history[k][1]
        settle_price = total_history[k + step][1]
        outcome = 1 if settle_price > ref_price else 0

        # Candidate prediction
        c_prob = predictor.predict_one(visible_history, signals)

        # Baseline base_rate prediction over identical visible history
        b_prob = base_rate([c for _, c in visible_history])

        candidate_probs.append(c_prob)
        base_rate_probs.append(b_prob)
        outcomes.append(outcome)

    candidate_report = evaluate(candidate_probs, outcomes)
    base_rate_report = evaluate(base_rate_probs, outcomes)

    decision = evaluate_promotion_gate(
        candidate_report=candidate_report,
        base_rate_report=base_rate_report,
        minimum_holdout_bars=DEFAULT_MINIMUM_HOLDOUT_BARS,
    )

    now = utc_now_iso()
    stamp_id = now.replace("-", "").replace(":", "").replace("+", "").replace(".", "_")
    model_id = f"torch_mlp_{instrument.lower()}_{stamp_id}_s{seed}"

    # Only a promoted candidate's weights are ever saved. An unpromoted
    # model's weights would never be loaded by anything -- the registry
    # already keeps its score for the audit trail, which is all a rejected
    # candidate needs.
    weights_path: str | None = None
    if decision.promoted:
        weights_file = MODELS_DIR / f"{model_id}.pt"
        predictor.save(weights_file)
        weights_path = str(weights_file)

    entry = ModelRegistryEntry(
        model_id=model_id,
        trained_at=now,
        instrument=instrument,
        interval=interval,
        horizon=horizon,
        train_start_time=train_start_time,
        train_end_time=train_end_time,
        train_bars=len(train_slice),
        holdout_start_time=holdout_start_time,
        holdout_end_time=holdout_end_time,
        holdout_bars=len(holdout_slice),
        hyperparameters={
            "seed": seed,
            "lookback": lookback,
            "epochs": epochs,
            "learning_rate": learning_rate,
            "feature_names": list(MULTIMODAL_FEATURE_NAMES),
            "calibration_temperature": predictor.temperature,
        },
        holdout_brier=candidate_report.brier,
        holdout_brier_reference=candidate_report.brier_reference,
        holdout_brier_skill=candidate_report.brier_skill_score,
        base_rate_brier_skill=base_rate_report.brier_skill_score,
        promoted=decision.promoted,
        promotion_reason=decision.reason,
        weights_path=weights_path,
    )

    record_registry_entry(connection, entry)
    return entry


def load_promoted_model(
    connection: sqlite3.Connection, *, instrument: str, horizon: str = "1h", interval: str = "1h"
) -> MultimodalTorchPredictor | None:
    """Load the most recently promoted model for live use, or None if there isn't one.

    Returns None (never raises) whenever there is nothing to load: no model
    has ever been promoted for this pair, or the on-disk weights file is
    missing (e.g. moved, or trained on a machine other than this one). A
    missing model is simply a reason not to trade it, not a crash.
    """

    entry = fetch_latest_promoted_model(connection, instrument, horizon, interval=interval)
    if entry is None or not entry.weights_path:
        return None
    weights_file = Path(entry.weights_path)
    if not weights_file.exists():
        return None
    return MultimodalTorchPredictor.load(weights_file)


def run_walk_forward_retraining(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    interval: str = "1h",
    horizon: str = "1h",
    train_bars: int = DEFAULT_TRAIN_BARS,
    holdout_bars: int = DEFAULT_HOLDOUT_BARS,
    roll_bars: int = DEFAULT_RETRAIN_INTERVAL_BARS,
    seed: int = DEFAULT_SEED,
    epochs: int = 40,
    learning_rate: float = 0.01,
    lookback: int = DEFAULT_LOOKBACK,
) -> list[ModelRegistryEntry]:
    """Execute historical walk-forward retraining across all available windows.

    Rolls forward by roll_bars after each holdout evaluation.
    """
    step = _step_bars(interval, horizon)
    min_bars_needed = train_bars + holdout_bars + step
    total_history = load_closes(
        connection, instrument, interval=interval, limit=2_147_483_647
    )
    if len(total_history) < min_bars_needed:
        raise ModelError(
            f"Need at least {min_bars_needed} bars for walk-forward, got {len(total_history)}"
        )

    entries: list[ModelRegistryEntry] = []
    # Start at the first point with full train + holdout
    current_end = min_bars_needed
    while current_end <= len(total_history):
        end_time = total_history[current_end - 1][0]
        entry = run_retrain_window(
            connection,
            instrument=instrument,
            interval=interval,
            horizon=horizon,
            train_bars=train_bars,
            holdout_bars=holdout_bars,
            seed=seed,
            epochs=epochs,
            learning_rate=learning_rate,
            lookback=lookback,
            end_of_holdout_iso=end_time,
        )
        entries.append(entry)
        current_end += roll_bars

    return entries


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", default="BTC-USD", help="Instrument to retrain")
    parser.add_argument("--interval", default="1h", help="Candle interval")
    parser.add_argument("--horizon", default="1h", help="Prediction horizon")
    parser.add_argument("--train-bars", type=int, default=DEFAULT_TRAIN_BARS)
    parser.add_argument("--holdout-bars", type=int, default=DEFAULT_HOLDOUT_BARS)
    parser.add_argument("--roll-bars", type=int, default=DEFAULT_RETRAIN_INTERVAL_BARS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--walk-forward", action="store_true", help="Run full historical walk-forward")
    parser.add_argument("--check-schedule", action="store_true", help="Check if retrain is due")
    parser.add_argument("--database", help="Override database path")
    parser.add_argument("--json", action="store_true", help="Output JSON format")
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    db_path = Path(args.database) if args.database else default_database_path()

    with connect(db_path) as connection:
        initialize_registry(connection)

        if args.check_schedule:
            due = should_retrain(
                connection,
                instrument=args.instrument,
                horizon=args.horizon,
                interval=args.interval,
                min_new_settled_bars=args.roll_bars,
            )
            print(json.dumps({"due": due}) if args.json else f"Retrain due: {due}")
            return 0

        if args.walk_forward:
            entries = run_walk_forward_retraining(
                connection,
                instrument=args.instrument,
                interval=args.interval,
                horizon=args.horizon,
                train_bars=args.train_bars,
                holdout_bars=args.holdout_bars,
                roll_bars=args.roll_bars,
                seed=args.seed,
            )
            promoted_count = sum(1 for e in entries if e.promoted)
            if args.json:
                print(json.dumps([e.__dict__ for e in entries], indent=2))
            else:
                print(f"Completed {len(entries)} retraining windows; {promoted_count} promoted.")
                for entry in entries:
                    status = "PROMOTED" if entry.promoted else "REJECTED"
                    print(
                        f"  [{status}] {entry.model_id}: holdout skill={entry.holdout_brier_skill:+.4f} "
                        f"vs base_rate={entry.base_rate_brier_skill:+.4f}"
                    )
            return 0

        # Default single retrain window
        entry = run_retrain_window(
            connection,
            instrument=args.instrument,
            interval=args.interval,
            horizon=args.horizon,
            train_bars=args.train_bars,
            holdout_bars=args.holdout_bars,
            seed=args.seed,
        )
        if args.json:
            print(json.dumps(entry.__dict__, indent=2))
        else:
            status = "PROMOTED" if entry.promoted else "REJECTED"
            print(
                f"[{status}] {entry.model_id} (holdout skill={entry.holdout_brier_skill:+.4f}, "
                f"base_rate={entry.base_rate_brier_skill:+.4f})"
            )
            print(f"  Reason: {entry.promotion_reason}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
