"""Tests for the probe book: ungated, auto-run paper trading of llm:agent calls."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from andy_trader.portfolio import initialize_portfolio, mark_to_market, run_paper_cycle
from andy_trader.probe_book import (
    PROBE_BOOK,
    PROBE_X10_BOOK,
    PROBE_X10_LEVERAGE,
    SOURCE_PREDICTOR,
    STARTING_CASH_PHP,
    probe_trade_once,
    probe_x10_trade_once,
)
from andy_trader.risk import check_and_enforce, rearm_kill_switch
from andy_trader.store import Candle, Prediction, connect, record_observations, record_prediction


def _seed_cycle(
    connection,
    *,
    now: datetime,
    probability_up: float,
    price: float = 100.0,
) -> None:
    """Record a fresh price bar and a fresh llm:agent call, as one cycle would."""

    bar_time = (now - timedelta(minutes=1)).isoformat()
    call_time = (now - timedelta(seconds=30)).isoformat()
    record_observations(
        connection,
        [
            Candle(
                instrument="BTC-USD", venue="bybit", interval="1h", open_time=bar_time,
                open=price, high=price, low=price, close=price, volume=1.0,
            )
        ],
    )
    record_prediction(
        connection,
        Prediction(
            predictor=SOURCE_PREDICTOR, instrument="BTC-USD", horizon="1h",
            probability_up=probability_up, reference_price=price,
            created_at=call_time, resolves_at=(now + timedelta(minutes=59)).isoformat(),
        ),
    )


def test_probe_trades_with_no_settled_history_and_no_approval(tmp_path: Path) -> None:
    """The whole point: a predictor with zero settled calls would be blocked by
    the skill gate on any gated book, but the probe book trades it anyway."""
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    with connect(database) as connection:
        _seed_cycle(connection, now=now, probability_up=0.9)
        attempt = probe_trade_once(connection, instrument="BTC-USD", now=now)

    assert attempt.skipped_reason is None
    assert attempt.trade is not None
    assert attempt.trade.side == "long"


def test_probe_trade_reason_says_ungated(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    with connect(database) as connection:
        _seed_cycle(connection, now=now, probability_up=0.9)
        attempt = probe_trade_once(connection, instrument="BTC-USD", now=now)

    assert attempt.trade is not None
    assert "ungated" in attempt.trade.reason


def test_probe_books_its_own_name_and_stake_separate_from_the_gated_predictor(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    with connect(database) as connection:
        _seed_cycle(connection, now=now, probability_up=0.9)
        probe_trade_once(connection, instrument="BTC-USD", now=now)

        rows = connection.execute(
            "SELECT predictor, starting_cash FROM paper_portfolio_state"
        ).fetchall()

    predictors = {row["predictor"] for row in rows}
    assert predictors == {PROBE_BOOK}  # never written under "llm:agent"
    assert all(row["starting_cash"] == STARTING_CASH_PHP for row in rows)
    assert STARTING_CASH_PHP == 1_000.0


def test_probe_never_jumps_directly_from_long_to_short(tmp_path: Path) -> None:
    """A long-to-short prediction swing must close to flat first, then open
    short on the NEXT cycle -- never in one step. If run_paper_cycle's
    hysteresis regressed, execute_paper_trade would raise PortfolioError
    instead of this test observing a clean flat-then-short sequence."""
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)

    with connect(database) as connection:
        # Cycle 1: confident up -> opens long.
        _seed_cycle(connection, now=now, probability_up=0.9)
        first = probe_trade_once(connection, instrument="BTC-USD", now=now)
        assert first.trade is not None and first.trade.side == "long"

        # Cycle 2: confident down, while still long -> must close to flat only,
        # never jump straight to short.
        now_2 = now + timedelta(hours=1)
        _seed_cycle(connection, now=now_2, probability_up=0.1)
        second = probe_trade_once(connection, instrument="BTC-USD", now=now_2)
        assert second.trade is not None
        assert second.trade.side == "flat"

        # Cycle 3: still confident down, now flat -> opens short.
        now_3 = now_2 + timedelta(hours=1)
        _seed_cycle(connection, now=now_3, probability_up=0.1)
        third = probe_trade_once(connection, instrument="BTC-USD", now=now_3)
        assert third.trade is not None
        assert third.trade.side == "short"

        state = connection.execute(
            "SELECT position_qty FROM paper_portfolio_state WHERE predictor = ? AND instrument = 'BTC-USD'",
            (PROBE_BOOK,),
        ).fetchone()
        assert state["position_qty"] < 0  # ended up short, reached safely through flat


@pytest.mark.parametrize("book", (probe_trade_once, probe_x10_trade_once))
def test_probe_holds_signal_flip_until_minimum_age(tmp_path: Path, book) -> None:
    now = datetime(2026, 10, 20, 0, tzinfo=UTC)
    with connect(tmp_path / "paper.db") as connection:
        _seed_cycle(connection, now=now, probability_up=0.9)
        assert book(connection, instrument="BTC-USD", now=now).trade.side == "long"

        five = now + timedelta(minutes=5)
        _seed_cycle(connection, now=five, probability_up=0.1)
        assert book(connection, instrument="BTC-USD", now=five).trade is None

        sixteen = now + timedelta(minutes=16)
        _seed_cycle(connection, now=sixteen, probability_up=0.1)
        assert book(connection, instrument="BTC-USD", now=sixteen).trade.side == "flat"


@pytest.mark.parametrize("book", (probe_trade_once, probe_x10_trade_once))
def test_stop_exits_during_minimum_hold(tmp_path: Path, book) -> None:
    now = datetime(2026, 10, 20, 0, tzinfo=UTC)
    with connect(tmp_path / "paper.db") as connection:
        _seed_cycle(connection, now=now, probability_up=0.9)
        assert book(connection, instrument="BTC-USD", now=now).trade.side == "long"
        later = now + timedelta(minutes=2)
        _seed_cycle(connection, now=later, probability_up=0.9, price=97)
        attempt = book(connection, instrument="BTC-USD", now=later)
        assert attempt.trade.side == "flat"
        assert "bracket stop" in attempt.trade.reason


@pytest.mark.parametrize("value,flip_minute,expected", [
    ("0", 5, "flat"), ("bad", 5, None), ("-3", 5, None),
])
def test_probe_hold_setting(tmp_path: Path, monkeypatch, value, flip_minute, expected) -> None:
    monkeypatch.setenv("PROBE_MIN_HOLD_MINUTES", value)
    now = datetime(2026, 10, 20, 0, tzinfo=UTC)
    with connect(tmp_path / "paper.db") as connection:
        _seed_cycle(connection, now=now, probability_up=0.9)
        probe_trade_once(connection, instrument="BTC-USD", now=now)
        flip = now + timedelta(minutes=flip_minute)
        _seed_cycle(connection, now=flip, probability_up=0.1)
        attempt = probe_trade_once(connection, instrument="BTC-USD", now=flip)
        assert (attempt.trade.side if attempt.trade else None) == expected
        if expected is None:
            later = now + timedelta(minutes=16)
            _seed_cycle(connection, now=later, probability_up=0.1)
            assert probe_trade_once(connection, instrument="BTC-USD", now=later).trade.side == "flat"


def test_gated_cycle_has_no_probe_hold(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROBE_MIN_HOLD_MINUTES", "60")
    now = datetime(2026, 10, 20, 0, tzinfo=UTC)
    with connect(tmp_path / "paper.db") as connection:
        opened = run_paper_cycle(connection, predictor="gated", instrument="BTC-USD",
                                 probability_up=0.9, price=100, now_iso=now.isoformat())
        assert opened.trade.side == "long"
        flipped = run_paper_cycle(connection, predictor="gated", instrument="BTC-USD",
                                  probability_up=0.1, price=100,
                                  now_iso=(now + timedelta(minutes=5)).isoformat())
        assert flipped.trade.side == "flat"


def test_x10_book_takes_ten_times_the_1x_position_on_the_same_call(tmp_path: Path) -> None:
    """Same predictor, same stake, same prediction -- ten times the notional.
    That is the entire difference between the two books, and the only honest
    way to compare leverage is to put the identical call through both."""
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    with connect(database) as connection:
        _seed_cycle(connection, now=now, probability_up=0.9)
        one_x = probe_trade_once(connection, instrument="BTC-USD", now=now)
        ten_x = probe_x10_trade_once(connection, instrument="BTC-USD", now=now)

        rows = {
            row["predictor"]: row
            for row in connection.execute(
                "SELECT predictor, starting_cash, position_qty, leverage FROM paper_portfolio_state"
            )
        }

    assert one_x.trade is not None and ten_x.trade is not None
    assert ten_x.trade.side == "long"
    assert ten_x.trade.qty == pytest.approx(one_x.trade.qty * PROBE_X10_LEVERAGE)
    assert PROBE_X10_LEVERAGE == 10.0
    assert set(rows) == {PROBE_BOOK, PROBE_X10_BOOK}  # never written under "llm:agent"
    assert rows[PROBE_BOOK]["leverage"] == 1.0
    assert rows[PROBE_X10_BOOK]["leverage"] == 10.0
    # Same stake: 10x is ten times the exposure, not ten times the money.
    assert rows[PROBE_X10_BOOK]["starting_cash"] == STARTING_CASH_PHP


def test_x10_trade_reason_says_ungated_x10(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    now = datetime.now(UTC)
    with connect(database) as connection:
        _seed_cycle(connection, now=now, probability_up=0.9)
        attempt = probe_x10_trade_once(connection, instrument="BTC-USD", now=now)

    assert attempt.trade is not None
    assert "ungated x10" in attempt.trade.reason


def test_x10_book_is_held_by_the_same_interlock_and_stays_tripped_until_rearmed(
    tmp_path: Path,
) -> None:
    """Item 7 of the build decision: the x10 book gets no exemption. A trip
    latches -- a later healthy mark does not quietly clear it -- and only an
    explicit human rearm lets it trade again."""
    database = tmp_path / "paper.db"
    now_iso = datetime.now(UTC).isoformat()
    with connect(database) as connection:
        # check_and_enforce reads paper_equity_curve, so the paper schema has
        # to exist before it is ever called.
        initialize_portfolio(connection)
        # One honest mark at the full stake, so the interlock has a day-open
        # and a peak to measure the drop against.
        mark_to_market(
            connection, predictor=PROBE_X10_BOOK, instrument="BTC-USD",
            price=100.0, now_iso=now_iso, starting_cash=STARTING_CASH_PHP,
        )

        tripped = check_and_enforce(
            connection, predictor=PROBE_X10_BOOK, instrument="BTC-USD",
            starting_cash=STARTING_CASH_PHP, now_iso=now_iso, current_equity=900.0,
        )
        assert tripped.allowed is False
        assert tripped.severity == "soft"

        # Recovered mark, still blocked: the trip latches until a human clears it.
        still_blocked = check_and_enforce(
            connection, predictor=PROBE_X10_BOOK, instrument="BTC-USD",
            starting_cash=STARTING_CASH_PHP, now_iso=now_iso,
            current_equity=STARTING_CASH_PHP,
        )
        assert still_blocked.allowed is False

        assert rearm_kill_switch(
            connection, predictor=PROBE_X10_BOOK, instrument="BTC-USD",
            now_iso=now_iso, note="Dawn reviewed it",
        ) is True
        after_rearm = check_and_enforce(
            connection, predictor=PROBE_X10_BOOK, instrument="BTC-USD",
            starting_cash=STARTING_CASH_PHP, now_iso=now_iso,
            current_equity=STARTING_CASH_PHP,
        )
        assert after_rearm.allowed is True
