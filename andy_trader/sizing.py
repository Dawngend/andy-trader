"""Deterministic paper position sizing from recent hourly prices."""

import math
import os
import statistics
from collections.abc import Sequence


def realized_volatility(closes: Sequence[float]) -> float | None:
    """Population standard deviation of hourly log returns in the last 24 closes."""
    if len(closes) < 6:
        return None
    recent = closes[-24:]
    returns = [math.log(current / previous) for previous, current in zip(recent, recent[1:])]
    return statistics.pstdev(returns)


def size_fraction(
    volatility: float | None, *, target_vol: float = 0.01, floor: float = 0.1, cap: float = 1.0
) -> float:
    """Clamp the volatility target to the permitted share of cash."""
    target_vol = float(os.environ.get("CRYPTO_TARGET_VOL", target_vol))
    if volatility is None or volatility == 0:
        return floor
    return min(cap, max(floor, target_vol / volatility))
