"""Compare realised volatility against the fixed round-trip cost, per instrument.

This module exists to settle one specific argument with data rather than opinion:
*higher-volatility instruments are where the money is.*

That claim has a real mechanism behind it. Trading costs are proportional to
position size in basis points, while the move being captured is not, so a fixed
30 bps round trip is trivial against a 3% move and impossible against a 0.3% one.
`cost_ratio` measures exactly that: a value above 1.0 means the average move does
not even cover the cost of taking it, which is unwinnable no matter how good the
forecast is.

What it does **not** measure is edge. A favourable cost ratio makes a correct
prediction worth something; it does not make predictions correct. Both halves
matter, and conflating them is the mistake this module is meant to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import statistics
from typing import Sequence

from andy_trader.predict import load_closes

# 10 bps fee plus 5 bps slippage, charged on entry and on exit. Matches the
# CT-04 backtest defaults so the two are directly comparable.
DEFAULT_ROUND_TRIP = 0.0030
MINIMUM_BARS = 20


class AnalysisError(ValueError):
    """Raised when there is not enough history to say anything honest."""


@dataclass(frozen=True)
class VolatilityProfile:
    instrument: str
    bars: int
    volatility: float
    mean_abs_move: float
    cost_ratio: float

    @property
    def cost_barrier_passable(self) -> bool:
        """True when a typical move more than covers the round trip.

        Passable is a necessary condition for profit, never a sufficient one.
        """

        return self.cost_ratio < 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument": self.instrument,
            "bars": self.bars,
            "volatility": self.volatility,
            "mean_abs_move": self.mean_abs_move,
            "cost_ratio": self.cost_ratio,
            "cost_barrier_passable": self.cost_barrier_passable,
        }


def returns(closes: Sequence[float]) -> list[float]:
    """Simple period-over-period returns. Raises on a non-positive price."""

    if len(closes) < 2:
        raise AnalysisError(f"need at least 2 closes, got {len(closes)}")
    out = []
    for previous, current in zip(closes, closes[1:]):
        if previous <= 0:
            raise AnalysisError(f"non-positive close {previous!r} makes a return undefined")
        out.append((current - previous) / previous)
    return out


def profile_closes(
    instrument: str,
    closes: Sequence[float],
    *,
    round_trip: float = DEFAULT_ROUND_TRIP,
) -> VolatilityProfile:
    if round_trip < 0:
        raise AnalysisError(f"round_trip cannot be negative, got {round_trip!r}")
    if len(closes) < MINIMUM_BARS:
        raise AnalysisError(
            f"{instrument} has {len(closes)} closes; need at least {MINIMUM_BARS} "
            "for a volatility estimate that is not noise"
        )
    rets = returns(closes)
    mean_abs = statistics.fmean(abs(r) for r in rets)
    # Population standard deviation, not sample: this is a description of the
    # observed window, not an inference about a wider population.
    volatility = statistics.pstdev(rets)
    if mean_abs == 0:
        raise AnalysisError(f"{instrument} never moved; a cost ratio would be infinite")
    return VolatilityProfile(
        instrument=instrument,
        bars=len(closes),
        volatility=volatility,
        mean_abs_move=mean_abs,
        cost_ratio=round_trip / mean_abs,
    )


def profile_instruments(
    connection: sqlite3.Connection,
    instruments: Sequence[str],
    *,
    interval: str = "1h",
    round_trip: float = DEFAULT_ROUND_TRIP,
) -> list[VolatilityProfile]:
    """Profile each instrument, most volatile first. Skips those with too little history."""

    profiles: list[VolatilityProfile] = []
    for instrument in instruments:
        closes = [close for _, close in load_closes(
            connection, instrument, interval=interval, limit=2**31 - 1
        )]
        try:
            profiles.append(profile_closes(instrument, closes, round_trip=round_trip))
        except AnalysisError:
            continue
    return sorted(profiles, key=lambda p: p.mean_abs_move, reverse=True)


def format_profiles(profiles: Sequence[VolatilityProfile]) -> str:
    if not profiles:
        return "No instrument had enough history to profile."
    lines = [
        f"{'instrument':<12}{'bars':>6}{'volatility':>13}{'avg |move|':>13}{'cost/move':>12}  barrier",
        "-" * 68,
    ]
    for p in profiles:
        barrier = "passable" if p.cost_barrier_passable else "UNWINNABLE"
        lines.append(
            f"{p.instrument:<12}{p.bars:>6}{p.volatility:>12.4%}"
            f"{p.mean_abs_move:>12.4%}{p.cost_ratio:>11.2f}x  {barrier}"
        )
    lines.append("")
    lines.append(
        "cost/move is the round trip divided by the average move. Above 1.00 the fee "
        "exceeds the\nmove itself, so no forecast however good can pay for the trade."
    )
    lines.append(
        "A passable barrier is necessary for profit and nowhere near sufficient. It says a "
        "correct\nprediction would be worth something; it says nothing about having one."
    )
    return "\n".join(lines)
