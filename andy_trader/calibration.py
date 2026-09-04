"""Score dated predictions: Brier, its Murphy decomposition, reliability, and hit rate."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

DEFAULT_BINS = 10


class CalibrationError(ValueError):
    """Raised when scoring is asked to summarise something it cannot summarise."""


@dataclass(frozen=True)
class Bin:
    lower: float
    upper: float
    count: int
    mean_probability: float
    observed_rate: float

    @property
    def gap(self) -> float:
        """Signed calibration gap. Positive means the predictor was over-confident."""

        return self.mean_probability - self.observed_rate


@dataclass(frozen=True)
class CalibrationReport:
    """Everything needed to answer 'is this better than guessing, and by how much'."""

    count: int
    base_rate: float
    brier: float
    brier_reference: float
    brier_skill_score: float
    reliability: float
    resolution: float
    uncertainty: float
    expected_calibration_error: float
    hit_rate: float
    bins: Sequence[Bin]
    degenerate: bool = False

    @property
    def beats_base_rate(self) -> bool:
        """Degenerate samples cannot be beaten, so they never count as a win."""

        return not self.degenerate and self.brier_skill_score > 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "base_rate": self.base_rate,
            "brier": self.brier,
            "brier_reference": self.brier_reference,
            "brier_skill_score": self.brier_skill_score,
            "reliability": self.reliability,
            "resolution": self.resolution,
            "uncertainty": self.uncertainty,
            "expected_calibration_error": self.expected_calibration_error,
            "hit_rate": self.hit_rate,
            "degenerate": self.degenerate,
            "beats_base_rate": self.beats_base_rate,
            "bins": [
                {
                    "lower": b.lower,
                    "upper": b.upper,
                    "count": b.count,
                    "mean_probability": b.mean_probability,
                    "observed_rate": b.observed_rate,
                    "gap": b.gap,
                }
                for b in self.bins
            ],
        }


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    _validate(probabilities, outcomes)
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes)) / len(outcomes)


def evaluate(
    probabilities: Sequence[float],
    outcomes: Sequence[int],
    *,
    bins: int = DEFAULT_BINS,
) -> CalibrationReport:
    """Full scoring pass over one predictor's settled calls.

    The number that matters is `brier_skill_score`, not `brier`. A raw Brier of
    0.24 sounds respectable and is worthless if the base rate alone scores 0.24;
    the skill score is what says whether the predictor knows anything the base
    rate does not. `hit_rate` is reported because people ask for it, but it
    throws away the confidence and should never be the headline.
    """

    _validate(probabilities, outcomes)
    if bins < 1:
        raise CalibrationError(f"bins must be >= 1, got {bins}")

    n = len(outcomes)
    base_rate = sum(outcomes) / n
    score = brier_score(probabilities, outcomes)

    # Reference: predict the base rate every time. This is the bar. Note it is
    # computed in hindsight and so slightly flatters the reference, which makes
    # it a conservative comparison rather than a generous one.
    reference = sum((base_rate - o) ** 2 for o in outcomes) / n

    # A sample where every outcome went the same way makes the reference
    # predictor perfect, so the skill ratio divides by zero. Reporting 0.0 there
    # would rank a perfect predictor and a bad one identically, which is exactly
    # the kind of flattering number this module exists to prevent. Flag it
    # instead: with a handful of settled calls an all-up run is common, and it
    # means "not enough evidence", not "no skill".
    degenerate = reference == 0.0
    if degenerate:
        skill = 0.0 if score == 0.0 else float("-inf")
    else:
        skill = 1.0 - (score / reference)

    grouped = _group(probabilities, outcomes, bins)
    uncertainty = base_rate * (1.0 - base_rate)
    reliability = sum(b.count * (b.mean_probability - b.observed_rate) ** 2 for b in grouped) / n
    resolution = sum(b.count * (b.observed_rate - base_rate) ** 2 for b in grouped) / n
    ece = sum(b.count * abs(b.mean_probability - b.observed_rate) for b in grouped) / n

    # A p of exactly 0.5 is not a directional call. Score only the decisive ones
    # and credit each tie as half a hit. Counting ties inside `decisive_hits`
    # would silently award every tie whose outcome happened to be down, because
    # `0.5 > 0.5` is False and False == bool(0).
    decisive_hits = sum(
        1 for p, o in zip(probabilities, outcomes) if p != 0.5 and (p > 0.5) == bool(o)
    )
    ties = sum(1 for p in probabilities if p == 0.5)
    hit_rate = (decisive_hits + ties * 0.5) / n

    return CalibrationReport(
        count=n,
        base_rate=base_rate,
        brier=score,
        brier_reference=reference,
        brier_skill_score=skill,
        reliability=reliability,
        resolution=resolution,
        uncertainty=uncertainty,
        expected_calibration_error=ece,
        hit_rate=hit_rate,
        bins=grouped,
        degenerate=degenerate,
    )


def _group(probabilities: Sequence[float], outcomes: Sequence[int], bins: int) -> list[Bin]:
    width = 1.0 / bins
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, o in zip(probabilities, outcomes):
        index = min(int(p / width), bins - 1)
        buckets[index].append((p, o))
    grouped: list[Bin] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        count = len(bucket)
        grouped.append(
            Bin(
                lower=index * width,
                upper=(index + 1) * width,
                count=count,
                mean_probability=sum(p for p, _ in bucket) / count,
                observed_rate=sum(o for _, o in bucket) / count,
            )
        )
    return grouped


def _validate(probabilities: Sequence[float], outcomes: Sequence[int]) -> None:
    if len(probabilities) != len(outcomes):
        raise CalibrationError(
            f"probabilities and outcomes differ in length: {len(probabilities)} vs {len(outcomes)}"
        )
    if not outcomes:
        raise CalibrationError("Cannot score an empty set of predictions")
    for p in probabilities:
        if not 0.0 <= p <= 1.0 or math.isnan(p):
            raise CalibrationError(f"probability out of range: {p!r}")
    for o in outcomes:
        if o not in (0, 1):
            raise CalibrationError(f"outcome must be 0 or 1, got {o!r}")


def format_report(report: CalibrationReport, *, predictor: str = "") -> str:
    """Human-readable summary. The verdict line is deliberately blunt."""

    header = f"Calibration report{f' for {predictor}' if predictor else ''}"
    if report.degenerate:
        verdict = "UNDECIDABLE, every outcome went the same way"
    elif report.beats_base_rate:
        verdict = "BEATS the base rate"
    else:
        verdict = "does NOT beat the base rate"
    lines = [
        header,
        "=" * len(header),
        f"predictions scored     {report.count}",
        f"base rate (share up)   {report.base_rate:.4f}",
        f"Brier score            {report.brier:.4f}   (lower is better)",
        f"Brier, base rate only  {report.brier_reference:.4f}",
        f"Brier skill score      {report.brier_skill_score:+.4f}   -> {verdict}",
        "",
        "Murphy decomposition   Brier = reliability - resolution + uncertainty",
        f"  reliability          {report.reliability:.4f}   (0 is perfectly calibrated)",
        f"  resolution           {report.resolution:.4f}   (higher means more informative)",
        f"  uncertainty          {report.uncertainty:.4f}   (fixed by the data, not the model)",
        f"expected calib. error  {report.expected_calibration_error:.4f}",
        f"hit rate               {report.hit_rate:.4f}   (ignores confidence; not the headline)",
        "",
        "Reliability curve",
        f"  {'bucket':<14}{'n':>6}{'mean p':>10}{'observed':>10}{'gap':>9}",
    ]
    for b in report.bins:
        bucket = f"[{b.lower:.1f}, {b.upper:.1f})"
        lines.append(
            f"  {bucket:<14}{b.count:>6}{b.mean_probability:>10.4f}"
            f"{b.observed_rate:>10.4f}{b.gap:>+9.4f}"
        )
    if report.degenerate:
        lines.append("")
        lines.append(
            "WARNING: every settled outcome went the same way, so predicting the base "
            "rate is already perfect and the skill score is undefined. This is a "
            "sample-size problem, not a result. Collect more settled calls."
        )
    elif report.resolution < 1e-9:
        lines.append("")
        lines.append(
            "NOTE: resolution is ~0. This predictor is not distinguishing between "
            "outcomes at all, even if its calibration looks good."
        )
    return "\n".join(lines)
