"""Context-aware predictors built from positioning and sentiment signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3
from typing import Sequence

from andy_trader.baselines import (
    MAX_CONFIDENCE,
    MIN_CONFIDENCE,
    PredictionContext,
)
from andy_trader.signals import FEAR_GREED, FUNDING_RATE, LONG_RATIO, latest_signal

# One hundred observations is roughly four days for hourly positioning. It was
# chosen before seeing any scores as a compromise between regime locality and a
# percentile with useful resolution, not because it made a backtest look good.
DEFAULT_SIGNAL_HISTORY = 100

# With fewer than 20 observations, one sample moves the percentile by at least
# five points. Twenty is an admittedly heuristic minimum chosen to abstain while
# the instrument-specific reference distribution is still that unstable.
MINIMUM_SIGNAL_HISTORY = 20

# "Extreme" means the outer quartile. Twenty-five percent is the conventional
# descriptive cutoff and was fixed before measurement; there is no claim that
# markets reverse at a theoretically special 75th percentile.
EXTREME_TAIL = 0.25

# Freshness follows source cadence with a small missed-poll allowance. Hourly
# account ratios expire after two hours, 8-hour funding after twelve, and the
# daily sentiment index after thirty-six. These prevent indefinite carry-forward
# while tolerating one delayed collection pass.
_MAX_AGE = {
    LONG_RATIO: timedelta(hours=2),
    FUNDING_RATE: timedelta(hours=12),
    FEAR_GREED: timedelta(hours=36),
}


@dataclass(frozen=True)
class SignalPredictor:
    """Map one signal's rolling percentile to a capped probability of an up move."""

    name: str
    signal: str
    direction: int
    market_wide: bool = False
    history_size: int = DEFAULT_SIGNAL_HISTORY
    minimum_signal_history: int = MINIMUM_SIGNAL_HISTORY
    minimum_history: int = 1

    def __post_init__(self) -> None:
        if self.signal not in _MAX_AGE:
            raise ValueError(f"Unsupported predictor signal {self.signal!r}")
        if self.direction not in {-1, 1}:
            raise ValueError(f"direction must be -1 or 1, got {self.direction!r}")
        if self.history_size < 1:
            raise ValueError("history_size must be at least 1")
        if self.minimum_signal_history < 1:
            raise ValueError("minimum_signal_history must be at least 1")
        if self.minimum_signal_history > self.history_size:
            raise ValueError("minimum_signal_history cannot exceed history_size")

    @property
    def prediction_name(self) -> str:
        return f"signal:{self.name}"

    def __call__(
        self,
        closes: Sequence[float],
        *,
        context: PredictionContext | None = None,
    ) -> float:
        del closes  # The shared interface supplies price history; this hypothesis does not use it.
        if context is None:
            return 0.5
        instrument = None if self.market_wide else context.instrument
        value = latest_signal(
            context.connection,
            self.signal,
            instrument=instrument,
            at_or_before=context.at_or_before,
        )
        if value is None:
            context.features.update({"signal": self.signal, "signal_status": "missing"})
            return 0.5

        history = _signal_history(
            context.connection,
            self.signal,
            instrument=instrument,
            at_or_before=context.at_or_before,
            limit=self.history_size,
        )
        if not history:
            context.features.update({"signal": self.signal, "signal_status": "missing"})
            return 0.5
        observed_at = history[0][0]
        age = _at(context.at_or_before) - _at(observed_at)
        if age > _MAX_AGE[self.signal]:
            context.features.update(
                {
                    "signal": self.signal,
                    "signal_status": "stale",
                    "signal_observed_at": observed_at,
                }
            )
            return 0.5
        values = [historical_value for _, historical_value in history]
        if len(values) < self.minimum_signal_history:
            context.features.update(
                {
                    "signal": self.signal,
                    "signal_status": "insufficient_history",
                    "signal_history": len(values),
                }
            )
            return 0.5

        percentile = _midrank_percentile(value, values)
        pressure = _tail_pressure(percentile)
        probability = max(
            MIN_CONFIDENCE,
            min(MAX_CONFIDENCE, 0.5 + self.direction * pressure * (MAX_CONFIDENCE - 0.5)),
        )
        context.features.update(
            {
                "signal": self.signal,
                "signal_status": "used",
                "signal_value": value,
                "signal_observed_at": observed_at,
                "signal_percentile": percentile,
                "signal_history": len(values),
                "signal_history_limit": self.history_size,
            }
        )
        return probability


def _at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _signal_history(
    connection: sqlite3.Connection,
    signal: str,
    *,
    instrument: str | None,
    at_or_before: str,
    limit: int,
) -> list[tuple[str, float]]:
    instrument_clause = "instrument IS NULL" if instrument is None else "instrument = ?"
    params: list[object] = [signal]
    if instrument is not None:
        params.append(instrument)
    params.extend((at_or_before, limit))
    rows = connection.execute(
        f"""
        SELECT observed_time, value
        FROM (
            SELECT observed_time, value, source, times_seen,
                   ROW_NUMBER() OVER (
                       PARTITION BY observed_time ORDER BY times_seen DESC, source ASC
                   ) AS rank
            FROM crypto_signals
            WHERE signal = ? AND {instrument_clause}
              AND degraded = 0 AND value IS NOT NULL AND observed_time <= ?
        )
        WHERE rank = 1
        ORDER BY observed_time DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [(str(row["observed_time"]), float(row["value"])) for row in rows]


def _midrank_percentile(value: float, history: Sequence[float]) -> float:
    below = sum(historical < value for historical in history)
    equal = sum(historical == value for historical in history)
    return (below + 0.5 * equal) / len(history)


def _tail_pressure(percentile: float) -> float:
    lower = EXTREME_TAIL
    upper = 1.0 - EXTREME_TAIL
    if percentile < lower:
        return (percentile - lower) / lower
    if percentile > upper:
        return (percentile - upper) / EXTREME_TAIL
    return 0.0


def crowd_contrarian() -> SignalPredictor:
    return SignalPredictor("crowd_contrarian", LONG_RATIO, direction=-1)


def funding_contrarian() -> SignalPredictor:
    return SignalPredictor("funding_contrarian", FUNDING_RATE, direction=-1)


def fear_greed_contrarian() -> SignalPredictor:
    return SignalPredictor(
        "fear_greed_contrarian", FEAR_GREED, direction=-1, market_wide=True
    )


def crowd_momentum() -> SignalPredictor:
    return SignalPredictor("crowd_momentum", LONG_RATIO, direction=1)


def default_signal_predictors() -> tuple[SignalPredictor, ...]:
    return (
        crowd_contrarian(),
        funding_contrarian(),
        fear_greed_contrarian(),
        crowd_momentum(),
    )
