"""Intra-round continuation: the 5-minute strategy, measured rather than assumed.

The claim this module exists to test, taken from a widely-shared Polymarket bot:
two minutes before a 5-minute round closes, if the price has already moved, bet
that the round finishes on the side it has already moved to.

Measured on 30 days of 1-minute BTC bars (8,640 clock-aligned rounds, 2026-09-06)
the underlying signal is real and strongly monotone in the size of the move:

    move so far        rounds     continued
    ------------------------------------------
    0-20 bps-ish          3790        68.2%
    $20-40                1885        82.0%
    $40-70                1438        88.1%
    $70-100                676        91.7%     <- the bot's own filter
    $100-150               510        96.7%
    $250+                   81       100.0%

So the signal is not the problem. The problem is price and size:

  * Against 400 real resolved Polymarket rounds, contracts bought near the entry
    moment at an average of $0.9266 won 94.16% of the time. That is an edge of
    +1.5 points, which is smaller than its own standard error (+/-1.59) and
    smaller than the bid-ask spread you cross to enter.
  * At the bot's stated 50%-of-bankroll sizing, that edge produces NEGATIVE
    compound growth (-0.0039 per trade) despite being positive expected value,
    because 50% is roughly 2.5x the Kelly-optimal stake and anything past 2x
    Kelly loses money by construction.

Hence this module keeps the signal and throws away the sizing. `kelly_fraction`
is the part that matters: it refuses to bet at all without a positive edge, and
caps what it will risk when there is one.

Nothing here touches an exchange. It emits probabilities into the ordinary
prediction log so the existing calibration and promotion machinery judges it on
the same terms as every other predictor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
import sqlite3
from typing import Sequence

from andy_trader.store import (
    FAST_HORIZONS,
    Prediction,
    record_prediction,
    settle_due_predictions,
    utc_now_iso,
)

# A Polymarket BTC round is five clock-aligned minutes; the bot enters with two
# minutes left, i.e. after the third minute has closed.
ROUND_MINUTES = 5
DECISION_MINUTE = 3
FAST_INTERVAL = "1m"
PREDICTOR_NAME = "fast:continuation"

# Move sizes are held in basis points, not dollars. The original bot hardcoded
# "$70 to $100", which is a statement about BTC at one particular price level
# and silently becomes a different strategy on ETH, on SOL, or on BTC six months
# from now. Basis points are the same bet at every price and on every asset.
DEFAULT_BAND_EDGES_BPS: tuple[float, ...] = (0.0, 2.5, 5.0, 8.0, 12.5, 19.0, 30.0, 1e9)

# Below this the "move" is noise and the round is a coin flip.
MIN_ACTIONABLE_BPS = 5.0


class FastMomentumError(ValueError):
    """Raised when the fast path is handed data it must not silently guess around."""


@dataclass(frozen=True)
class Round:
    """One reconstructed 5-minute round."""

    open_time: datetime
    open_price: float
    decision_price: float
    settle_price: float

    @property
    def observed_move_bps(self) -> float:
        """Signed move from the round's open to the decision point, in bps."""

        if self.open_price <= 0:
            raise FastMomentumError("round open price must be positive")
        return (self.decision_price - self.open_price) / self.open_price * 10_000.0

    @property
    def final_move(self) -> float:
        return self.settle_price - self.open_price

    @property
    def continued(self) -> bool | None:
        """Did the round settle on the side it had already moved to?

        None for an exact tie or a flat decision point, which is genuinely
        undefined rather than a win or a loss, and must not be counted as either.
        """

        observed = self.decision_price - self.open_price
        if observed == 0 or self.final_move == 0:
            return None
        return (self.final_move > 0) == (observed > 0)


