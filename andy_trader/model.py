"""Experimental deterministic PyTorch predictor with trailing-only crypto features."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Sequence

from andy_trader.baselines import PredictionContext
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.predict import load_closes
from andy_trader.store import (
    Prediction,
    connect,
    default_database_path,
    horizon_delta,
    record_prediction,
    utc_now_iso,
)

DEFAULT_SEED = 1729
DEFAULT_LOOKBACK = 24
FEATURE_NAMES = (
    "return_1",
    "return_3",
    "return_6",
    "mean_return",
    "return_volatility",
    "ema_12_distance",
)


class ModelError(RuntimeError):
    """Raised when the optional model cannot train or make an honest prediction."""


@dataclass(frozen=True)
class FeatureRow:
    """Features known at one close index, with no outcome attached."""

    close_index: int
    values: tuple[float, ...]


def _torch_modules() -> tuple[Any, Any]:
    try:
        import torch
        from torch import nn
    except ImportError as exc:
        raise ModelError(
            "PyTorch is optional; install the model section of requirements.txt to use CT-05"
        ) from exc
    return torch, nn


def _return(start: float, end: float) -> float:
    if start <= 0 or end <= 0:
        raise ModelError("Feature prices must be positive")
    return math.log(end / start)


def _ema(values: Sequence[float], period: int) -> float:
    multiplier = 2.0 / (period + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = (float(value) - result) * multiplier + result
    return result


def engineer_features(
    closes: Sequence[float], *, lookback: int = DEFAULT_LOOKBACK
) -> list[FeatureRow]:
    """Build one vector per eligible bar from that bar and its trailing history only."""

    if lookback < 6:
        raise ModelError("lookback must be at least 6")
    prices = [float(close) for close in closes]
    if any(not math.isfinite(close) or close <= 0 for close in prices):
        raise ModelError("Feature prices must be finite and positive")

    rows: list[FeatureRow] = []
    for index in range(lookback, len(prices)):
        trailing = prices[index - lookback:index + 1]
        returns = [_return(previous, current) for previous, current in zip(trailing, trailing[1:])]
        mean_return = sum(returns) / len(returns)
        variance = sum((value - mean_return) ** 2 for value in returns) / len(returns)
        ema_12 = _ema(trailing, 12)
        rows.append(
            FeatureRow(
                close_index=index,
                values=(
                    _return(prices[index - 1], prices[index]),
                    _return(prices[index - 3], prices[index]),
                    _return(prices[index - 6], prices[index]),
                    mean_return,
                    math.sqrt(variance),
                    prices[index] / ema_12 - 1.0,
                ),
            )
        )
    return rows


@dataclass
class TorchPredictor:
    """Small CPU MLP whose sigmoid is temperature-calibrated chronologically."""

    seed: int = DEFAULT_SEED
    lookback: int = DEFAULT_LOOKBACK
    epochs: int = 40
    learning_rate: float = 0.01
    name: str = "model:torch_mlp"
    _network: Any = field(default=None, init=False, repr=False)
    _means: tuple[float, ...] = field(default=(), init=False, repr=False)
    _scales: tuple[float, ...] = field(default=(), init=False, repr=False)
    _temperature: float = field(default=1.0, init=False, repr=False)

    @property
    def minimum_history(self) -> int:
        # Eight labelled rows are the minimum fit. Prediction needs one current
        # unlabelled row beyond those, and fit() deliberately withholds its own
        # final row because that row's outcome is not inside the fit slice.
        return self.lookback + 10

    @property
    def prediction_name(self) -> str:
        return self.name

    @property
    def temperature(self) -> float:
        return self._temperature

    def fit(self, closes: Sequence[float]) -> None:
        """Fit on labelled bars strictly before the backtest evaluation bar."""

        torch, nn = _torch_modules()
        rows = engineer_features(closes, lookback=self.lookback)
        labelled = rows[:-1]
        if len(labelled) < 8:
            raise ModelError(
                f"Need at least 8 labelled feature rows, got {len(labelled)} from {len(closes)} closes"
            )
        features = [row.values for row in labelled]
        outcomes = [
            1.0 if closes[row.close_index + 1] > closes[row.close_index] else 0.0
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

        # Each walk-forward fit starts from the recorded seed. Reusing trained
        # weights would make a result depend on how many earlier windows ran.
        torch.manual_seed(self.seed)
        torch.use_deterministic_algorithms(True)
        network = nn.Sequential(
            nn.Linear(len(FEATURE_NAMES), 8),
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

        # Temperature scaling changes confidence without changing direction. It
        # is fitted only on the chronological tail held out from network fitting,
        # so the reported reliability is not trained on the evaluation window.
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

    def __call__(
        self,
        closes: Sequence[float],
        *,
        context: PredictionContext | None = None,
    ) -> float:
        if self._network is None:
            raise ModelError("TorchPredictor must be fitted before prediction")
        rows = engineer_features(closes, lookback=self.lookback)
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


def record_live_prediction(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    horizon: str = "1h",
    interval: str = "1h",
    predictor: TorchPredictor | None = None,
    now_iso: str | None = None,
    mode: str = "advisory",
) -> int:
    """Fit on prior closes and append one advisory model call to the audit store."""

    active = predictor or TorchPredictor()
    history = load_closes(connection, instrument, interval=interval)
    if len(history) < active.minimum_history:
        raise ModelError(
            f"{instrument} has {len(history)} closes; need at least {active.minimum_history}"
        )
    closes = [close for _, close in history]
    active.fit(closes[:-1])
    probability = active(closes)
    now = now_iso or utc_now_iso()
    resolves_at = (datetime.fromisoformat(now) + horizon_delta(horizon)).isoformat()
    return record_prediction(
        connection,
        Prediction(
            predictor=active.name,
            instrument=instrument,
            horizon=horizon,
            probability_up=probability,
            reference_price=closes[-1],
            created_at=now,
            resolves_at=resolves_at,
            mode=mode,
            features={
                "seed": active.seed,
                "interval": interval,
                "lookback": active.lookback,
                "epochs": active.epochs,
                "feature_names": list(FEATURE_NAMES),
                "history_bars": len(closes),
                "last_close_at": history[-1][0],
                "calibration_temperature": active.temperature,
            },
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", required=True, help="Instrument, e.g. BTC-USD")
    parser.add_argument("--horizon", default="1h")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    database_path = Path(args.database) if args.database else default_database_path()
    predictor = TorchPredictor(seed=args.seed)
    now = utc_now_iso()
    with connect(database_path) as connection:
        prediction_id = record_live_prediction(
            connection,
            instrument=args.instrument,
            horizon=args.horizon,
            interval=args.interval,
            predictor=predictor,
            now_iso=now,
        )
        row = connection.execute(
            "SELECT probability_up FROM crypto_predictions WHERE id = ?", (prediction_id,)
        ).fetchone()
    result = {
        "prediction_id": prediction_id,
        "predictor": predictor.name,
        "probability_up": float(row["probability_up"]),
        "seed": predictor.seed,
    }
    print(json.dumps(result) if args.json else (
        f"logged {predictor.name} prediction {prediction_id}: P(up)={result['probability_up']:.4f}"
    ))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
