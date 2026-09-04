import pytest

from andy_trader.calibration import CalibrationError, brier_score, evaluate, format_report


def test_brier_score_of_a_perfect_predictor_is_zero() -> None:
    assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0


def test_brier_score_of_constant_half_is_a_quarter() -> None:
    assert brier_score([0.5] * 4, [1, 0, 1, 0]) == 0.25


def test_perfect_predictor_beats_the_base_rate() -> None:
    report = evaluate([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0])
    assert report.brier == 0.0
    assert report.brier_skill_score == 1.0
    assert report.beats_base_rate


def test_constant_half_does_not_beat_a_skewed_base_rate() -> None:
    """When 75% of moves are up, always saying 0.5 is worse than saying 0.75."""

    outcomes = [1, 1, 1, 0]
    report = evaluate([0.5] * 4, outcomes)
    assert report.base_rate == 0.75
    assert not report.beats_base_rate
    assert report.brier_skill_score < 0.0


def test_predicting_the_base_rate_scores_exactly_zero_skill() -> None:
    outcomes = [1, 1, 1, 0]
    report = evaluate([0.75] * 4, outcomes)
    assert report.brier == pytest.approx(report.brier_reference)
    assert report.brier_skill_score == pytest.approx(0.0)


def test_murphy_decomposition_identity_holds() -> None:
    """Brier = reliability - resolution + uncertainty.

    Exact only when every probability inside a bin is identical, so this uses
    three discrete values that each land alone in their own bin. If this test
    fails the scoring maths is wrong, not the test.
    """

    probabilities = [0.15, 0.15, 0.15, 0.45, 0.45, 0.85, 0.85, 0.85]
    outcomes = [0, 0, 1, 0, 1, 1, 1, 0]
    report = evaluate(probabilities, outcomes, bins=10)
    identity = report.reliability - report.resolution + report.uncertainty
    assert report.brier == pytest.approx(identity, abs=1e-12)


def test_uncorrelated_predictor_has_near_zero_resolution() -> None:
    """A predictor that varies but tracks nothing should show no resolution."""

    probabilities = [0.35, 0.65, 0.35, 0.65]
    outcomes = [1, 0, 0, 1]
    report = evaluate(probabilities, outcomes, bins=10)
    assert report.resolution == pytest.approx(0.0, abs=1e-12)


def test_wellcalibrated_bins_have_small_gaps() -> None:
    probabilities = [0.25] * 4 + [0.75] * 4
    outcomes = [1, 0, 0, 0, 1, 1, 1, 0]
    report = evaluate(probabilities, outcomes, bins=4)
    for bucket in report.bins:
        assert abs(bucket.gap) < 1e-9


def test_hit_rate_treats_an_exact_half_as_half_a_hit() -> None:
    report = evaluate([0.5, 0.5], [1, 0])
    assert report.hit_rate == pytest.approx(0.5)


def test_hit_rate_counts_direction_not_confidence() -> None:
    report = evaluate([0.51, 0.99, 0.49], [1, 1, 0])
    assert report.hit_rate == pytest.approx(1.0)


def test_empty_input_is_rejected() -> None:
    with pytest.raises(CalibrationError):
        evaluate([], [])


def test_length_mismatch_is_rejected() -> None:
    with pytest.raises(CalibrationError):
        evaluate([0.5], [1, 0])


def test_probability_outside_zero_one_is_rejected() -> None:
    with pytest.raises(CalibrationError):
        evaluate([1.2], [1])


def test_non_binary_outcome_is_rejected() -> None:
    with pytest.raises(CalibrationError):
        evaluate([0.5], [2])


def test_one_sided_sample_is_flagged_degenerate_not_scored_as_a_win() -> None:
    """All outcomes up makes the base rate perfect, so nobody can beat it."""

    perfect = evaluate([1.0, 1.0, 1.0], [1, 1, 1])
    assert perfect.degenerate
    assert perfect.brier == 0.0
    assert not perfect.beats_base_rate

    wrong = evaluate([0.5, 0.5, 0.5], [1, 1, 1])
    assert wrong.degenerate
    assert wrong.brier_skill_score == float("-inf")
    assert not wrong.beats_base_rate


def test_degenerate_report_warns_about_sample_size() -> None:
    text = format_report(evaluate([1.0, 1.0], [1, 1]))
    assert "UNDECIDABLE" in text
    assert "sample-size problem" in text


def test_report_names_the_verdict_and_flags_zero_resolution() -> None:
    text = format_report(evaluate([0.5] * 4, [1, 0, 1, 0]), predictor="coin_flip")
    assert "coin_flip" in text
    assert "base rate" in text
    assert "resolution is ~0" in text
