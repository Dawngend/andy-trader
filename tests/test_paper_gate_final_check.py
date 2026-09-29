"""Tests for the FINAL CHECK section printed under the paper gate report."""

from andy_trader.paper_gate import MINIMUM_SETTLED_CALLS, EligibilityVerdict, final_check


def _verdict(**overrides) -> EligibilityVerdict:
    fields = dict(
        eligible=False,
        reason="",
        predictor="baseline:momentum",
        instrument="BTC-USD",
        sample_size=MINIMUM_SETTLED_CALLS,
        brier_skill_score=-0.01,
        hit_rate=0.5,
        break_even_win_rate=0.6,
        recent_brier_skill_score=-0.02,
        logged_calls=800,
        corrected_calls=400,
    )
    fields.update(overrides)
    return EligibilityVerdict(**fields)


def test_nothing_eligible_says_no_edge_and_nothing_to_invalidate() -> None:
    text = "\n".join(final_check([_verdict(), _verdict(instrument="ETH-USD")]))
    assert "No edge is demonstrated: 0 of 2 pair(s) clear the gate." in text
    assert "nothing to invalidate yet" in text


def test_eligible_pair_gets_its_own_invalidation_condition() -> None:
    passing = _verdict(
        eligible=True, brier_skill_score=0.01, recent_brier_skill_score=0.02,
        break_even_win_rate=0.55,
    )
    text = "\n".join(final_check([passing, _verdict(instrument="ETH-USD")]))
    assert "1 of 2 pair(s) clear the gate: baseline:momentum BTC-USD." in text
    assert "hit rate below 55.0%" in text
    assert "confirm on a fresh window" in text


def test_recent_skill_flipping_sign_is_flagged_as_regime_change() -> None:
    decayed = _verdict(brier_skill_score=0.01, recent_brier_skill_score=-0.01)
    text = "\n".join(final_check([decayed]))
    assert "1 pair(s) have recent skill on the opposite side of zero" in text


def test_thin_samples_uncoverable_costs_and_overlap_are_counted() -> None:
    thin = _verdict(sample_size=13, logged_calls=100)
    hopeless = _verdict(instrument="DOGE-USD", break_even_win_rate=1.0, logged_calls=900)
    text = "\n".join(final_check([thin, hopeless]))
    assert f"1 of 2 pair(s) are below {MINIMUM_SETTLED_CALLS} independent calls" in text
    assert "1 pair(s) cannot break even at any hit rate" in text
    assert f"1,000 logged calls rest on {13 + MINIMUM_SETTLED_CALLS:,} independent outcomes" in text


def test_missing_scores_do_not_crash_or_count_as_regime_change() -> None:
    unscored = _verdict(brier_skill_score=None, recent_brier_skill_score=None,
                        break_even_win_rate=None, logged_calls=None)
    text = "\n".join(final_check([unscored]))
    assert "No pair's recent skill contradicts its lifetime skill." in text
    assert "0 pair(s) cannot break even" in text
