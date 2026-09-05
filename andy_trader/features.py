"""Multimodal feature engineering with strict backward as-of signal joins.

Every feature vector computed here must obey the anti-leakage invariant:
for a candle whose close is established at reference_time T, no data
(price bar or signal) observed at > T may ever be read or influence the vector.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
import sqlite3
from typing import Mapping, Sequence

from andy_trader.model import ModelError, _ema, _return
from andy_trader.signals import (
    FEAR_GREED,
    FUNDING_RATE,
    KNOWN_SIGNALS,
    LONG_RATIO,
    OPEN_INTEREST,
    initialize_signals,
)

# Freshness limits matching signal_predictors.py with open_interest added.
# Signals older than these thresholds are treated as missing/stale.
SIGNAL_MAX_AGE: Mapping[str, timedelta] = {
    LONG_RATIO: timedelta(hours=2),
    OPEN_INTEREST: timedelta(hours=2),
    FUNDING_RATE: timedelta(hours=12),
    FEAR_GREED: timedelta(hours=36),
}

MULTIMODAL_FEATURE_NAMES: tuple[str, ...] = (
    "return_1",
    "return_3",
    "return_6",
    "mean_return",
    "return_volatility",
    "ema_12_distance",
    "funding_rate",
    "funding_is_valid",
    "long_ratio_spread",
    "long_ratio_is_valid",
    "open_interest_momentum",
    "open_interest_is_valid",
    "fear_greed_normalized",
    "fear_greed_is_valid",
)


@dataclass(frozen=True)
class SignalObservation:
    """One immutable signal point known at observed_time."""

    signal: str
    observed_time: str
    value: float


@dataclass(frozen=True)
class MultimodalFeatureRow:
    """Features known at close_index and reference_time, with no future leakage."""

    close_index: int
    reference_time: str
    values: tuple[float, ...]


def parse_iso_utc(iso_str: str) -> datetime:
    """Parse ISO timestamp to timezone-aware UTC datetime."""
    parsed = datetime.fromisoformat(iso_str)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def load_signals_for_series(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    until_iso: str | None = None,
) -> dict[str, list[SignalObservation]]:
    """Batch-fetch all non-degraded signals up to until_iso, ordered chronologically.

    Returns a dict mapping signal name to sorted list of SignalObservation.
    """
    initialize_signals(connection)
    clauses = ["degraded = 0", "value IS NOT NULL"]
    params: list[object] = []

    # Either instrument-specific or market-wide (instrument IS NULL)
    clauses.append("(instrument = ? OR instrument IS NULL)")
    params.append(instrument)

    if until_iso is not None:
        clauses.append("observed_time <= ?")
        params.append(until_iso)

    sql = f"""
        SELECT signal, observed_time, value
        FROM (
            SELECT signal, observed_time, value, source, times_seen,
                   ROW_NUMBER() OVER (
                       PARTITION BY signal, observed_time ORDER BY times_seen DESC, source ASC
                   ) AS rank
            FROM crypto_signals
            WHERE {" AND ".join(clauses)}
        )
        WHERE rank = 1
        ORDER BY observed_time ASC
    """
    rows = connection.execute(sql, tuple(params)).fetchall()

    result: dict[str, list[SignalObservation]] = {sig: [] for sig in KNOWN_SIGNALS}
    for row in rows:
        sig = str(row["signal"])
        if sig in result:
            result[sig].append(
                SignalObservation(
                    signal=sig,
                    observed_time=str(row["observed_time"]),
                    value=float(row["value"]),
                )
            )
    return result


def as_of_signal_lookup(
    observations: Sequence[SignalObservation],
    reference_time: str,
    max_age: timedelta,
) -> float | None:
    """Find the latest observation where observed_time <= reference_time within max_age.

    This is a strict backward join: no observation whose timestamp is strictly
    greater than reference_time can ever be returned.
    """
    if not observations:
        return None

    # Binary search to find the latest observation at or before reference_time
    times = [obs.observed_time for obs in observations]
    index = bisect.bisect_right(times, reference_time) - 1
    if index < 0:
        return None

    cand = observations[index]
    # Invariant assertion: cand.observed_time MUST be <= reference_time
    if cand.observed_time > reference_time:  # pragma: no cover - defensive safety
        raise ModelError(
            f"Leakage invariant violated: {cand.observed_time} > {reference_time}"
        )

    ref_dt = parse_iso_utc(reference_time)
    obs_dt = parse_iso_utc(cand.observed_time)
    if ref_dt < obs_dt:  # pragma: no cover - defensive safety
        raise ModelError(f"Leakage invariant violated: {ref_dt} < {obs_dt}")

    if (ref_dt - obs_dt) > max_age:
        # Stale
        return None

    return cand.value


def engineer_multimodal_features(
    history: Sequence[tuple[str, float]],
    signals: Mapping[str, Sequence[SignalObservation]],
    *,
    lookback: int = 24,
) -> list[MultimodalFeatureRow]:
    """Build multimodal feature rows from closes and signals with strict as-of joins.

    history: Sequence of (open_time, close) ordered chronologically.
    signals: Mapping of signal name to sorted observations.

    No feature at index i is allowed to read closes beyond i or signals
    observed after history[i][0].
    """
    if lookback < 6:
        raise ModelError("lookback must be at least 6")

    prices = [float(close) for _, close in history]
    if any(not math.isfinite(c) or c <= 0 for c in prices):
        raise ModelError("Feature prices must be finite and positive")

    funding_obs = signals.get(FUNDING_RATE, ())
    long_obs = signals.get(LONG_RATIO, ())
    oi_obs = signals.get(OPEN_INTEREST, ())
    fg_obs = signals.get(FEAR_GREED, ())

    rows: list[MultimodalFeatureRow] = []

    for index in range(lookback, len(history)):
        ref_time, current_price = history[index]
        trailing_prices = prices[index - lookback : index + 1]

        # Price-derived features (identical to model.py trailing formulas)
        returns = [
            _return(prev, cur)
            for prev, cur in zip(trailing_prices, trailing_prices[1:])
        ]
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        volatility = math.sqrt(variance)
        ema_12 = _ema(trailing_prices, 12)
        ema_dist = (current_price / ema_12) - 1.0

        # As-of signal joins
        # 1. Funding rate: clamped to [-0.01, 0.01]
        raw_funding = as_of_signal_lookup(
            funding_obs, ref_time, SIGNAL_MAX_AGE[FUNDING_RATE]
        )
        if raw_funding is not None and math.isfinite(raw_funding):
            funding_rate = max(-0.01, min(0.01, raw_funding))
            funding_valid = 1.0
        else:
            funding_rate = 0.0
            funding_valid = 0.0

        # 2. Long ratio: spread around 0.5, clamped to [-0.5, 0.5]
        raw_long = as_of_signal_lookup(
            long_obs, ref_time, SIGNAL_MAX_AGE[LONG_RATIO]
        )
        if raw_long is not None and math.isfinite(raw_long):
            long_spread = max(-0.5, min(0.5, raw_long - 0.5))
            long_valid = 1.0
        else:
            long_spread = 0.0
            long_valid = 0.0

        # 3. Open Interest: normalized relative change over lookback
        # Look up OI at ref_time, and OI at trailing ref_time (6 bars back)
        raw_oi_current = as_of_signal_lookup(
            oi_obs, ref_time, SIGNAL_MAX_AGE[OPEN_INTEREST]
        )
        trailing_ref_time = history[index - 6][0]
        raw_oi_trailing = as_of_signal_lookup(
            oi_obs, trailing_ref_time, SIGNAL_MAX_AGE[OPEN_INTEREST] + timedelta(hours=6)
        )
        if (
            raw_oi_current is not None
            and raw_oi_trailing is not None
            and raw_oi_trailing > 0
            and raw_oi_current > 0
        ):
            oi_momentum = max(-1.0, min(1.0, math.log(raw_oi_current / raw_oi_trailing)))
            oi_valid = 1.0
        else:
            oi_momentum = 0.0
            oi_valid = 0.0

        # 4. Fear & Greed: normalized to [-1.0, 1.0]
        raw_fg = as_of_signal_lookup(
            fg_obs, ref_time, SIGNAL_MAX_AGE[FEAR_GREED]
        )
        if raw_fg is not None and math.isfinite(raw_fg):
            fg_normalized = max(-1.0, min(1.0, (raw_fg - 50.0) / 50.0))
            fg_valid = 1.0
        else:
            fg_normalized = 0.0
            fg_valid = 0.0

        values = (
            _return(prices[index - 1], current_price),
            _return(prices[index - 3], current_price),
            _return(prices[index - 6], current_price),
            mean_return,
            volatility,
            ema_dist,
            funding_rate,
            funding_valid,
            long_spread,
            long_valid,
            oi_momentum,
            oi_valid,
            fg_normalized,
            fg_valid,
        )

        rows.append(
            MultimodalFeatureRow(
                close_index=index,
                reference_time=ref_time,
                values=values,
            )
        )

    return rows
