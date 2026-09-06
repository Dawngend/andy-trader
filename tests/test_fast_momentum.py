"""Tests for the intra-round continuation strategy and its position sizing."""

from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from andy_trader.fast_momentum import (
    ContinuationCurve,
    FastMomentumError,
    Round,
    build_rounds,
    fit_continuation_curve,
    kelly_fraction,
    load_minute_closes,
    log_growth_per_trade,
    predict_round_once,
    probability_up,
    settle_fast_predictions,
)
from andy_trader.store import (
    Candle,
    Prediction,
    initialize_database,
    record_observations,
    record_prediction,
    settle_due_predictions,
)


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def _minute_bar(stamp: datetime, close: float, *, interval: str = "1m") -> Candle:
    return Candle(
        instrument="BTC-USD",
        venue="binance",
        interval=interval,
        open_time=stamp.isoformat(),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
    )


def _round(observed: float, final: float, *, open_price: float = 100_000.0) -> Round:
    return Round(
        open_time=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
        open_price=open_price,
        decision_price=open_price + observed,
        settle_price=open_price + final,
    )


# --------------------------------------------------------------------------
# Round reconstruction
# --------------------------------------------------------------------------


def test_a_round_missing_a_price_the_bet_depends_on_is_dropped() -> None:
    """An incomplete round must be dropped, never interpolated. Inventing the
    decision-point price is precisely what makes a fast backtest optimistic."""
    base = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    full = {
        -1: 100.0,  # the round's opening reference
        0: 101.0,
        1: 102.0,
        2: 103.0,  # decision point
        3: 104.0,
        4: 105.0,  # settle
    }
    for dropped in (-1, 2, 4):
        partial = [
            (base + timedelta(minutes=offset), price)
            for offset, price in full.items()
            if offset != dropped
        ]
        assert build_rounds(partial) == [], f"round survived without minute {dropped}"

    rounds = build_rounds([(base + timedelta(minutes=o), p) for o, p in full.items()])
    assert len(rounds) == 1
    assert rounds[0].open_price == 100.0
    assert rounds[0].decision_price == 103.0
    assert rounds[0].settle_price == 105.0


def test_a_minute_the_bet_does_not_depend_on_may_be_missing() -> None:
    """Minutes 1 and 3 sit inside the round but no part of the bet reads them.
    Discarding an otherwise-measurable round over them would throw away real
    observations to no purpose, so the round is kept."""
    base = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    closes = [
        (base - timedelta(minutes=1), 100.0),
        (base, 101.0),
        (base + timedelta(minutes=2), 103.0),
        (base + timedelta(minutes=4), 105.0),
    ]

    rounds = build_rounds(closes)

    assert len(rounds) == 1
    assert rounds[0].decision_price == 103.0


def test_rounds_only_start_on_clock_aligned_minutes() -> None:
    base = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    closes = [(base - timedelta(minutes=1) + timedelta(minutes=i), 100.0 + i) for i in range(12)]
    rounds = build_rounds(closes)
    assert [r.open_time.minute for r in rounds] == [0, 5]


def test_an_exact_tie_is_neither_a_win_nor_a_loss() -> None:
    """Counting a flat settle as a win would inflate every continuation rate."""
    assert _round(observed=50.0, final=0.0).continued is None
    assert _round(observed=0.0, final=50.0).continued is None
    assert _round(observed=50.0, final=80.0).continued is True
    assert _round(observed=50.0, final=-20.0).continued is False


# --------------------------------------------------------------------------
# The curve
# --------------------------------------------------------------------------


def test_curve_measures_continuation_per_band() -> None:
    # 100_000 open, so 10 bps == $100 of movement.
    big = [_round(observed=200.0, final=300.0) for _ in range(40)]  # 20 bps, continued
    big += [_round(observed=200.0, final=-50.0) for _ in range(10)]  # 20 bps, reversed
    curve = fit_continuation_curve(big, edges_bps=(0.0, 10.0, 100.0))

    assert curve.counts == (0, 50)
    assert curve.probabilities[1] == pytest.approx(0.8)


def test_a_thin_band_reports_no_opinion_rather_than_a_perfect_record() -> None:
    """Five-for-five is not a 100% edge, and trading it as one is how a strategy
    with no evidence behind it gets sized like a certainty."""
    curve = fit_continuation_curve(
        [_round(observed=200.0, final=300.0) for _ in range(5)],
        edges_bps=(0.0, 10.0, 100.0),
    )

    assert curve.probabilities[1] == 1.0  # the raw measurement is honest
    assert curve.probability(20.0) == 0.5  # but it refuses to be used


