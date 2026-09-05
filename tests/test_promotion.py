"""Unit tests for the pure promotion gate function."""

import pytest

from andy_trader.calibration import evaluate
from andy_trader.promotion import evaluate_promotion_gate


def test_promotion_rejected_when_sample_size_below_minimum() -> None:
    # 20 samples < 24 minimum
    outcomes = [1, 0] * 10
    cand_probs = [0.6, 0.4] * 10
    base_probs = [0.5, 0.5] * 10

    cand_rep = evaluate(cand_probs, outcomes)
    base_rep = evaluate(base_probs, outcomes)

    decision = evaluate_promotion_gate(
        candidate_report=cand_rep,
        base_rate_report=base_rep,
        minimum_holdout_bars=24,
    )
    assert not decision.promoted
    assert "Holdout sample too small" in decision.reason


def test_promotion_rejected_when_sample_counts_mismatch() -> None:
    outcomes_24 = [1, 0] * 12
    outcomes_26 = [1, 0] * 13
    cand_probs = [0.6, 0.4] * 12
    base_probs = [0.5, 0.5] * 13

    cand_rep = evaluate(cand_probs, outcomes_24)
    base_rep = evaluate(base_probs, outcomes_26)

    decision = evaluate_promotion_gate(
        candidate_report=cand_rep,
        base_rate_report=base_rep,
        minimum_holdout_bars=20,
    )
    assert not decision.promoted
    assert "Mismatched holdout counts" in decision.reason


def test_promotion_rejected_when_sample_is_degenerate() -> None:
    # All 1s -> degenerate
    outcomes = [1] * 30
    cand_probs = [0.8] * 30
    base_probs = [0.6] * 30

    cand_rep = evaluate(cand_probs, outcomes)
    base_rep = evaluate(base_probs, outcomes)
    assert cand_rep.degenerate
    assert base_rep.degenerate

    decision = evaluate_promotion_gate(
        candidate_report=cand_rep,
        base_rate_report=base_rep,
        minimum_holdout_bars=24,
    )
    assert not decision.promoted
    assert "degenerate" in decision.reason


def test_promotion_rejected_when_candidate_has_negative_skill() -> None:
    outcomes = [1, 0] * 15
    # Predicts wrong direction confidently
    cand_probs = [0.2, 0.8] * 15
    base_probs = [0.5, 0.5] * 15

    cand_rep = evaluate(cand_probs, outcomes)
    base_rep = evaluate(base_probs, outcomes)

    assert cand_rep.brier_skill_score < 0.0
    decision = evaluate_promotion_gate(
        candidate_report=cand_rep,
        base_rate_report=base_rep,
        minimum_holdout_bars=24,
    )
    assert not decision.promoted
    assert "non-positive Brier skill score" in decision.reason


def test_promotion_rejected_when_candidate_ties_base_rate() -> None:
    outcomes = [1, 0] * 15
    probs = [0.55, 0.45] * 15

    cand_rep = evaluate(probs, outcomes)
    base_rep = evaluate(probs, outcomes)

    decision = evaluate_promotion_gate(
        candidate_report=cand_rep,
        base_rate_report=base_rep,
        minimum_holdout_bars=24,
    )
    assert not decision.promoted
    assert "does not beat baseline:base_rate" in decision.reason


def test_promotion_rejected_when_candidate_skill_trails_base_rate() -> None:
    outcomes = [1, 0] * 15
    # Base rate predicts closer to truth than candidate
    cand_probs = [0.52, 0.48] * 15
    base_probs = [0.65, 0.35] * 15

    cand_rep = evaluate(cand_probs, outcomes)
    base_rep = evaluate(base_probs, outcomes)

    decision = evaluate_promotion_gate(
        candidate_report=cand_rep,
        base_rate_report=base_rep,
        minimum_holdout_bars=24,
    )
    assert not decision.promoted
    assert "does not beat baseline:base_rate" in decision.reason


def test_promotion_approved_when_candidate_strictly_beats_base_rate() -> None:
    # 30 bars, well-calibrated candidate beating base rate
    outcomes = [1, 0, 1, 1, 0, 0] * 5
    cand_probs = [0.75, 0.25, 0.80, 0.70, 0.30, 0.20] * 5
    base_probs = [0.50, 0.50, 0.50, 0.50, 0.50, 0.50] * 5

    cand_rep = evaluate(cand_probs, outcomes)
    base_rep = evaluate(base_probs, outcomes)

    assert cand_rep.brier_skill_score > base_rep.brier_skill_score
    assert cand_rep.brier < base_rep.brier
    assert cand_rep.brier_skill_score > 0.0
    assert not cand_rep.degenerate

    decision = evaluate_promotion_gate(
        candidate_report=cand_rep,
        base_rate_report=base_rep,
        minimum_holdout_bars=24,
    )
    assert decision.promoted
    assert "cleared promotion gate" in decision.reason
    assert decision.candidate_skill == cand_rep.brier_skill_score
    assert decision.base_rate_skill == base_rep.brier_skill_score
