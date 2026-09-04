"""Paper bankrolls with a hard retirement rule: lose enough and the predictor stops trading.

The idea is borrowed from a genuinely good instinct: a desk that has to pay its
own bill or get unplugged cannot quietly bleed while you tell yourself it is
learning. A hard constraint forces the question "is this actually working" to be
answered on a schedule rather than whenever it becomes convenient.

**Two failure modes this module is built to refuse.**

*Survivorship bias.* Kill the losers, report only the survivors, and a purely
random process produces a table of winners. This is precisely how the trading
screenshots people post acquire their credibility: you are shown the desk that
lived, never the nineteen that did not. So `SurvivalReport` always carries the
retired alongside the living, `format_report` prints the graveyard, and there is
deliberately no option to suppress it.

*The scorer being judged by what it scores.* Survival pressure must act on the
strategy and never on the measurement, or the cheapest route to survival becomes
flattering the numbers. This module therefore only ever **reads** settled
outcomes. It cannot write a prediction, cannot settle one, and cannot alter a
calibration report. It is a consumer of the record, not a participant in it.

Nothing here places an order or touches money. A bankroll is a number in memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

# Ruin is set below zero-crossing on purpose. A bankroll at 20% of its starting
# value is not "nearly dead", it is dead: recovering from an 80% drawdown needs a
# 400% gain, which no honest short-horizon strategy is going to produce.
DEFAULT_RUIN_FRACTION = 0.20
DEFAULT_STARTING_BANKROLL = 1.0
# A predictor must survive at least this many resolved trades before retirement
# is meaningful. Below it, ruin is a statement about variance, not skill.
DEFAULT_GRACE_TRADES = 30


class SurvivalError(ValueError):
    """Raised when a survival run is configured so that its verdict would be meaningless."""


@dataclass
class Bankroll:
    """One predictor's paper capital and its life or death."""

    predictor: str
    equity: float = DEFAULT_STARTING_BANKROLL
    peak: float = DEFAULT_STARTING_BANKROLL
    trades: int = 0
    wins: int = 0
    retired_after_trade: int | None = None
    retired_at_equity: float | None = None

    @property
    def alive(self) -> bool:
        return self.retired_after_trade is None

    @property
    def max_drawdown(self) -> float:
        return 0.0 if self.peak <= 0 else (self.peak - self.equity) / self.peak

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


@dataclass(frozen=True)
class SurvivalReport:
    """The living and the dead. Both, always."""

    survivors: Sequence[Bankroll]
    retired: Sequence[Bankroll]
    trades_evaluated: int
    ruin_fraction: float
    grace_trades: int

    @property
    def population(self) -> int:
        return len(self.survivors) + len(self.retired)

    @property
    def survival_rate(self) -> float:
        return len(self.survivors) / self.population if self.population else 0.0

    def as_dict(self) -> dict[str, object]:
        def row(b: Bankroll) -> dict[str, object]:
            return {
                "predictor": b.predictor,
                "equity": b.equity,
                "trades": b.trades,
                "win_rate": b.win_rate,
                "max_drawdown": b.max_drawdown,
                "alive": b.alive,
                "retired_after_trade": b.retired_after_trade,
            }

        return {
            "survivors": [row(b) for b in self.survivors],
            "retired": [row(b) for b in self.retired],
            "population": self.population,
            "survival_rate": self.survival_rate,
            "trades_evaluated": self.trades_evaluated,
            "ruin_fraction": self.ruin_fraction,
            "grace_trades": self.grace_trades,
        }


@dataclass(frozen=True)
class ResolvedTrade:
    """One settled call, reduced to what survival accounting needs."""

    predictor: str
    probability_up: float
    outcome_up: int
    reference_price: float
    settle_price: float


