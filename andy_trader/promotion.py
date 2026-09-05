"""Pure promotion gate evaluating candidate holdout performance against baseline:base_rate.

This is safety-critical: a model is only promoted if it proves out-of-sample skill
over an identical holdout window against the hardest dumb baseline to beat.
"""

from __future__ import annotations

from dataclasses import dataclass

from andy_trader.calibration import CalibrationReport

DEFAULT_MINIMUM_HOLDOUT_BARS = 24


@dataclass(frozen=True)
class PromotionDecision:
    """The deterministic verdict on whether a retrained candidate clears the live bar."""

    promoted: bool
    candidate_skill: float
    base_rate_skill: float
    candidate_brier: float
    base_rate_brier: float
    holdout_bars: int
    reason: str


def evaluate_promotion_gate(
    *,
    candidate_report: CalibrationReport,
    base_rate_report: CalibrationReport,
    minimum_holdout_bars: int = DEFAULT_MINIMUM_HOLDOUT_BARS,
) -> PromotionDecision:
    """Pure function deciding if a candidate model clears promotion.

    Zero I/O, zero side effects.

    Conditions for promotion:
    1. Holdout sample size >= minimum_holdout_bars (default 24).
    2. Both reports must evaluate identical sample counts.
    3. Neither report can be degenerate (all outcomes identical).
    4. Candidate Brier skill score must be strictly positive (> 0.0).
    5. Candidate Brier skill score must strictly exceed baseline:base_rate's skill score.
    6. Candidate raw Brier score must be strictly lower than baseline:base_rate's raw Brier score.
    """
    c_count = candidate_report.count
    b_count = base_rate_report.count

    if c_count < minimum_holdout_bars:
        return PromotionDecision(
            promoted=False,
            candidate_skill=candidate_report.brier_skill_score,
            base_rate_skill=base_rate_report.brier_skill_score,
            candidate_brier=candidate_report.brier,
            base_rate_brier=base_rate_report.brier,
            holdout_bars=c_count,
            reason=(
                f"Holdout sample too small: {c_count} bars < minimum {minimum_holdout_bars}"
            ),
        )

    if c_count != b_count:
        return PromotionDecision(
            promoted=False,
            candidate_skill=candidate_report.brier_skill_score,
            base_rate_skill=base_rate_report.brier_skill_score,
            candidate_brier=candidate_report.brier,
            base_rate_brier=base_rate_report.brier,
            holdout_bars=c_count,
            reason=(
                f"Mismatched holdout counts: candidate evaluated on {c_count} bars "
                f"vs base_rate on {b_count} bars"
            ),
        )

    if candidate_report.degenerate or base_rate_report.degenerate:
        return PromotionDecision(
            promoted=False,
            candidate_skill=candidate_report.brier_skill_score,
            base_rate_skill=base_rate_report.brier_skill_score,
            candidate_brier=candidate_report.brier,
            base_rate_brier=base_rate_report.brier,
            holdout_bars=c_count,
            reason=(
                "Holdout sample is degenerate (all outcomes went the same way); "
                "insufficient evidence to evaluate skill"
            ),
        )

    if candidate_report.brier_skill_score <= 0.0:
        return PromotionDecision(
            promoted=False,
            candidate_skill=candidate_report.brier_skill_score,
            base_rate_skill=base_rate_report.brier_skill_score,
            candidate_brier=candidate_report.brier,
            base_rate_brier=base_rate_report.brier,
            holdout_bars=c_count,
            reason=(
                f"Candidate has non-positive Brier skill score: "
                f"{candidate_report.brier_skill_score:+.4f} <= 0.0"
            ),
        )

    if candidate_report.brier_skill_score <= base_rate_report.brier_skill_score:
        return PromotionDecision(
            promoted=False,
            candidate_skill=candidate_report.brier_skill_score,
            base_rate_skill=base_rate_report.brier_skill_score,
            candidate_brier=candidate_report.brier,
            base_rate_brier=base_rate_report.brier,
            holdout_bars=c_count,
            reason=(
                f"Candidate skill ({candidate_report.brier_skill_score:+.4f}) does not beat "
                f"baseline:base_rate ({base_rate_report.brier_skill_score:+.4f})"
            ),
        )

    if candidate_report.brier >= base_rate_report.brier:
        return PromotionDecision(
            promoted=False,
            candidate_skill=candidate_report.brier_skill_score,
            base_rate_skill=base_rate_report.brier_skill_score,
            candidate_brier=candidate_report.brier,
            base_rate_brier=base_rate_report.brier,
            holdout_bars=c_count,
            reason=(
                f"Candidate raw Brier ({candidate_report.brier:.4f}) does not beat "
                f"baseline:base_rate ({base_rate_report.brier:.4f})"
            ),
        )

    return PromotionDecision(
        promoted=True,
        candidate_skill=candidate_report.brier_skill_score,
        base_rate_skill=base_rate_report.brier_skill_score,
        candidate_brier=candidate_report.brier,
        base_rate_brier=base_rate_report.brier,
        holdout_bars=c_count,
        reason=(
            f"Candidate cleared promotion gate: skill {candidate_report.brier_skill_score:+.4f} "
            f"> base_rate {base_rate_report.brier_skill_score:+.4f} "
            f"(Brier {candidate_report.brier:.4f} < {base_rate_report.brier:.4f}) "
            f"over {c_count} holdout bars"
        ),
    )
