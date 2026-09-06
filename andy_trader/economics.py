"""Whether a horizon can be traded profitably at all, before any predictor exists.

This module answers a question that comes before "is the model any good": given
what it costs to trade and how far price actually moves, what accuracy would ANY
predictor need just to break even?

    EV per trade = M(2p - 1) - C
        M = average absolute price move over the horizon
        C = round-trip cost (fee + slippage, on entry and on exit)
        p = probability the directional call is right

    break-even  =>  p = 0.5 + C / (2M)

C is fixed no matter which horizon is traded. M grows with the horizon. So the
required accuracy falls as the horizon lengthens, and a horizon can be closed to
profit by arithmetic alone.

Measured on this repository's own data, 2026-09-06:

    1m     1.4 bps move  ->  break-even  1115%    impossible
    1h    23.6 bps move  ->  break-even   113%    impossible
    4h    48.8 bps move  ->  break-even    81%    very hard

At 1h the round-trip cost is LARGER than the average move. A predictor that was
right every single time would still lose money. That is not a model problem, a
threshold problem, or a gate that is set too strictly; it is the instrument. The
best directional accuracy observed across every predictor here is about 52%.

The practical consequence: short-horizon spot trading on retail fees is closed,
and no amount of model work reopens it. What changes the answer is a longer
horizon, materially lower costs (maker rather than taker fills), or an
instrument with a different cost structure altogether -- a binary prediction
contract, for instance, has no basis-point cost at all; its cost is the gap
between the price paid and the true probability.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

# Matches andy_trader.backtest and andy_trader.portfolio: 10 bps fee and 5 bps
# slippage, charged on entry and again on exit.
DEFAULT_ROUND_TRIP_BPS = 30.0
MINIMUM_BARS = 30


@dataclass(frozen=True)
class HorizonEconomics:
    """What any predictor would need to achieve at one horizon to break even."""

    interval: str
    bars: int
    average_move_bps: float
    round_trip_bps: float
    break_even_win_rate: float

    @property
    def cost_share_of_move(self) -> float:
        """Fraction of the average move consumed by trading it. Over 1.0 is fatal."""

        return self.round_trip_bps / self.average_move_bps if self.average_move_bps else float("inf")

    @property
    def verdict(self) -> str:
        if self.break_even_win_rate >= 1.0:
            return "impossible"
        if self.break_even_win_rate >= 0.70:
            return "very hard"
        if self.break_even_win_rate >= 0.60:
            return "hard"
        return "plausible"


def average_absolute_move_bps(
    connection: sqlite3.Connection, *, instrument: str, interval: str
) -> tuple[float, int]:
    """Mean absolute bar-to-bar move, in basis points, and the bars behind it."""

    rows = connection.execute(
        """
        SELECT close FROM crypto_observations
        WHERE instrument = ? AND interval = ? AND degraded = 0 AND close IS NOT NULL
        GROUP BY open_time ORDER BY open_time ASC
        """,
        (instrument, interval),
    ).fetchall()
    closes = [float(row["close"]) for row in rows]
    moves = [
        abs(later - earlier) / earlier * 10_000.0
        for earlier, later in zip(closes, closes[1:])
        if earlier > 0
    ]
    if not moves:
        return 0.0, len(closes)
    return sum(moves) / len(moves), len(closes)


def evaluate_horizon(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    interval: str,
    round_trip_bps: float = DEFAULT_ROUND_TRIP_BPS,
) -> HorizonEconomics | None:
    """Break-even accuracy for one horizon, or None without enough history."""

    average_move, bars = average_absolute_move_bps(
        connection, instrument=instrument, interval=interval
    )
    if bars < MINIMUM_BARS or average_move <= 0:
        return None
    break_even = 0.5 + round_trip_bps / (2.0 * average_move)
    return HorizonEconomics(
        interval=interval,
        bars=bars,
        average_move_bps=average_move,
        round_trip_bps=round_trip_bps,
        break_even_win_rate=break_even,
    )


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    from pathlib import Path

    from andy_trader.store import connect, default_database_path

    parser = argparse.ArgumentParser(description="Can this horizon be traded at all?")
    parser.add_argument("--instrument", default="BTC-USD")
    parser.add_argument("--intervals", default="1m,1h,4h,1d")
    parser.add_argument("--round-trip-bps", type=float, default=DEFAULT_ROUND_TRIP_BPS)
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    args = parser.parse_args(argv)

    db_path = Path(args.database) if args.database else default_database_path()
    connection = connect(db_path)

    print(f"{args.instrument}, round-trip cost {args.round_trip_bps:.0f} bps\n")
    print(f"{'interval':>9} {'bars':>7} {'avg |move|':>13} {'cost/move':>11} {'break-even':>12}")
    print("-" * 60)
    for interval in [i.strip() for i in args.intervals.split(",") if i.strip()]:
        result = evaluate_horizon(
            connection,
            instrument=args.instrument,
            interval=interval,
            round_trip_bps=args.round_trip_bps,
        )
        if result is None:
            print(f"{interval:>9} {'--':>7}   (needs {MINIMUM_BARS}+ bars)")
            continue
        print(
            f"{result.interval:>9} {result.bars:>7} {result.average_move_bps:>10.1f} bps "
            f"{result.cost_share_of_move * 100:>10.0f}% "
            f"{result.break_even_win_rate * 100:>10.1f}%  {result.verdict}"
        )

    print(
        "\nCost is flat across horizons; the move is not. Where cost/move exceeds\n"
        "100%, trading the average move costs more than the move is worth and no\n"
        "predictor, however good, can profit there."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