@dataclass(frozen=True)
class ContinuationCurve:
    """Empirical P(continuation) as a function of how far price has already moved.

    Built from settled history only. The caller is responsible for passing rounds
    that closed strictly before the moment being predicted; this class has no way
    to detect lookahead and will happily report a beautifully calibrated curve
    fitted on the future.
    """

    edges_bps: tuple[float, ...]
    probabilities: tuple[float, ...]
    counts: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.edges_bps) < 2:
            raise FastMomentumError("a curve needs at least two band edges")
        if len(self.probabilities) != len(self.edges_bps) - 1:
            raise FastMomentumError("probabilities must have one entry per band")
        if len(self.counts) != len(self.probabilities):
            raise FastMomentumError("counts must have one entry per band")

    def probability(self, abs_move_bps: float, *, minimum_samples: int = 30) -> float:
        """P(the move continues), given how far it has already gone.

        Falls back to 0.5 for a band with too little evidence. A thin band that
        happened to go 5-for-5 is not a 100% edge and must not be traded as one.
        """

        magnitude = abs(abs_move_bps)
        for index in range(len(self.probabilities)):
            low, high = self.edges_bps[index], self.edges_bps[index + 1]
            if low <= magnitude < high:
                if self.counts[index] < minimum_samples:
                    return 0.5
                return self.probabilities[index]
        return 0.5


def fit_continuation_curve(
    rounds: Sequence[Round],
    *,
    edges_bps: Sequence[float] = DEFAULT_BAND_EDGES_BPS,
) -> ContinuationCurve:
    """Measure the continuation rate per move-size band from settled rounds."""

    edges = tuple(float(edge) for edge in edges_bps)
    wins = [0] * (len(edges) - 1)
    totals = [0] * (len(edges) - 1)
    for item in rounds:
        outcome = item.continued
        if outcome is None:
            continue
        magnitude = abs(item.observed_move_bps)
        for index in range(len(edges) - 1):
            if edges[index] <= magnitude < edges[index + 1]:
                totals[index] += 1
                if outcome:
                    wins[index] += 1
                break
    probabilities = tuple(
        (wins[i] / totals[i]) if totals[i] else 0.5 for i in range(len(totals))
    )
    return ContinuationCurve(edges, probabilities, tuple(totals))


def probability_up(
    move_bps: float,
    curve: ContinuationCurve,
    *,
    minimum_bps: float = MIN_ACTIONABLE_BPS,
) -> float:
    """Convert a signed observed move into P(round closes above its open).

    The curve reports P(continuation), which is direction-free. A downward move
    that continues means the round closes DOWN, so the probability has to be
    mirrored rather than reused.
    """

    if abs(move_bps) < minimum_bps:
        return 0.5
    continuation = curve.probability(abs(move_bps))
    return continuation if move_bps > 0 else 1.0 - continuation


def kelly_fraction(
    probability: float,
    price: float,
    *,
    scale: float = 0.5,
    cap: float = 0.20,
) -> float:
    """Fraction of bankroll to stake on a binary contract bought at `price`.

    This is the function that separates this implementation from the bot it came
    from. That bot staked a flat ~50% of allocation. Against the edge actually
    measured here, 50% is about 2.5x Kelly, and past 2x Kelly the compound growth
    rate turns negative no matter how favourable each individual bet is: you win
    almost every trade and still trend to zero.

    Defaults are half-Kelly, capped at 20%. Half-Kelly gives up a quarter of the
    growth rate for a large reduction in drawdown, which is the right trade when
    the edge itself is estimated rather than known -- and here the measured edge
    was within one standard error of zero, so the true edge may be smaller than
    the estimate, or absent.
    """

    if not 0.0 < price < 1.0:
        raise FastMomentumError(f"price must be strictly between 0 and 1, got {price!r}")
    if not 0.0 <= probability <= 1.0:
        raise FastMomentumError(f"probability must be in [0, 1], got {probability!r}")
    net_odds = (1.0 - price) / price
    edge = probability * net_odds - (1.0 - probability)
    if edge <= 0:
        return 0.0
    full_kelly = edge / net_odds
    return max(0.0, min(full_kelly * scale, cap))


def log_growth_per_trade(probability: float, price: float, stake_fraction: float) -> float:
    """Expected log growth per trade. Negative means the account bleeds out.

    Exposed because expected value alone is famously reassuring and famously
    insufficient: the bot this module is derived from had POSITIVE expected value
    per trade and negative growth, which is the entire reason it fails.
    """

    if not 0.0 <= stake_fraction < 1.0:
        raise FastMomentumError("stake_fraction must be in [0, 1)")
    if not 0.0 < price < 1.0:
        raise FastMomentumError("price must be strictly between 0 and 1")
    win_multiplier = 1.0 + stake_fraction * ((1.0 - price) / price)
    lose_multiplier = 1.0 - stake_fraction
    if lose_multiplier <= 0:
        return float("-inf")
    return probability * math.log(win_multiplier) + (1.0 - probability) * math.log(
        lose_multiplier
    )


