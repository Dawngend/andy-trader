"""Tests for the skill gate on paper trading."""

from datetime import UTC, datetime, timedelta
import sqlite3

from andy_trader.paper_gate import MINIMUM_SETTLED_CALLS, evaluate_paper_eligibility
from andy_trader.portfolio import paper_trade_once
from andy_trader.store import (
    Candle,
    Prediction,
    initialize_database,
    record_observations,
    record_prediction,
)


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def _settled_calls(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    count: int,
    probability: float,
    correct: bool,
    instrument: str = "BTC-USD",
) -> None:
    """Write `count` already-settled calls, alternating outcomes so the sample
    is never degenerate, with the call either right or wrong as asked."""

    base = datetime(2026, 8, 1, tzinfo=UTC)
    for index in range(count):
        outcome_up = index % 2
        # A "correct" predictor leans toward the outcome that actually happened.
        leaning_up = bool(outcome_up) if correct else not bool(outcome_up)
        probability_up = probability if leaning_up else 1.0 - probability
        created = base + timedelta(hours=index)
        record_prediction(
            connection,
            Prediction(
                predictor=predictor,
                instrument=instrument,
                horizon="1h",
                probability_up=probability_up,
                reference_price=100.0,
                created_at=created.isoformat(),
                resolves_at=(created + timedelta(hours=1)).isoformat(),
            ),
        )
    connection.execute(
        """
        UPDATE crypto_predictions
        SET settled_at = ?, settle_price = 100.0,
            outcome_up = CASE WHEN (id %  2) = 1 THEN 0 ELSE 1 END
        WHERE predictor = ? AND settled_at IS NULL
        """,
        (datetime(2026, 9, 1, tzinfo=UTC).isoformat(), predictor),
    )
    connection.commit()


def test_a_predictor_with_too_little_history_has_not_proven_anything() -> None:
    connection = _conn()
    _settled_calls(connection, predictor="baseline:new", count=20, probability=0.9, correct=True)

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:new", instrument="BTC-USD"
    )

    assert not verdict.eligible
    assert "needs 200" in verdict.reason
    assert verdict.sample_size == 20


def test_a_predictor_that_loses_to_the_base_rate_may_not_deploy_capital() -> None:
    """The live failure this gate was written for: baseline:momentum scored
    -0.0390 Brier skill with a 47.1% hit rate over 2,818 calls and was still
    paper-trading eight instruments, because paper trading was gated by a config
    string rather than by evidence."""
    connection = _conn()
    _settled_calls(
        connection,
        predictor="baseline:momentum",
        count=MINIMUM_SETTLED_CALLS + 40,
        probability=0.75,
        correct=False,  # confidently wrong, exactly like the real thing
    )

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:momentum", instrument="BTC-USD"
    )

    assert not verdict.eligible
    assert verdict.brier_skill_score is not None and verdict.brier_skill_score < 0
    assert "has not earned the right to deploy capital" in verdict.reason


def test_a_predictor_that_beats_the_base_rate_is_allowed_through() -> None:
    connection = _conn()
    _settled_calls(
        connection,
        predictor="baseline:good",
        count=MINIMUM_SETTLED_CALLS + 40,
        probability=0.75,
        correct=True,
    )

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:good", instrument="BTC-USD"
    )

    assert verdict.eligible
    assert verdict.brier_skill_score is not None and verdict.brier_skill_score > 0


def test_the_gate_is_judged_per_instrument_not_per_predictor() -> None:
    """Working on BTC is not evidence about DOGE. A predictor carried by one
    instrument must not trade the other seven on that reputation."""
    connection = _conn()
    _settled_calls(
        connection,
        predictor="baseline:mixed",
        count=MINIMUM_SETTLED_CALLS + 40,
        probability=0.75,
        correct=True,
        instrument="BTC-USD",
    )

    assert evaluate_paper_eligibility(
        connection, predictor="baseline:mixed", instrument="BTC-USD"
    ).eligible
    assert not evaluate_paper_eligibility(
        connection, predictor="baseline:mixed", instrument="DOGE-USD"
    ).eligible


def test_propose_refuses_to_rank_when_nothing_is_scoreable(tmp_path, capsys) -> None:
    """A leaderboard built from unscoreable candidates is not a leaderboard, and
    presenting one would invite funding the top of a list of noise."""
    from andy_trader.paper_gate import main
    from andy_trader.store import connect

    database = tmp_path / "gate.db"
    connection = connect(database)
    initialize_database(connection)
    _settled_calls(connection, predictor="baseline:a", count=10, probability=0.7, correct=True)
    connection.close()

    assert main(["--database", str(database), "--propose", "3"]) == 0

    output = capsys.readouterr().out
    assert "There is no ranking to make" in output
    assert "deploy nothing" in output


