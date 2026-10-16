"""Tests for the probe book: ungated, auto-run paper trading of llm:agent calls."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from andy_trader.probe_book import PROBE_BOOK, SOURCE_PREDICTOR, STARTING_CASH_PHP, probe_trade_once
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