def test_downward_moves_mirror_the_continuation_probability() -> None:
    """The curve reports P(continuation), which has no direction. A down-move
    that continues means the round closes DOWN, so reusing the number unmirrored
    would bet the wrong way every single time price fell."""
    curve = ContinuationCurve((0.0, 10.0, 100.0), (0.5, 0.92), (0, 500))

    assert probability_up(20.0, curve) == pytest.approx(0.92)
    assert probability_up(-20.0, curve) == pytest.approx(0.08)


def test_a_move_too_small_to_mean_anything_is_a_coin_flip() -> None:
    curve = ContinuationCurve((0.0, 10.0, 100.0), (0.5, 0.92), (0, 500))
    assert probability_up(1.0, curve) == 0.5


# --------------------------------------------------------------------------
# Sizing -- the part that actually decides whether this survives
# --------------------------------------------------------------------------


def test_no_edge_means_no_bet() -> None:
    # Buying at the true probability is a fair bet, and a fair bet is not an
    # opportunity once any cost exists.
    assert kelly_fraction(0.92, 0.92) == 0.0
    assert kelly_fraction(0.90, 0.95) == 0.0


def test_kelly_is_scaled_and_capped() -> None:
    full = kelly_fraction(0.9416, 0.9266, scale=1.0, cap=1.0)
    half = kelly_fraction(0.9416, 0.9266, scale=0.5, cap=1.0)

    assert full == pytest.approx(0.2042, abs=1e-3)
    assert half == pytest.approx(full / 2)
    assert kelly_fraction(0.9416, 0.9266, scale=1.0, cap=0.05) == 0.05


def test_the_original_bots_fifty_percent_sizing_loses_money_at_the_measured_edge() -> None:
    """This is the finding this whole module exists to encode.

    Against 400 real resolved Polymarket rounds the favourite won 94.16% of the
    time at an average price of $0.9266. Every individual bet is favourable --
    positive expected value. Staked at the source bot's ~50% of bankroll, the
    COMPOUND growth rate is still negative, because 50% is roughly 2.5x Kelly and
    past 2x Kelly growth turns negative no matter how good each bet is.

    That is how an account wins ~94% of its trades and still goes to zero.
    """
    measured_probability = 0.9416
    measured_price = 0.9266

    # The bet itself is genuinely favourable.
    net_odds = (1 - measured_price) / measured_price
    expected_value = measured_probability * net_odds - (1 - measured_probability)
    assert expected_value > 0

    # And staking half the account on it still bleeds out.
    bot_growth = log_growth_per_trade(measured_probability, measured_price, 0.50)
    assert bot_growth < 0

    # Half-Kelly on the same edge grows instead.
    sized = kelly_fraction(measured_probability, measured_price)
    assert 0.0 < sized < 0.15
    assert log_growth_per_trade(measured_probability, measured_price, sized) > 0


def test_growth_rejects_a_stake_that_could_wipe_the_account() -> None:
    with pytest.raises(FastMomentumError):
        log_growth_per_trade(0.94, 0.93, 1.0)


# --------------------------------------------------------------------------
# Settlement safety
# --------------------------------------------------------------------------


def test_the_hourly_settlement_pass_refuses_to_touch_a_two_minute_call() -> None:
    """The default pass tolerates a 90-minute gap against 1h bars. Letting it
    settle a 2-minute prediction would resolve it with a price from a different
    hour and record the resulting coin flip as a real, scored outcome."""
    connection = _conn()
    record_observations(
        connection,
        [_minute_bar(datetime(2026, 9, 6, 12, 0, tzinfo=UTC), 100.0, interval="1h")],
    )
    record_prediction(
        connection,
        Prediction(
            predictor="fast:continuation",
            instrument="BTC-USD",
            horizon="2m",
            probability_up=0.92,
            reference_price=99.0,
            created_at="2026-09-06T12:03:00+00:00",
            resolves_at="2026-09-06T12:05:00+00:00",
        ),
    )

    result = settle_due_predictions(connection, now_iso="2026-09-06T13:00:00+00:00")

    assert result["settled"] == 0
    assert result["due"] == 0
    row = connection.execute("SELECT settled_at FROM crypto_predictions").fetchone()
    assert row["settled_at"] is None