def test_propose_warns_about_the_selection_premium_before_recommending(
    tmp_path, capsys
) -> None:
    """Picking the best of N candidates inflates apparent skill even when every
    candidate's true edge is zero. That warning must appear next to any ranking,
    or the ranking reads as evidence when it is not."""
    from andy_trader.paper_gate import main
    from andy_trader.store import connect

    database = tmp_path / "gate.db"
    connection = connect(database)
    initialize_database(connection)
    for name in ("baseline:a", "baseline:b", "baseline:c", "baseline:d"):
        _settled_calls(
            connection,
            predictor=name,
            count=MINIMUM_SETTLED_CALLS + 20,
            probability=0.75,
            correct=False,
        )
    connection.close()

    assert main(["--database", str(database), "--propose", "3"]) == 0

    output = capsys.readouterr().out
    assert "Selection premium" in output
    assert "standard errors" in output
    assert "deploy nothing" in output  # none of them clear the gate on their own


def test_paper_trade_refuses_to_open_for_an_unproven_predictor() -> None:
    connection = _conn()
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    record_observations(
        connection,
        [
            Candle(
                instrument="BTC-USD", venue="binance", interval="1h",
                open_time=now.isoformat(), open=100.0, high=100.0, low=100.0,
                close=100.0, volume=1.0,
            )
        ],
    )
    record_prediction(
        connection,
        Prediction(
            predictor="baseline:momentum", instrument="BTC-USD", horizon="1h",
            probability_up=0.80, reference_price=100.0,
            created_at=now.isoformat(),
            resolves_at=(now + timedelta(hours=1)).isoformat(),
        ),
    )

    attempt = paper_trade_once(
        connection, predictor="baseline:momentum", instrument="BTC-USD", now=now
    )

    assert attempt.trade is None
    assert attempt.skipped_reason is not None
    assert "skill gate" in attempt.skipped_reason


def test_the_gate_can_be_bypassed_explicitly_for_backtests() -> None:
    connection = _conn()
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    record_observations(
        connection,
        [
            Candle(
                instrument="BTC-USD", venue="binance", interval="1h",
                open_time=now.isoformat(), open=100.0, high=100.0, low=100.0,
                close=100.0, volume=1.0,
            )
        ],
    )
    record_prediction(
        connection,
        Prediction(
            predictor="baseline:momentum", instrument="BTC-USD", horizon="1h",
            probability_up=0.80, reference_price=100.0,
            created_at=now.isoformat(),
            resolves_at=(now + timedelta(hours=1)).isoformat(),
        ),
    )

    attempt = paper_trade_once(
        connection,
        predictor="baseline:momentum",
        instrument="BTC-USD",
        now=now,
        skill_gate_disabled=True,
    )

    assert attempt.trade is not None


def test_an_open_position_can_still_be_closed_after_the_gate_shuts() -> None:
    """The gate blocks new exposure, never the removal of it. Trapping a
    position inside a safety check would be a failure mode invented by the
    safety check itself."""
    connection = _conn()
    opened = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    record_observations(
        connection,
        [
            Candle(
                instrument="BTC-USD", venue="binance", interval="1h",
                open_time=opened.isoformat(), open=100.0, high=100.0, low=100.0,
                close=100.0, volume=1.0,
            )
        ],
    )
    record_prediction(
        connection,
        Prediction(
            predictor="baseline:momentum", instrument="BTC-USD", horizon="1h",
            probability_up=0.80, reference_price=100.0,
            created_at=opened.isoformat(),
            resolves_at=(opened + timedelta(hours=1)).isoformat(),
        ),
    )
    # Open a position with the gate deliberately bypassed.
    assert paper_trade_once(
        connection, predictor="baseline:momentum", instrument="BTC-USD",
        now=opened, skill_gate_disabled=True,
    ).trade is not None

    # Now an exit signal arrives while the gate is shut.
    later = opened + timedelta(hours=1)
    record_observations(
        connection,
        [
            Candle(
                instrument="BTC-USD", venue="binance", interval="1h",
                open_time=later.isoformat(), open=101.0, high=101.0, low=101.0,
                close=101.0, volume=1.0,
            )
        ],
    )
    record_prediction(
        connection,
        Prediction(
            predictor="baseline:momentum", instrument="BTC-USD", horizon="1h",
            probability_up=0.20, reference_price=101.0,
            created_at=later.isoformat(),
            resolves_at=(later + timedelta(hours=1)).isoformat(),
        ),
    )

    attempt = paper_trade_once(
        connection, predictor="baseline:momentum", instrument="BTC-USD", now=later
    )

    assert attempt.skipped_reason is None, attempt.skipped_reason
    assert attempt.trade is not None
    assert attempt.trade.side == "flat"
