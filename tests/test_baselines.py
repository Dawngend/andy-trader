import pytest

from andy_trader.baselines import (
    MAX_CONFIDENCE,
    MIN_CONFIDENCE,
    Baseline,
    BaselineError,
    base_rate,
    coin_flip,
    default_baselines,
    make_ema_crossover,
    make_random,
    momentum,
)

RISING = [100.0 + i for i in range(40)]
FALLING = [140.0 - i for i in range(40)]


def test_coin_flip_is_exactly_one_half() -> None:
    assert coin_flip(RISING) == 0.5


def test_base_rate_counts_up_moves() -> None:
    # 3 up moves out of 4 transitions, clamped by MAX_CONFIDENCE.
    assert base_rate([1.0, 2.0, 3.0, 4.0, 3.5]) == pytest.approx(MAX_CONFIDENCE)


def test_base_rate_of_a_flat_series_is_the_floor() -> None:
    assert base_rate([5.0, 5.0, 5.0]) == pytest.approx(MIN_CONFIDENCE)


def test_base_rate_of_a_single_point_is_uninformative() -> None:
    assert base_rate([5.0]) == 0.5


def test_momentum_follows_the_last_move() -> None:
    assert momentum([100.0, 101.0]) > 0.5
    assert momentum([100.0, 99.0]) < 0.5


def test_momentum_is_flat_when_nothing_moved() -> None:
    assert momentum([100.0, 100.0]) == pytest.approx(0.5)


def test_momentum_saturates_rather_than_exceeding_the_ceiling() -> None:
    assert momentum([100.0, 200.0]) == pytest.approx(MAX_CONFIDENCE)
    assert momentum([200.0, 100.0]) == pytest.approx(MIN_CONFIDENCE)


def test_ema_crossover_is_bullish_on_a_rising_series() -> None:
    assert make_ema_crossover(12, 26)(RISING) > 0.5


def test_ema_crossover_is_bearish_on_a_falling_series() -> None:
    assert make_ema_crossover(12, 26)(FALLING) < 0.5


def test_ema_crossover_rejects_a_fast_period_at_or_above_slow() -> None:
    with pytest.raises(BaselineError):
        make_ema_crossover(26, 26)


def test_random_is_reproducible_for_a_given_seed() -> None:
    first = [make_random(7)(RISING) for _ in range(5)]
    second = [make_random(7)(RISING) for _ in range(5)]
    assert first == second


def test_baseline_enforces_minimum_history() -> None:
    baseline = Baseline("needs_more", momentum, minimum_history=5)
    with pytest.raises(BaselineError):
        baseline([100.0, 101.0])


def test_baseline_rejects_an_out_of_range_probability() -> None:
    baseline = Baseline("broken", lambda _closes: 1.5, minimum_history=1)
    with pytest.raises(BaselineError):
        baseline([100.0])


def test_default_baselines_all_produce_valid_probabilities() -> None:
    for baseline in default_baselines():
        value = baseline(RISING)
        assert 0.0 <= value <= 1.0


def test_default_baselines_have_unique_names() -> None:
    names = [baseline.name for baseline in default_baselines()]
    assert len(names) == len(set(names))
