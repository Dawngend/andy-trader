"""A skill gate on paper trading: earn the right to deploy capital first.

This exists because of a real hole found on 2026-09-06. CT-07 gates ML models
hard -- a candidate model may not serve until it beats `baseline:base_rate` on
a holdout. But paper trading a *baseline* was gated by nothing at all. It was a
config string, so any predictor named in `--paper-trade` began deploying capital
immediately and kept deploying it no matter how badly it scored.

What that cost, measured on the live book:

    baseline:momentum   2,818 settled calls
                        Brier skill  -0.0390  (worse than base_rate's -0.0269)
                        hit rate      47.1%   (worse than a coin flip)
                        paper result  -3.04%, 0 winners across 8 instruments

Its calibration bins say why, and it is not a cost problem. When momentum was
most confident price would FALL (mean call 0.36) price actually rose 56% of the
time; when most confident price would RISE (0.63) price rose only 55%, below the
57.7% base rate. At the 1h horizon this signal is not weak, it is inverted.

The rule here is deliberately the same bar CT-07 already applies to models:
beat the base rate on a real sample, or do not trade. Nothing about a predictor
being hand-written rather than learned earns it a lower standard.

Note the fix is NOT "momentum is backwards, so invert it". A 47% hit rate over
one regime is not a licence to trade 53% the other way; that is fitting the
sign to the sample, which is the same error as trusting a stranger's screenshot.
If an inverted momentum has an edge it can be added as its own predictor, log
its own calls, and clear this same gate on its own evidence.

A SECOND hole was found the same day, once the first pair ever cleared this
gate: `baseline:momentum` on LINK-USD reached 200 settled 1h calls at +0.0006
Brier skill and a 57.8% hit rate -- genuinely better than the base rate, by the
only test this module originally ran. But `andy_trader.economics` had already
established that 1h round-trip costs are ~30bps against LINK-USD's own ~46bps
average move, which needs an **82.6%** hit rate just to break even. Beating the
base rate is a statistical claim; covering costs is an economic one, and they
are not the same test. A predictor can clear the first and fail the second by a
wide margin, and until this fix the gate could not tell the difference. It now
checks both, because a pair that would still lose money net has not actually
earned anything.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from andy_trader.calibration import CalibrationError, evaluate
from andy_trader.economics import DEFAULT_ROUND_TRIP_BPS, evaluate_horizon
from andy_trader.store import FAST_HORIZONS, fetch_settled

# A predictor needs a real sample before its score means anything. At 1h across
# the live instruments this is a few weeks of history, which is the point: the
# gate should be slow to open, and it costs nothing to wait because the thing
# being withheld is permission to lose money.
MINIMUM_SETTLED_CALLS = 200


def _observation_interval_for(horizon: str) -> str:
    """The price-observation interval a horizon's economics must be measured
    against. Only the sub-hourly fast-continuation horizons diverge from their
    own name: they predict a horizon (e.g. "2m") for which no observation
    series exists, trading instead on the close of a 1m bar."""

    return "1m" if horizon in FAST_HORIZONS else horizon


@dataclass(frozen=True)
class EligibilityVerdict:
    """Whether a predictor has earned the right to open new paper positions."""

    eligible: bool
    reason: str
    predictor: str
    instrument: str
    sample_size: int
    brier_skill_score: float | None = None
    hit_rate: float | None = None
    break_even_win_rate: float | None = None


def evaluate_paper_eligibility(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    horizon: str = "1h",
    minimum_calls: int = MINIMUM_SETTLED_CALLS,
    round_trip_bps: float = DEFAULT_ROUND_TRIP_BPS,
) -> EligibilityVerdict:
    """Decide whether `predictor` may open new positions in `instrument`.

    Judged per instrument, not per predictor, because "works on BTC" and "works
    on DOGE" are different claims and a predictor that is carried by one
    instrument should not get to trade the other seven on its reputation.
    """

    rows = fetch_settled(
        connection, predictor=predictor, instrument=instrument, horizon=horizon
    )
    sample_size = len(rows)
    if sample_size < minimum_calls:
        return EligibilityVerdict(
            eligible=False,
            reason=(
                f"only {sample_size} settled {horizon} calls for {predictor} on "
                f"{instrument}; needs {minimum_calls} before its score means anything"
            ),
            predictor=predictor,
            instrument=instrument,
            sample_size=sample_size,
        )

    probabilities = [float(row["probability_up"]) for row in rows]
    outcomes = [int(row["outcome_up"]) for row in rows]
    try:
        report = evaluate(probabilities, outcomes)
    except CalibrationError as exc:
        return EligibilityVerdict(
            eligible=False,
            reason=f"cannot score {predictor} on {instrument}: {exc}",
            predictor=predictor,
            instrument=instrument,
            sample_size=sample_size,
        )

    if report.degenerate:
        return EligibilityVerdict(
            eligible=False,
            reason=(
                f"{predictor} on {instrument} has a degenerate sample "
                f"(every outcome identical); skill is undefined, not proven"
            ),
            predictor=predictor,
            instrument=instrument,
            sample_size=sample_size,
            hit_rate=report.hit_rate,
        )

    if not report.beats_base_rate:
        return EligibilityVerdict(
            eligible=False,
            reason=(
                f"{predictor} on {instrument} scores {report.brier_skill_score:+.4f} "
                f"Brier skill over {sample_size} calls (hit rate {report.hit_rate:.1%}); "
                f"it does not beat always predicting the base rate, so it has not "
                f"earned the right to deploy capital"
            ),
            predictor=predictor,
            instrument=instrument,
            sample_size=sample_size,
            brier_skill_score=report.brier_skill_score,
            hit_rate=report.hit_rate,
        )

    # Beating the base rate is a STATISTICAL claim. Covering the cost of
    # trading is an ECONOMIC one, and they are not the same test: a predictor
    # can be genuinely better than guessing and still lose money on every
    # trade if its edge is smaller than what a round trip costs. Found for
    # real on 2026-09-06 -- baseline:momentum cleared the statistical bar on
    # LINK-USD at 1h (+0.0006 skill, 57.8% hit rate) while needing an 82.6%
    # hit rate just to break even against that horizon's own average move.
    econ = evaluate_horizon(
        connection,
        instrument=instrument,
        interval=_observation_interval_for(horizon),
        round_trip_bps=round_trip_bps,
    )
    if econ is None:
        return EligibilityVerdict(
            eligible=False,
            reason=(
                f"{predictor} on {instrument} beats the base rate statistically, but "
                f"there is not enough {_observation_interval_for(horizon)} price history "
                f"to verify it can cover a round trip's cost -- statistical evidence "
                f"without economic verification is not enough to deploy capital"
            ),
            predictor=predictor,
            instrument=instrument,
            sample_size=sample_size,
            brier_skill_score=report.brier_skill_score,
            hit_rate=report.hit_rate,
        )

    if report.hit_rate < econ.break_even_win_rate:
        return EligibilityVerdict(
            eligible=False,
            reason=(
                f"{predictor} on {instrument} beats the base rate ({report.brier_skill_score:+.4f} "
                f"skill) but its {report.hit_rate:.1%} hit rate is below the "
                f"{econ.break_even_win_rate:.1%} needed to cover a {econ.round_trip_bps:.0f}bps "
                f"round trip against this horizon's {econ.average_move_bps:.1f}bps average move -- "
                f"statistically better than guessing is not the same as economically profitable"
            ),
            predictor=predictor,
            instrument=instrument,
            sample_size=sample_size,
            brier_skill_score=report.brier_skill_score,
            hit_rate=report.hit_rate,
            break_even_win_rate=econ.break_even_win_rate,
        )

    return EligibilityVerdict(
        eligible=True,
        reason=(
            f"{predictor} on {instrument} beats the base rate ({report.brier_skill_score:+.4f} "
            f"skill) AND its {report.hit_rate:.1%} hit rate clears the "
            f"{econ.break_even_win_rate:.1%} needed to cover costs, over {sample_size} calls"
        ),
        predictor=predictor,
        instrument=instrument,
        sample_size=sample_size,
        brier_skill_score=report.brier_skill_score,
        hit_rate=report.hit_rate,
        break_even_win_rate=econ.break_even_win_rate,
    )


def main(argv: "Sequence[str] | None" = None) -> int:
    """Report which (predictor, instrument) pairs are currently allowed to trade."""

    import argparse
    from pathlib import Path

    from andy_trader.store import connect, default_database_path

    parser = argparse.ArgumentParser(description="Who has earned the right to trade?")
    parser.add_argument(
        "--horizon",
        help=(
            "Limit to one horizon, e.g. 1h or 2m. Omit to report every horizon "
            "with settled history -- the 2m fast-continuation strategy and the "
            "1h baselines are different questions and both belong on this list."
        ),
    )
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument("--predictor", help="Limit to one predictor")
    parser.add_argument(
        "--propose",
        type=int,
        metavar="N",
        help=(
            "Rank candidates and report whether the top N have earned a real "
            "deployment. Ranking alone is not evidence: see the selection premium "
            "this prints."
        ),
    )
    parser.add_argument(
        "--stake-php",
        type=float,
        default=1000.0,
        help="Stake per selected instrument, in pesos (default 1000)",
    )
    parser.add_argument("--php-per-usd", type=float, default=62.62)
    args = parser.parse_args(argv)

    db_path = Path(args.database) if args.database else default_database_path()
    connection = connect(db_path)

    if args.horizon:
        pairs = connection.execute(
            """
            SELECT DISTINCT predictor, instrument, horizon FROM crypto_predictions
            WHERE settled_at IS NOT NULL AND horizon = ?
            ORDER BY predictor, instrument
            """,
            (args.horizon,),
        ).fetchall()
    else:
        pairs = connection.execute(
            """
            SELECT DISTINCT predictor, instrument, horizon FROM crypto_predictions
            WHERE settled_at IS NOT NULL
            ORDER BY horizon, predictor, instrument
            """
        ).fetchall()

    print(
        f"{'predictor':<28} {'instrument':<11} {'horizon':>7} {'n':>5} {'skill':>9} "
        f"{'hit':>7} {'need':>7}  verdict"
    )
    allowed = 0
    for row in pairs:
        if args.predictor and row["predictor"] != args.predictor:
            continue
        verdict = evaluate_paper_eligibility(
            connection,
            predictor=row["predictor"],
            instrument=row["instrument"],
            horizon=row["horizon"],
        )
        skill = (
            f"{verdict.brier_skill_score:+.4f}"
            if verdict.brier_skill_score is not None
            else "--"
        )
        hit = f"{verdict.hit_rate:.1%}" if verdict.hit_rate is not None else "--"
        need = (
            f"{verdict.break_even_win_rate:.1%}"
            if verdict.break_even_win_rate is not None
            else "--"
        )
        mark = "MAY TRADE" if verdict.eligible else "blocked"
        allowed += 1 if verdict.eligible else 0
        print(
            f"{row['predictor']:<28} {row['instrument']:<11} {row['horizon']:>7} "
            f"{verdict.sample_size:>5} {skill:>9} {hit:>7} {need:>7}  {mark}"
        )
    print(f"\n{allowed} pair(s) currently allowed to open new positions.")

    if args.propose:
        _propose(
            connection,
            pairs=pairs,
            wanted=args.propose,
            stake_php=args.stake_php,
            php_per_usd=args.php_per_usd,
            only_predictor=args.predictor,
        )
    return 0


def _propose(
    connection: sqlite3.Connection,
    *,
    pairs: "list",
    wanted: int,
    stake_php: float,
    php_per_usd: float,
    only_predictor: str | None,
) -> None:
    """Rank candidates and say plainly whether a deployment is justified yet.

    The warning this prints is the whole point. Ranking N candidates and funding
    the best few is a selection procedure, and selection manufactures apparent
    edge out of nothing: the maximum of N noisy measurements sits above the truth
    even when every candidate's true edge is exactly zero. Funding the top of a
    leaderboard is therefore not the same as funding something that works, and
    the difference is invisible unless you say it out loud.
    """

    import math

    verdicts = []
    for row in pairs:
        if only_predictor and row["predictor"] != only_predictor:
            continue
        verdict = evaluate_paper_eligibility(
            connection,
            predictor=row["predictor"],
            instrument=row["instrument"],
            # Each row's own horizon, not one shared value: pairs can now span
            # multiple horizons (2m fast-continuation alongside 1h baselines),
            # and scoring a 2m predictor's calls as if they were 1h would
            # silently answer a different question than the one being asked.
            horizon=row["horizon"],
        )
        if verdict.brier_skill_score is not None:
            verdicts.append(verdict)

    stake_usd = round(stake_php / php_per_usd, 2)
    total_php = stake_php * wanted

    print("\n" + "=" * 74)
    print(
        f"PROPOSED DEPLOYMENT: top {wanted} at PHP {stake_php:,.0f} each "
        f"(${stake_usd} each, PHP {total_php:,.0f} total)"
    )
    print("=" * 74)

    if not verdicts:
        print(
            "\nNothing is scoreable yet -- every candidate is still below the minimum\n"
            f"sample of {MINIMUM_SETTLED_CALLS} settled calls. There is no ranking to make."
        )
        print("\nRECOMMENDATION: deploy nothing. Wait for evidence.")
        return

    ranked = sorted(verdicts, key=lambda v: v.brier_skill_score or -9e9, reverse=True)
    candidates = len(ranked)

    # Expected maximum of N standard normals: how much apparent edge pure
    # selection hands you for free. Sound approximation for small N.
    selection_premium = (
        math.sqrt(2.0 * math.log(candidates)) if candidates > 1 else 0.0
    )

    print(f"\n{'rank':>4} {'predictor':<26} {'instrument':<11} {'skill':>9}  eligible")
    for index, verdict in enumerate(ranked[:wanted], start=1):
        print(
            f"{index:>4} {verdict.predictor:<26} {verdict.instrument:<11} "
            f"{verdict.brier_skill_score:>+9.4f}  {'yes' if verdict.eligible else 'NO'}"
        )

    qualified = [v for v in ranked[:wanted] if v.eligible]
    print(
        f"\nSelection premium: ranking {candidates} candidates and taking the best "
        f"inflates\napparent skill by roughly {selection_premium:.2f} standard errors "
        "even when every\ncandidate's true edge is exactly zero. A leaderboard is not evidence."
    )

    if len(qualified) < wanted:
        print(
            f"\nRECOMMENDATION: deploy nothing. {len(qualified)} of {wanted} proposed "
            f"instruments\nclear the gate on their own evidence. Being top-{wanted} of "
            f"{candidates} losers is still a loser."
        )
        return

    print(
        f"\n{wanted} of {wanted} clear the gate independently. Before funding these with "
        "real money,\nvalidate them on a FRESH window they were not selected on: if the "
        "edge was\nselection noise it disappears in round two, and if it is real it survives."
    )


if __name__ == "__main__":  # pragma: no cover
    import sys
    from typing import Sequence  # noqa: F401 - referenced by main's annotation

    sys.exit(main())
