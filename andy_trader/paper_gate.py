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
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from andy_trader.calibration import CalibrationError, evaluate
from andy_trader.store import fetch_settled

# A predictor needs a real sample before its score means anything. At 1h across
# the live instruments this is a few weeks of history, which is the point: the
# gate should be slow to open, and it costs nothing to wait because the thing
# being withheld is permission to lose money.
MINIMUM_SETTLED_CALLS = 200


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


def evaluate_paper_eligibility(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    horizon: str = "1h",
    minimum_calls: int = MINIMUM_SETTLED_CALLS,
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

    return EligibilityVerdict(
        eligible=True,
        reason=(
            f"{predictor} on {instrument} beats the base rate: "
            f"{report.brier_skill_score:+.4f} skill over {sample_size} calls"
        ),
        predictor=predictor,
        instrument=instrument,
        sample_size=sample_size,
        brier_skill_score=report.brier_skill_score,
        hit_rate=report.hit_rate,
    )


def main(argv: "Sequence[str] | None" = None) -> int:
    """Report which (predictor, instrument) pairs are currently allowed to trade."""

    import argparse
    from pathlib import Path

    from andy_trader.store import connect, default_database_path

    parser = argparse.ArgumentParser(description="Who has earned the right to trade?")
    parser.add_argument("--horizon", default="1h")
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument("--predictor", help="Limit to one predictor")
    args = parser.parse_args(argv)

    db_path = Path(args.database) if args.database else default_database_path()
    connection = connect(db_path)

    pairs = connection.execute(
        """
        SELECT DISTINCT predictor, instrument FROM crypto_predictions
        WHERE settled_at IS NOT NULL AND horizon = ?
        ORDER BY predictor, instrument
        """,
        (args.horizon,),
    ).fetchall()

    print(f"{'predictor':<28} {'instrument':<11} {'n':>5} {'skill':>9} {'hit':>7}  verdict")
    allowed = 0
    for row in pairs:
        if args.predictor and row["predictor"] != args.predictor:
            continue
        verdict = evaluate_paper_eligibility(
            connection,
            predictor=row["predictor"],
            instrument=row["instrument"],
            horizon=args.horizon,
        )
        skill = (
            f"{verdict.brier_skill_score:+.4f}"
            if verdict.brier_skill_score is not None
            else "--"
        )
        hit = f"{verdict.hit_rate:.1%}" if verdict.hit_rate is not None else "--"
        mark = "MAY TRADE" if verdict.eligible else "blocked"
        allowed += 1 if verdict.eligible else 0
        print(
            f"{row['predictor']:<28} {row['instrument']:<11} {verdict.sample_size:>5} "
            f"{skill:>9} {hit:>7}  {mark}"
        )
    print(f"\n{allowed} pair(s) currently allowed to open new positions.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    from typing import Sequence  # noqa: F401 - referenced by main's annotation

    sys.exit(main())