def test_the_fast_pass_settles_that_same_call_against_minute_bars() -> None:
    connection = _conn()
    record_observations(
        connection,
        [_minute_bar(datetime(2026, 9, 6, 12, 5, tzinfo=UTC), 101.0)],
    )
    record_prediction(
        connection,
        Prediction(
            predictor="fast:continuation",
            instrument="BTC-USD",
            horizon="2m",
            probability_up=0.92,
            reference_price=100.0,
            created_at="2026-09-06T12:03:00+00:00",
            resolves_at="2026-09-06T12:05:00+00:00",
        ),
    )

    result = settle_fast_predictions(connection, now_iso="2026-09-06T12:06:00+00:00")

    assert result["settled"] == 1
    row = connection.execute(
        "SELECT outcome_up, settle_price FROM crypto_predictions"
    ).fetchone()
    assert row["outcome_up"] == 1
    assert row["settle_price"] == 101.0


# --------------------------------------------------------------------------
# Live prediction path
# --------------------------------------------------------------------------


def _seed_round(connection: sqlite3.Connection, prices: dict[int, float]) -> None:
    base = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    record_observations(
        connection,
        [_minute_bar(base + timedelta(minutes=offset), price) for offset, price in prices.items()],
    )


def test_no_call_is_made_outside_the_decision_minute() -> None:
    connection = _conn()
    curve = ContinuationCurve((0.0, 10.0, 100.0), (0.5, 0.92), (0, 500))
    _seed_round(connection, {-1: 100_000.0, 0: 100_050.0, 1: 100_150.0, 2: 100_200.0})

    # 12:02 -- only bar 12:01 is complete, one minute early.
    assert predict_round_once(
        connection,
        instrument="BTC-USD",
        curve=curve,
        now=datetime(2026, 9, 6, 12, 2, 30, tzinfo=UTC),
    ) is None


def test_a_call_uses_the_round_open_as_its_reference_price() -> None:
    """The bet is 'does this round close above where it opened', so the
    reference price must be the round's open, not the price at entry. Using the
    entry price would score a completely different question."""
    connection = _conn()
    curve = ContinuationCurve((0.0, 10.0, 100.0), (0.5, 0.92), (0, 500))
    _seed_round(connection, {-1: 100_000.0, 0: 100_050.0, 1: 100_150.0, 2: 100_200.0})

    prediction = predict_round_once(
        connection,
        instrument="BTC-USD",
        curve=curve,
        now=datetime(2026, 9, 6, 12, 3, 10, tzinfo=UTC),
    )

    assert prediction is not None
    assert prediction.reference_price == 100_000.0
    assert prediction.probability_up == pytest.approx(0.92)
    assert prediction.resolves_at == "2026-09-06T12:05:00+00:00"
    assert prediction.features["observed_move_bps"] == pytest.approx(20.0)


def test_a_downward_round_produces_a_call_below_one_half() -> None:
    connection = _conn()
    curve = ContinuationCurve((0.0, 10.0, 100.0), (0.5, 0.92), (0, 500))
    _seed_round(connection, {-1: 100_000.0, 0: 99_950.0, 1: 99_850.0, 2: 99_800.0})

    prediction = predict_round_once(
        connection,
        instrument="BTC-USD",
        curve=curve,
        now=datetime(2026, 9, 6, 12, 3, 10, tzinfo=UTC),
    )

    assert prediction is not None
    assert prediction.probability_up == pytest.approx(0.08)


def test_a_quiet_round_produces_no_call_at_all() -> None:
    """Selectivity is the strategy. Most rounds must produce nothing."""
    connection = _conn()
    curve = ContinuationCurve((0.0, 10.0, 100.0), (0.5, 0.92), (0, 500))
    _seed_round(connection, {-1: 100_000.0, 0: 100_002.0, 1: 100_001.0, 2: 100_003.0})

    assert predict_round_once(
        connection,
        instrument="BTC-USD",
        curve=curve,
        now=datetime(2026, 9, 6, 12, 3, 10, tzinfo=UTC),
    ) is None


def test_minute_closes_prefer_the_most_confirmed_observation() -> None:
    connection = _conn()
    stamp = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    record_observations(connection, [_minute_bar(stamp, 100.0)])
    record_observations(connection, [_minute_bar(stamp, 100.0)])  # seen twice
    record_observations(
        connection,
        [
            Candle(
                instrument="BTC-USD",
                venue="bybit",
                interval="1m",
                open_time=stamp.isoformat(),
                open=999.0,
                high=999.0,
                low=999.0,
                close=999.0,
                volume=1.0,
            )
        ],
    )

    closes = load_minute_closes(connection, "BTC-USD")

    assert closes == [(stamp, 100.0)]