def load_minute_closes(
    connection: sqlite3.Connection,
    instrument: str,
    *,
    since_iso: str | None = None,
    venue: str | None = None,
) -> list[tuple[datetime, float]]:
    """Ordered (time, close) pairs from non-degraded 1m bars.

    Where several venues reported the same minute, the most-confirmed row wins,
    matching the tie-break `close_price_at` already uses so the two never
    disagree about what the price was.
    """

    clauses = ["interval = ?", "instrument = ?", "degraded = 0", "close IS NOT NULL"]
    params: list[object] = [FAST_INTERVAL, instrument]
    if since_iso:
        clauses.append("open_time >= ?")
        params.append(since_iso)
    if venue:
        clauses.append("venue = ?")
        params.append(venue)
    rows = connection.execute(
        f"""
        SELECT open_time, close, times_seen
        FROM crypto_observations
        WHERE {' AND '.join(clauses)}
        ORDER BY open_time ASC, times_seen ASC
        """,
        params,
    ).fetchall()
    # Later rows win, and rows arrive in ascending times_seen order, so the
    # most-confirmed observation for each minute is the one left standing.
    best: dict[datetime, float] = {}
    for row in rows:
        best[datetime.fromisoformat(row["open_time"])] = float(row["close"])
    return sorted(best.items())


def build_rounds(closes: Sequence[tuple[datetime, float]]) -> list[Round]:
    """Group 1-minute closes into clock-aligned 5-minute rounds.

    A round is only emitted when every minute it needs is present. An incomplete
    round is dropped rather than interpolated: inventing the decision-point price
    is exactly the kind of small convenience that makes a backtest optimistic.
    """

    by_minute = {stamp.replace(second=0, microsecond=0): price for stamp, price in closes}
    rounds: list[Round] = []
    for stamp in sorted(by_minute):
        if stamp.minute % ROUND_MINUTES != 0:
            continue
        # The round's opening reference is the close of the minute before it, so
        # that "the move so far" is measured from a price that was actually
        # observable when the round began.
        open_price = by_minute.get(stamp - timedelta(minutes=1))
        decision = by_minute.get(stamp + timedelta(minutes=DECISION_MINUTE - 1))
        settle = by_minute.get(stamp + timedelta(minutes=ROUND_MINUTES - 1))
        if open_price is None or decision is None or settle is None:
            continue
        rounds.append(
            Round(
                open_time=stamp,
                open_price=open_price,
                decision_price=decision,
                settle_price=settle,
            )
        )
    return rounds


def settle_fast_predictions(
    connection: sqlite3.Connection,
    *,
    now_iso: str | None = None,
    tolerance_minutes: int = 2,
) -> dict[str, int]:
    """Settle sub-hourly predictions against 1m bars, never the hourly series.

    The default settlement pass deliberately refuses to touch these horizons,
    because its 90-minute tolerance against 1h bars would resolve a 2-minute call
    with a price from another hour entirely and score the result as real.
    """

    return settle_due_predictions(
        connection,
        now_iso=now_iso,
        interval=FAST_INTERVAL,
        tolerance_minutes=tolerance_minutes,
        horizons=FAST_HORIZONS,
    )


