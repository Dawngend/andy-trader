"""Deliberately dumb predictors. Any real model has to beat these out of sample."""

from __future__ import annotations

from dataclasses import dataclass, field
import random
import sqlite3
from typing import Callable, Protocol, Sequence

# Confidence ceiling for the heuristic baselines. A baseline that emits 0.0 or
# 1.0 is a strawman: Brier punishes certainty so hard that beating it proves
# nothing. Capping at 0.65 keeps them honest opponents rather than free wins.
MAX_CONFIDENCE = 0.65
MIN_CONFIDENCE = 1.0 - MAX_CONFIDENCE


class BaselineError(ValueError):
    """Raised when a baseline is handed a history it cannot use."""


@dataclass(frozen=True)
class PredictionContext:
    """Information knowable when any predictor is asked for a probability."""

    connection: sqlite3.Connection
    instrument: str
    at_or_before: str
    features: dict[str, object] = field(default_factory=dict)


class Predictor(Protocol):
    """Shared contract for price-only and context-aware predictors."""

    name: str
    minimum_history: int

    @property
    def prediction_name(self) -> str: ...

    def __call__(
        self,
        closes: Sequence[float],
        *,
        context: PredictionContext,
    ) -> float: ...


@dataclass(frozen=True)
class Baseline:
    """A named function from price history to P(next close > current close)."""

    name: str
    predict: Callable[[Sequence[float]], float]
    minimum_history: int = 2

    @property
    def prediction_name(self) -> str:
        return f"baseline:{self.name}"

    def __call__(
        self,
        closes: Sequence[float],
        *,
        context: PredictionContext | None = None,
    ) -> float:
        if len(closes) < self.minimum_history:
            raise BaselineError(
                f"{self.name} needs at least {self.minimum_history} closes, got {len(closes)}"
            )
        value = self.predict(closes)
        if not 0.0 <= value <= 1.0:
            raise BaselineError(f"{self.name} produced an out-of-range probability {value!r}")
        return value


def coin_flip(_closes: Sequence[float]) -> float:
    """The true zero-information predictor. Brier lands on exactly 0.25.

    This is the floor. A model that cannot beat a constant 0.5 has learned
    nothing at all, and the Brier skill score against the base rate will say so.
    """

    return 0.5


def base_rate(closes: Sequence[float]) -> float:
    """Predict the historical share of up-moves, ignoring current conditions.

    This is the baseline that actually matters. It is what `brier_skill_score`
    compares against, and it is the one most naive models fail to beat, because
    "up slightly more often than down" is most of the signal in a drifting asset.
    """

    ups = sum(1 for previous, current in zip(closes, closes[1:]) if current > previous)
    moves = len(closes) - 1
    if moves <= 0:
        return 0.5
    return _clamp(ups / moves)


def momentum(closes: Sequence[float]) -> float:
    """Follow the last move, with confidence scaled by its size.

    Deliberately naive: it has no notion of mean reversion, no volatility
    normalisation beyond a crude one, and no memory past two bars.
    """

    previous, current = closes[-2], closes[-1]
    if previous <= 0:
        return 0.5
    change = (current - previous) / previous
    # 1% move maps to roughly the confidence ceiling; larger moves saturate.
    scaled = max(-1.0, min(1.0, change / 0.01))
    return _clamp(0.5 + scaled * (MAX_CONFIDENCE - 0.5))


def make_ema_crossover(fast: int = 12, slow: int = 26) -> Callable[[Sequence[float]], float]:
    """Classic fast-over-slow EMA crossover, expressed as a probability."""

    if fast >= slow:
        raise BaselineError(f"fast period must be below slow, got {fast} >= {slow}")

    def predict(closes: Sequence[float]) -> float:
        fast_value = _ema(closes, fast)
        slow_value = _ema(closes, slow)
        if slow_value <= 0:
            return 0.5
        spread = (fast_value - slow_value) / slow_value
        scaled = max(-1.0, min(1.0, spread / 0.02))
        return _clamp(0.5 + scaled * (MAX_CONFIDENCE - 0.5))

    return predict


def make_random(seed: int | None = None) -> Callable[[Sequence[float]], float]:
    """Uniform random probability. Seeded so a run is reproducible.

    Distinct from `coin_flip`: this one is badly calibrated on purpose, so the
    reliability term in the Murphy decomposition has something to catch.
    """

    rng = random.Random(seed)

    def predict(_closes: Sequence[float]) -> float:
        return rng.random()

    return predict


def _ema(closes: Sequence[float], period: int) -> float:
    if not closes:
        raise BaselineError("EMA needs at least one close")
    multiplier = 2.0 / (period + 1.0)
    value = float(closes[0])
    for close in closes[1:]:
        value = (float(close) - value) * multiplier + value
    return value


def _clamp(value: float) -> float:
    return max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, value))


def default_baselines(*, seed: int | None = 1729) -> tuple[Baseline, ...]:
    """The four in the CT-03 plan, plus the constant-0.5 floor.

    Order matters only for reporting. The bar a model must clear is the best of
    these, not the average, and in practice that is almost always `base_rate`.
    """

    return (
        Baseline("coin_flip", coin_flip, minimum_history=1),
        Baseline("random", make_random(seed), minimum_history=1),
        Baseline("base_rate", base_rate, minimum_history=2),
        Baseline("momentum", momentum, minimum_history=2),
        Baseline("ema_crossover_12_26", make_ema_crossover(12, 26), minimum_history=26),
    )
