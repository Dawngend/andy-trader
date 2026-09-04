import pytest

from andy_trader.survival import (
    ResolvedTrade,
    SurvivalError,
    format_report,
    run_survival,
)


def _trade(predictor: str, *, up: bool, correct: bool, move: float = 0.10) -> ResolvedTrade:
    """One resolved call. `correct` decides whether the direction matched the outcome."""

    outcome_up = 1 if (up if correct else not up) else 0
    reference = 100.0
    settle = reference * (1.0 + move) if outcome_up else reference * (1.0 - move)
    return ResolvedTrade(
        predictor=predictor,
        probability_up=0.9 if up else 0.1,
        outcome_up=outcome_up,
        reference_price=reference,
        settle_price=settle,
    )


def test_a_consistent_loser_is_retired() -> None:
    trades = [_trade("loser", up=True, correct=False) for _ in range(200)]
    report = run_survival(trades, grace_trades=5, stake_fraction=0.5)

    assert [b.predictor for b in report.retired] == ["loser"]
    assert report.survivors == []
    assert report.retired[0].retired_after_trade is not None


def test_a_consistent_winner_survives() -> None:
    trades = [_trade("winner", up=True, correct=True) for _ in range(50)]
    report = run_survival(trades, grace_trades=5)

    assert [b.predictor for b in report.survivors] == ["winner"]
    assert report.retired == []
    assert report.survivors[0].equity > 1.0
    assert report.survivors[0].win_rate == 1.0


def test_a_retired_predictor_takes_no_further_trades() -> None:
    trades = [_trade("loser", up=True, correct=False) for _ in range(200)]
    report = run_survival(trades, grace_trades=5, stake_fraction=0.5)

    book = report.retired[0]
    # Retirement stops the book, so trades taken must be fewer than trades offered.
    assert book.trades < len(trades)
    assert book.trades == book.retired_after_trade


def test_the_grace_period_prevents_an_early_death() -> None:
    """Ruin inside the grace period is variance, not evidence, so it must not retire."""

    trades = [_trade("unlucky", up=True, correct=False) for _ in range(20)]
    report = run_survival(trades, grace_trades=1000, stake_fraction=0.9)

    assert report.retired == []
    assert report.survivors[0].equity < 0.20  # Ruined by equity, spared by the grace rule.


def test_the_dead_are_always_reported() -> None:
    """Survivorship bias is the failure mode; the graveyard is not suppressible."""

    trades = (
        [_trade("winner", up=True, correct=True) for _ in range(60)]
        + [_trade("loser", up=True, correct=False) for _ in range(200)]
    )
    report = run_survival(trades, grace_trades=5, stake_fraction=0.5)

    assert report.population == 2
    assert len(report.survivors) == 1 and len(report.retired) == 1
    assert report.survival_rate == pytest.approx(0.5)
    text = format_report(report)
    assert "RETIRED" in text
    assert "loser" in text
    assert "trading screenshot" in text


def test_an_abstaining_predictor_takes_no_trades_and_cannot_die() -> None:
    trades = [
        ResolvedTrade("abstainer", probability_up=0.5, outcome_up=0,
                      reference_price=100.0, settle_price=50.0)
        for _ in range(100)
    ]
    report = run_survival(trades, grace_trades=1)

    assert report.trades_evaluated == 0
    assert report.survivors[0].trades == 0
    assert report.survivors[0].equity == 1.0


def test_costs_make_a_coin_flip_bleed() -> None:
    """Alternating right and wrong is break-even gross and a loss net of costs."""

    trades = []
    for index in range(100):
        trades.append(_trade("flipper", up=True, correct=index % 2 == 0, move=0.01))
    report = run_survival(trades, grace_trades=1000, fee_bps=10.0, slippage_bps=5.0)

    assert report.survivors[0].win_rate == pytest.approx(0.5)
    assert report.survivors[0].equity < 1.0


def test_zero_cost_leaves_a_coin_flip_roughly_flat() -> None:
    trades = []
    for index in range(100):
        trades.append(_trade("flipper", up=True, correct=index % 2 == 0, move=0.01))
    report = run_survival(
        trades, grace_trades=1000, fee_bps=0.0, slippage_bps=0.0, stake_fraction=0.05
    )

    assert report.survivors[0].equity == pytest.approx(1.0, abs=0.01)


def test_warning_when_too_few_trades_have_resolved() -> None:
    trades = [_trade("someone", up=True, correct=True) for _ in range(3)]
    text = format_report(run_survival(trades, grace_trades=30))

    assert "grace period" in text
    assert "not a verdict" in text


def test_survivors_are_ranked_by_equity() -> None:
    trades = (
        [_trade("good", up=True, correct=True) for _ in range(40)]
        + [_trade("better", up=True, correct=True, move=0.20) for _ in range(40)]
    )
    report = run_survival(trades, grace_trades=5)

    assert [b.predictor for b in report.survivors] == ["better", "good"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ruin_fraction": 0.0},
        {"ruin_fraction": 1.0},
        {"stake_fraction": 0.0},
        {"stake_fraction": 1.5},
        {"starting_bankroll": 0.0},
        {"grace_trades": -1},
        {"fee_bps": -1.0},
    ],
)
def test_configurations_that_would_make_the_verdict_meaningless_are_refused(kwargs) -> None:
    with pytest.raises(SurvivalError):
        run_survival([_trade("x", up=True, correct=True)], **kwargs)


def test_a_non_positive_reference_price_is_refused() -> None:
    bad = ResolvedTrade("x", probability_up=0.9, outcome_up=1,
                        reference_price=0.0, settle_price=10.0)
    with pytest.raises(SurvivalError, match="non-positive"):
        run_survival([bad])