def predict_round_once(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    curve: ContinuationCurve,
    now: datetime | None = None,
    mode: str = "advisory",
) -> Prediction | None:
    """Emit one continuation call for the round currently in progress.

    Returns None when there is no actionable state: outside the decision minute,
    missing bars, or a move too small to be anything but noise. Returning None is
    the common case by design -- the strategy's whole premise is selectivity.
    """

    moment = now or datetime.now(UTC)
    # The bar labelled M closes at M+1, so the newest bar we are allowed to look
    # at is the one before the minute currently in progress. Using the in-progress
    # minute instead would read a price that has not finished forming, which is
    # lookahead of exactly the kind that makes a fast strategy look brilliant in
    # a backtest and lose money live.
    latest_complete = moment.replace(second=0, microsecond=0) - timedelta(minutes=1)
    offset = latest_complete.minute % ROUND_MINUTES
    if offset != DECISION_MINUTE - 1:
        return None

    round_start = latest_complete - timedelta(minutes=offset)
    closes = load_minute_closes(
        connection,
        instrument,
        since_iso=(round_start - timedelta(minutes=5)).isoformat(),
    )
    by_minute = {stamp.replace(second=0, microsecond=0): price for stamp, price in closes}
    open_price = by_minute.get(round_start - timedelta(minutes=1))
    decision_price = by_minute.get(latest_complete)
    if open_price is None or decision_price is None or open_price <= 0:
        return None

    move_bps = (decision_price - open_price) / open_price * 10_000.0
    if abs(move_bps) < MIN_ACTIONABLE_BPS:
        return None

    probability = probability_up(move_bps, curve)
    if probability == 0.5:
        return None

    resolves_at = round_start + timedelta(minutes=ROUND_MINUTES)
    prediction = Prediction(
        predictor=PREDICTOR_NAME,
        instrument=instrument,
        horizon="2m",
        probability_up=probability,
        reference_price=open_price,
        created_at=moment.isoformat(),
        resolves_at=resolves_at.isoformat(),
        mode=mode,
        features={
            "strategy": "intra_round_continuation",
            "round_start": round_start.isoformat(),
            "observed_move_bps": round(move_bps, 4),
            "decision_price": decision_price,
            "curve_counts": list(curve.counts),
        },
    )
    record_prediction(connection, prediction)
    return prediction


def main(argv: Sequence[str] | None = None) -> int:
    """Report the continuation curve measured from whatever 1m data we hold.

    This is the honest status check for the strategy: it says how much evidence
    exists, what the evidence currently claims, and what stake that justifies.
    Early on it will mostly say "not enough data", which is the correct answer.
    """

    import argparse

    from andy_trader.store import connect, default_database_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", default="BTC-USD")
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument(
        "--price",
        type=float,
        default=0.9266,
        help="Contract price to size against; defaults to the measured Polymarket average",
    )
    args = parser.parse_args(argv)

    from pathlib import Path

    db_path = Path(args.database) if args.database else default_database_path()
    connection = connect(db_path)
    closes = load_minute_closes(connection, args.instrument)
    rounds = build_rounds(closes)
    curve = fit_continuation_curve(rounds)

    print(f"instrument      : {args.instrument}")
    print(f"1m bars held    : {len(closes)}")
    print(f"complete rounds : {len(rounds)}")
    if not rounds:
        print("\nNo complete rounds yet. Collect 1m bars first:")
        print("  python -m andy_trader.collector --intervals 1m --venues binance")
        return 0

    print(f"\n{'move band (bps)':>18} {'rounds':>8} {'continued':>11} {'usable':>8}")
    for index, count in enumerate(curve.counts):
        low, high = curve.edges_bps[index], curve.edges_bps[index + 1]
        label = f"{low:g}-{'+' if high >= 1e9 else format(high, 'g')}"
        usable = "yes" if count >= 30 else "no"
        share = f"{curve.probabilities[index] * 100:.1f}%" if count else "n/a"
        print(f"{label:>18} {count:>8} {share:>11} {usable:>8}")

    print(f"\nsizing against a ${args.price:.4f} contract:")
    print(f"  {'band':>14} {'P(win)':>9} {'edge':>9} {'half-Kelly':>12} {'growth':>11}")
    for index, count in enumerate(curve.counts):
        if count < 30:
            continue
        probability = curve.probabilities[index]
        stake = kelly_fraction(probability, args.price)
        growth = log_growth_per_trade(probability, args.price, stake) if stake else 0.0
        low, high = curve.edges_bps[index], curve.edges_bps[index + 1]
        label = f"{low:g}-{'+' if high >= 1e9 else format(high, 'g')}"
        print(
            f"  {label:>14} {probability * 100:>8.1f}% {probability - args.price:>+9.4f} "
            f"{stake * 100:>11.2f}% {growth:>+11.5f}"
        )
    print(
        "\nA 0.00% stake is not a bug: it means this band shows no edge over the\n"
        "price you would have to pay, so the correct position size is nothing."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
