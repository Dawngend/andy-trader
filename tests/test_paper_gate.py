"""Tests for the skill gate on paper trading."""

from datetime import UTC, datetime, timedelta
import sqlite3

from andy_trader.paper_gate import (
    DEFAULT_RECENT_WINDOW,
    MINIMUM_SETTLED_CALLS,
    evaluate_paper_eligibility,
    independent_calls,
)
from andy_trader.economics import evaluate_horizon
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
            outcome_up = CASE WHEN (id %  2) = 1 THEN 0 ELSE 1 END,
            settle_note = 'test price as of ' || resolves_at
        WHERE predictor = ? AND settled_at IS NULL
        """,
        (datetime(2026, 9, 1, tzinfo=UTC).isoformat(), predictor),
    )
    connection.commit()


def _price_history(
    connection: sqlite3.Connection, *, instrument: str = "BTC-USD", bars: int = 40
) -> None:
    """A real price series so the economics check has a move to measure. A
    predictor claiming skill with no price history behind it cannot have its
    break-even verified, which the gate correctly refuses to treat as proof."""

    base = datetime(2026, 8, 1, tzinfo=UTC)
    price = 100.0
    for index in range(bars):
        price *= 1.02 if index % 2 == 0 else (1.0 / 1.02)
        record_observations(
            connection,
            [
                Candle(
                    instrument=instrument, venue="binance", interval="1h",
                    open_time=(base + timedelta(hours=index)).isoformat(),
                    open=price, high=price, low=price, close=price, volume=1.0,
                )
            ],
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


def test_legacy_settlements_cannot_open_the_paper_gate() -> None:
    connection = _conn()
    total = MINIMUM_SETTLED_CALLS + 40
    _settled_calls(
        connection,
        predictor="baseline:legacy_winner",
        count=total,
        probability=0.9,
        correct=True,
    )
    connection.execute(
        "UPDATE crypto_predictions SET settle_note = NULL "
        "WHERE predictor = 'baseline:legacy_winner'"
    )
    connection.commit()

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:legacy_winner", instrument="BTC-USD"
    )

    assert verdict.eligible is False
    assert verdict.sample_size == 0
    assert verdict.corrected_calls == 0
    assert verdict.logged_calls == total
    assert f"0 corrected settlements of {total} logged" in verdict.reason


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


def test_beating_the_base_rate_statistically_is_not_enough_if_it_cannot_cover_costs() -> None:
    """The second real gap found on 2026-09-06: baseline:momentum reached 200
    settled 1h calls on LINK-USD at +0.0006 Brier skill and a 57.8% hit rate --
    genuinely better than the base rate -- while needing an 82.6% hit rate just
    to break even against that horizon's own ~46bps average move. Statistically
    better than guessing and economically profitable are different claims, and
    a gate that only checked the first would have let this pair start trading
    real (simulated) capital it could not actually keep."""
    connection = _conn()
    instrument = "LINK-USD"
    predictor = "baseline:borderline"

    # ~100bps average move -> break-even of ~65% at the default 30bps round trip.
    base = datetime(2026, 8, 1, tzinfo=UTC)
    price = 100.0
    for index in range(40):
        price *= 1.01 if index % 2 == 0 else (1.0 / 1.01)
        record_observations(
            connection,
            [
                Candle(
                    instrument=instrument, venue="binance", interval="1h",
                    open_time=(base + timedelta(hours=index)).isoformat(),
                    open=price, high=price, low=price, close=price, volume=1.0,
                )
            ],
        )

    # A genuine but weak edge: right 55% of the time, confidence matched to
    # that rate rather than overstated, so Brier skill comes out positive
    # instead of being punished for confident wrongness.
    total = MINIMUM_SETTLED_CALLS + 40
    for index in range(total):
        outcome_up = index % 2
        correct = (index % 20) < 11  # exactly 55% of calls
        leaning_up = bool(outcome_up) if correct else not bool(outcome_up)
        probability_up = 0.55 if leaning_up else 0.45
        created = base + timedelta(hours=index)
        record_prediction(
            connection,
            Prediction(
                predictor=predictor, instrument=instrument, horizon="1h",
                probability_up=probability_up, reference_price=100.0,
                created_at=created.isoformat(),
                resolves_at=(created + timedelta(hours=1)).isoformat(),
            ),
        )
    connection.execute(
        """
        UPDATE crypto_predictions
        SET settled_at = ?, settle_price = 100.0,
            outcome_up = CASE WHEN (id % 2) = 1 THEN 0 ELSE 1 END,
            settle_note = 'test price as of ' || resolves_at
        WHERE predictor = ? AND settled_at IS NULL
        """,
        (datetime(2026, 9, 1, tzinfo=UTC).isoformat(), predictor),
    )
    connection.commit()

    verdict = evaluate_paper_eligibility(connection, predictor=predictor, instrument=instrument)

    assert verdict.brier_skill_score is not None and verdict.brier_skill_score > 0
    assert verdict.hit_rate is not None and 0.5 < verdict.hit_rate < 0.6
    assert verdict.break_even_win_rate is not None
    assert verdict.break_even_win_rate > verdict.hit_rate
    assert not verdict.eligible
    assert "economically profitable" in verdict.reason


def _calls_with_explicit_outcomes(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    confident_correct: int,
    confident_wrong: int,
    probability: float = 0.75,
    instrument: str = "BTC-USD",
) -> None:
    """Write `confident_correct` calls that were right, followed in time by
    `confident_wrong` calls that were confidently wrong -- a predictor whose
    good history has since decayed. Outcomes are set explicitly per row by id,
    not by an id-parity trick, so a good block followed by a bad block cannot
    silently scramble which calls landed which way. Outcomes alternate so
    neither the lifetime nor the recent slice is ever degenerate."""

    base = datetime(2026, 8, 1, tzinfo=UTC)
    total = confident_correct + confident_wrong
    row_outcomes: list[tuple[int, int]] = []
    for index in range(total):
        outcome_up = index % 2
        decayed = index >= confident_correct
        leaning_up = (not bool(outcome_up)) if decayed else bool(outcome_up)
        probability_up = probability if leaning_up else 1.0 - probability
        created = base + timedelta(hours=index)
        row_id = record_prediction(
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
        row_outcomes.append((row_id, outcome_up))

    settled_at = datetime(2026, 9, 1, tzinfo=UTC).isoformat()
    for row_id, outcome_up in row_outcomes:
        connection.execute(
            "UPDATE crypto_predictions SET settled_at = ?, settle_price = 100.0, "
            "outcome_up = ?, settle_note = 'test price as of ' || resolves_at WHERE id = ?",
            (settled_at, outcome_up, row_id),
        )
    connection.commit()


def test_a_lifetime_average_earned_long_ago_does_not_excuse_recent_decay() -> None:
    """The exact failure DaviddTech described: his best strategy's equity
    curve kept looking fine on the lifetime average while a real losing streak
    was already underway. A predictor with a great early record and a recently
    inverted one must be blocked, not waved through on its history."""
    connection = _conn()
    good_block = MINIMUM_SETTLED_CALLS  # 200 confidently correct calls
    bad_block = DEFAULT_RECENT_WINDOW  # then 100 confidently wrong calls
    _calls_with_explicit_outcomes(
        connection,
        predictor="baseline:decayed",
        confident_correct=good_block,
        confident_wrong=bad_block,
    )
    _price_history(connection, instrument="BTC-USD", bars=40)

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:decayed", instrument="BTC-USD"
    )

    # The lifetime average is still real -- 200 right, 100 wrong nets positive.
    assert verdict.brier_skill_score is not None and verdict.brier_skill_score > 0
    # But the most recent DEFAULT_RECENT_WINDOW calls are the wrong ones.
    assert verdict.recent_sample_size == DEFAULT_RECENT_WINDOW
    assert verdict.recent_brier_skill_score is not None and verdict.recent_brier_skill_score < 0
    assert not verdict.eligible
    assert "stale" in verdict.reason


def test_a_predictor_that_is_still_good_recently_is_not_penalized(
) -> None:
    """The check must not become paranoid: a predictor whose recent window is
    exactly as good as its lifetime record should pass both checks and say so."""
    connection = _conn()
    _calls_with_explicit_outcomes(
        connection,
        predictor="baseline:consistent",
        confident_correct=MINIMUM_SETTLED_CALLS + DEFAULT_RECENT_WINDOW,
        confident_wrong=0,
    )
    # Give it real price history so the economics check can clear too.
    _price_history(connection, instrument="BTC-USD", bars=40)

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:consistent", instrument="BTC-USD"
    )

    assert verdict.eligible
    assert verdict.recent_sample_size == DEFAULT_RECENT_WINDOW
    assert verdict.recent_brier_skill_score is not None and verdict.recent_brier_skill_score > 0
    assert "independently confirm" in verdict.reason


def test_recent_window_check_is_skipped_when_there_is_not_enough_recent_history(
) -> None:
    """A caller may configure a recent_window larger than minimum_calls; in
    that case there is no meaningful trailing slice yet and the check must not
    fire on data that does not exist."""
    connection = _conn()
    _calls_with_explicit_outcomes(
        connection,
        predictor="baseline:thin",
        confident_correct=MINIMUM_SETTLED_CALLS,
        confident_wrong=0,
    )
    _price_history(connection, instrument="BTC-USD", bars=40)

    verdict = evaluate_paper_eligibility(
        connection,
        predictor="baseline:thin",
        instrument="BTC-USD",
        recent_window=MINIMUM_SETTLED_CALLS + 1,
    )

    assert verdict.eligible
    assert verdict.recent_sample_size is None


def test_a_predictor_that_beats_the_base_rate_is_allowed_through() -> None:
    connection = _conn()
    _price_history(connection)
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
    assert verdict.hit_rate is not None and verdict.break_even_win_rate is not None
    assert verdict.hit_rate >= verdict.break_even_win_rate


def test_the_gate_is_judged_per_instrument_not_per_predictor() -> None:
    """Working on BTC is not evidence about DOGE. A predictor carried by one
    instrument must not trade the other seven on that reputation."""
    connection = _conn()
    _price_history(connection, instrument="BTC-USD")
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


def test_the_gate_does_not_create_a_book_at_the_wrong_stake(monkeypatch) -> None:
    """A real bug, found on the live dashboard 2026-09-06.

    The gate calls get_or_create_state purely to ask "is a position open". That
    call BIRTHS the portfolio row, and it was not passing a stake, so the row
    was created at the hardcoded $10,000 default even though the configured
    stake was $15.97. Adding one predictor to the candidate list silently
    created eight books at $10,000 each and the dashboard jumped to $80,127.76.

    The gate is a read-only question. It must never be the thing that decides
    how much capital a book starts with.
    """
    monkeypatch.setenv("CRYPTO_PAPER_STARTING_CASH", "15.97")
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
            predictor="baseline:ema_crossover_12_26", instrument="BTC-USD",
            horizon="1h", probability_up=0.80, reference_price=100.0,
            created_at=now.isoformat(),
            resolves_at=(now + timedelta(hours=1)).isoformat(),
        ),
    )

    attempt = paper_trade_once(
        connection,
        predictor="baseline:ema_crossover_12_26",
        instrument="BTC-USD",
        now=now,
    )

    assert "skill gate" in (attempt.skipped_reason or "")
    row = connection.execute(
        "SELECT starting_cash, cash FROM paper_portfolio_state WHERE instrument = 'BTC-USD'"
    ).fetchone()
    assert row["starting_cash"] == 15.97, "gate created the book at the wrong stake"
    assert row["cash"] == 15.97


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


def _spaced_calls(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    count: int,
    spacing: timedelta,
    instrument: str = "BTC-USD",
) -> None:
    """Confident, correct 1h calls made every `spacing`, settled on alternating outcomes."""

    base = datetime(2026, 8, 1, tzinfo=UTC)
    for index in range(count):
        created = base + index * spacing
        record_prediction(
            connection,
            Prediction(
                predictor=predictor, instrument=instrument, horizon="1h",
                probability_up=0.9 if index % 2 == 0 else 0.1, reference_price=100.0,
                created_at=created.isoformat(),
                resolves_at=(created + timedelta(hours=1)).isoformat(),
            ),
        )
    connection.execute(
        """
        UPDATE crypto_predictions
        SET settled_at = ?, settle_price = 100.0,
            outcome_up = CASE WHEN (id % 2) = 1 THEN 1 ELSE 0 END,
            settle_note = 'test price as of ' || resolves_at
        WHERE predictor = ? AND settled_at IS NULL
        """,
        (datetime(2026, 9, 1, tzinfo=UTC).isoformat(), predictor),
    )
    connection.commit()


def test_calls_inside_one_forecast_window_are_one_piece_of_evidence() -> None:
    """The fourth gap, found 2026-09-28: the scheduler logs a call every 15
    minutes for every horizon, so four 1h calls share one outcome window. Live,
    baseline:random on AVAX-USD at 1d showed a t-statistic of 15 from 1,039
    "settled calls" that were only 17 independent days. Overlapping calls must
    not be allowed to fill the 200-call minimum on their own."""
    connection = _conn()
    _price_history(connection)
    logged = 4 * (MINIMUM_SETTLED_CALLS - 1)
    _spaced_calls(connection, predictor="baseline:eager", count=logged, spacing=timedelta(minutes=15))

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:eager", instrument="BTC-USD"
    )

    assert not verdict.eligible
    assert verdict.logged_calls == logged
    assert verdict.sample_size == MINIMUM_SETTLED_CALLS - 1
    assert "independent" in verdict.reason and f"{logged} logged" in verdict.reason


def test_calls_a_full_window_apart_all_count() -> None:
    connection = _conn()
    _price_history(connection)
    _spaced_calls(
        connection, predictor="baseline:patient", count=MINIMUM_SETTLED_CALLS,
        spacing=timedelta(hours=1),
    )

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:patient", instrument="BTC-USD"
    )

    assert verdict.sample_size == verdict.logged_calls == MINIMUM_SETTLED_CALLS
    assert verdict.eligible, verdict.reason


def test_independent_calls_keeps_only_disjoint_windows() -> None:
    def call(start_minute: int, length_minutes: int = 60) -> dict[str, str]:
        start = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=start_minute)
        return {
            "created_at": start.isoformat(),
            "resolves_at": (start + timedelta(minutes=length_minutes)).isoformat(),
        }

    rows = [call(0), call(15), call(59), call(60), call(61), call(130)]
    kept = independent_calls(rows)  # type: ignore[arg-type]

    # A call made exactly when the previous window resolves is independent.
    assert [row["created_at"] for row in kept] == [
        rows[0]["created_at"], rows[3]["created_at"], rows[5]["created_at"]
    ]


def test_costs_larger_than_the_average_move_are_explained_not_quoted_as_a_percentage() -> None:
    """Live, the fast strategy was blocked with "90.5% hit rate is below the
    550.5% needed": the right decision, stated as an impossible number. When
    the round trip is at least the average move, no hit rate can pay for it."""
    connection = _conn()
    _price_history(connection)  # ~200bps average 1h move
    _spaced_calls(
        connection, predictor="baseline:sharp", count=MINIMUM_SETTLED_CALLS,
        spacing=timedelta(hours=1),
    )

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:sharp", instrument="BTC-USD", round_trip_bps=500.0
    )

    assert not verdict.eligible
    assert verdict.break_even_win_rate is not None and verdict.break_even_win_rate > 1.0
    assert "even calling every move right would not" in verdict.reason
    assert f"{verdict.break_even_win_rate:.1%}" not in verdict.reason


def test_a_perfect_record_at_exactly_break_even_has_earned_nothing() -> None:
    """Found in Codex's review of the independent-calls change: with the round
    trip equal to the average move, break-even is exactly 100%, and a perfect
    100% hit rate used to pass the `<` check while expecting zero profit."""
    connection = _conn()
    _price_history(connection)
    _spaced_calls(
        connection, predictor="baseline:flawless", count=MINIMUM_SETTLED_CALLS,
        spacing=timedelta(hours=1),
    )
    econ = evaluate_horizon(connection, instrument="BTC-USD", interval="1h")
    assert econ is not None

    verdict = evaluate_paper_eligibility(
        connection, predictor="baseline:flawless", instrument="BTC-USD",
        round_trip_bps=econ.average_move_bps,
    )

    assert verdict.hit_rate == 1.0
    assert verdict.break_even_win_rate == 1.0
    assert not verdict.eligible


def test_independent_calls_compares_instants_not_stored_text() -> None:
    """Rows arrive ordered by the stored string. A +08:00 stamp sorts after a
    UTC one it actually precedes, and a naive stamp (read as UTC) must not
    crash the comparison against aware ones."""
    # Exactly the order `ORDER BY created_at` would return these strings in.
    rows = [
        {"created_at": "2026-08-01T01:00:00+00:00", "resolves_at": "2026-08-01T02:00:00+00:00"},
        {"created_at": "2026-08-01T02:00:00", "resolves_at": "2026-08-01T03:00:00"},
        # 00:30 UTC written in Manila time: sorts last as text, is really first.
        {"created_at": "2026-08-01T08:30:00+08:00", "resolves_at": "2026-08-01T09:30:00+08:00"},
    ]
    assert [row["created_at"] for row in rows] == sorted(row["created_at"] for row in rows)

    kept = independent_calls(rows)  # type: ignore[arg-type]

    assert [row["created_at"] for row in kept] == [
        "2026-08-01T08:30:00+08:00", "2026-08-01T02:00:00"
    ]