def run_survival(
    trades: Sequence[ResolvedTrade],
    *,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
    ruin_fraction: float = DEFAULT_RUIN_FRACTION,
    grace_trades: int = DEFAULT_GRACE_TRADES,
    fee_bps: float = 10.0,
    slippage_bps: float = 5.0,
    stake_fraction: float = 0.10,
) -> SurvivalReport:
    """Walk settled trades in order, compounding each predictor's paper bankroll.

    A predictor that falls to `ruin_fraction` of its starting bankroll, having
    taken at least `grace_trades`, is retired and takes no further trades. It
    stays in the report.

    `stake_fraction` is a fixed fraction of current equity, not a Kelly bet.
    Fixed fractional staking cannot reach exactly zero, which is why ruin is a
    threshold rather than a zero-crossing.
    """

    if not 0.0 < ruin_fraction < 1.0:
        raise SurvivalError(f"ruin_fraction must be between 0 and 1, got {ruin_fraction!r}")
    if not 0.0 < stake_fraction <= 1.0:
        raise SurvivalError(f"stake_fraction must be in (0, 1], got {stake_fraction!r}")
    if starting_bankroll <= 0:
        raise SurvivalError("starting_bankroll must be positive")
    if grace_trades < 0:
        raise SurvivalError("grace_trades cannot be negative")
    if fee_bps < 0 or slippage_bps < 0:
        raise SurvivalError("costs cannot be negative")

    cost = 2.0 * (fee_bps + slippage_bps) / 10_000.0
    ruin_level = starting_bankroll * ruin_fraction
    books: dict[str, Bankroll] = {}
    evaluated = 0

    for trade in trades:
        book = books.setdefault(
            trade.predictor,
            Bankroll(trade.predictor, equity=starting_bankroll, peak=starting_bankroll),
        )
        if not book.alive:
            continue
        direction = 1 if trade.probability_up > 0.5 else -1 if trade.probability_up < 0.5 else 0
        if direction == 0:
            continue  # No position taken, so no capital at risk and no trade recorded.
        if trade.reference_price <= 0:
            raise SurvivalError(f"non-positive reference price in {trade.predictor} trade")

        evaluated += 1
        book.trades += 1
        asset_return = (trade.settle_price - trade.reference_price) / trade.reference_price
        realised = direction * asset_return - cost
        if direction == (1 if trade.outcome_up else -1):
            book.wins += 1
        book.equity *= 1.0 + stake_fraction * realised
        book.peak = max(book.peak, book.equity)

        if book.equity <= ruin_level and book.trades >= grace_trades:
            book.retired_after_trade = book.trades
            book.retired_at_equity = book.equity

    survivors = sorted(
        (b for b in books.values() if b.alive), key=lambda b: b.equity, reverse=True
    )
    retired = sorted(
        (b for b in books.values() if not b.alive), key=lambda b: b.equity, reverse=True
    )
    return SurvivalReport(
        survivors=survivors,
        retired=retired,
        trades_evaluated=evaluated,
        ruin_fraction=ruin_fraction,
        grace_trades=grace_trades,
    )


def format_report(report: SurvivalReport) -> str:
    """Print the living and the dead. The graveyard is not optional."""

    lines = [
        f"Survival run over {report.trades_evaluated} resolved trades",
        f"ruin at {report.ruin_fraction:.0%} of starting bankroll, "
        f"grace period {report.grace_trades} trades",
        "",
        f"{'predictor':<26}{'equity':>10}{'trades':>8}{'win rate':>10}{'drawdown':>10}  state",
        "-" * 72,
    ]
    for book in report.survivors:
        lines.append(
            f"{book.predictor:<26}{book.equity:>10.4f}{book.trades:>8}"
            f"{book.win_rate:>10.2%}{book.max_drawdown:>10.2%}  alive"
        )
    for book in report.retired:
        lines.append(
            f"{book.predictor:<26}{book.equity:>10.4f}{book.trades:>8}"
            f"{book.win_rate:>10.2%}{book.max_drawdown:>10.2%}  "
            f"RETIRED after trade {book.retired_after_trade}"
        )

    lines.append("")
    lines.append(
        f"survival rate {report.survival_rate:.0%} "
        f"({len(report.survivors)} of {report.population})"
    )
    if report.retired:
        lines.append(
            "The retired are listed above deliberately. Reporting only survivors turns a "
            "random\nprocess into a table of winners, which is the whole trick behind every "
            "trading screenshot."
        )
    if report.trades_evaluated < report.grace_trades:
        lines.append("")
        lines.append(
            f"WARNING: only {report.trades_evaluated} trades resolved, below the "
            f"{report.grace_trades}-trade grace period.\nNobody can die yet and nobody has "
            "earned the right to be called a survivor. This is a\nsmoke test of the "
            "mechanism, not a verdict on any predictor."
        )
    return "\n".join(lines)
