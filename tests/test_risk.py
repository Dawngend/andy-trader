"""Tests for CT-10: the risk interlock. Every limit, both trip severities, the throttle."""

import sqlite3

import pytest

from andy_trader.portfolio import initialize_portfolio, mark_to_market
from andy_trader.risk import (
    RiskLimits,
    check_and_enforce,
    fetch_kill_switch_state,
    initialize_risk,
    rearm_kill_switch,
)


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_portfolio(connection)
    initialize_risk(connection)
    return connection


def _mark(connection, equity_price, *, predictor="p", instrument="BTC-USD", at="2026-09-05T00:00:00+00:00"):
    # mark_to_market reads position_qty from state; for these tests we only
    # need direct control over recorded equity, so seed the curve directly
    # when a fully flat mark at a given "price" would not by itself reach a
    # specific equity number. Simpler: drive it through real state changes.
    return mark_to_market(connection, predictor="p", instrument=instrument, price=equity_price, now_iso=at)


def test_allows_the_first_trade_with_no_history() -> None:
    connection = _conn()
    decision = check_and_enforce(
        connection, predictor="p", instrument="BTC-USD", starting_cash=10_000.0,
        now_iso="2026-09-05T00:00:00+00:00",
    )
    assert decision.allowed
    assert "no equity history" in decision.reason


def test_permanent_halt_trips_hard_and_cannot_be_rearmed() -> None:
    connection = _conn()
    # Simulate catastrophic loss: equity crashed to 55% of starting cash (45% loss).
    connection.execute(
        "INSERT INTO paper_portfolio_state (predictor, instrument, starting_cash, cash, position_qty, updated_at) "
        "VALUES ('p', 'BTC-USD', 10000.0, 5500.0, 0.0, '2026-09-05T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES ('p', 'BTC-USD', 5500.0, 0.0, 100.0, 0.0, 5500.0, '2026-09-05T01:00:00+00:00')"
    )
    connection.commit()

    decision = check_and_enforce(
        connection, predictor="p", instrument="BTC-USD", starting_cash=10_000.0,
        now_iso="2026-09-05T02:00:00+00:00",
    )
    assert not decision.allowed
    assert decision.tripped_now
    assert decision.severity == "hard"

    state = fetch_kill_switch_state(connection, predictor="p", instrument="BTC-USD")
    assert state["tripped"] == 1
    assert state["severity"] == "hard"

    # A hard halt refuses rearm outright -- this is the whole point of it.
    rearmed = rearm_kill_switch(
        connection, predictor="p", instrument="BTC-USD", now_iso="2026-09-05T03:00:00+00:00",
        note="I promise it's fine now",
    )
    assert rearmed is False
    still_tripped = fetch_kill_switch_state(connection, predictor="p", instrument="BTC-USD")
    assert still_tripped["tripped"] == 1


def test_daily_loss_trips_soft_and_can_be_rearmed() -> None:
    connection = _conn()
    connection.execute(
        "INSERT INTO paper_portfolio_state (predictor, instrument, starting_cash, cash, position_qty, updated_at) "
        "VALUES ('p', 'BTC-USD', 10000.0, 9400.0, 0.0, '2026-09-05T08:00:00+00:00')"
    )
    # Equity opened today at 10000, now down to 9400 -- a 6% daily loss, over the 5% default.
    connection.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES ('p', 'BTC-USD', 10000.0, 0.0, 100.0, 0.0, 10000.0, '2026-09-05T00:05:00+00:00')"
    )
    connection.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES ('p', 'BTC-USD', 9400.0, 0.0, 94.0, 0.0, 9400.0, '2026-09-05T08:00:00+00:00')"
    )
    connection.commit()

    decision = check_and_enforce(
        connection, predictor="p", instrument="BTC-USD", starting_cash=10_000.0,
        now_iso="2026-09-05T09:00:00+00:00",
    )
    assert not decision.allowed
    assert decision.severity == "soft"
    assert decision.daily_loss_pct == pytest.approx(6.0, abs=0.01)

    rearmed = rearm_kill_switch(
        connection, predictor="p", instrument="BTC-USD", now_iso="2026-09-05T10:00:00+00:00",
        note="reviewed, resuming",
    )
    assert rearmed is True
    state = fetch_kill_switch_state(connection, predictor="p", instrument="BTC-USD")
    assert state["tripped"] == 0


def test_drawdown_from_peak_trips_even_without_a_daily_loss() -> None:
    connection = _conn()
    connection.execute(
        "INSERT INTO paper_portfolio_state (predictor, instrument, starting_cash, cash, position_qty, updated_at) "
        "VALUES ('p', 'BTC-USD', 10000.0, 8700.0, 0.0, '2026-09-05T00:00:00+00:00')"
    )
    # Peak was 12000 (a good run), now down to 8700 -- 27.5% drawdown from peak.
    # That's also a 13% loss vs starting cash, well under the 40% hard-halt
    # limit, so this should trip as "soft" (drawdown), not "hard" (total loss).
    connection.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES ('p', 'BTC-USD', 12000.0, 0.0, 120.0, 0.0, 12000.0, '2026-09-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES ('p', 'BTC-USD', 8700.0, 0.0, 87.0, 0.0, 8700.0, '2026-09-05T00:00:00+00:00')"
    )
    connection.commit()

    decision = check_and_enforce(
        connection, predictor="p", instrument="BTC-USD", starting_cash=10_000.0,
        now_iso="2026-09-05T01:00:00+00:00",
    )
    assert not decision.allowed
    assert decision.severity == "soft"
    assert decision.drawdown_pct == pytest.approx(27.5, abs=0.01)
    assert decision.total_loss_pct == pytest.approx(13.0, abs=0.01)  # under the 40% hard-halt limit


def test_trade_rate_throttle_blocks_without_tripping_the_kill_switch() -> None:
    connection = _conn()
    connection.execute(
        "INSERT INTO paper_portfolio_state (predictor, instrument, starting_cash, cash, position_qty, updated_at) "
        "VALUES ('p', 'BTC-USD', 10000.0, 10000.0, 0.0, '2026-09-05T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES ('p', 'BTC-USD', 10000.0, 0.0, 100.0, 0.0, 10000.0, '2026-09-05T00:00:00+00:00')"
    )
    for i in range(4):  # at the default limit of 4 trades/hour
        connection.execute(
            "INSERT INTO paper_trades (predictor, instrument, side, qty, price, fee_cost, slippage_cost, cash_after, executed_at, reason) "
            "VALUES ('p', 'BTC-USD', 'long', 1.0, 100.0, 0.0, 0.0, 0.0, ?, 'test')",
            (f"2026-09-05T00:{10+i}:00+00:00",),
        )
    connection.commit()

    decision = check_and_enforce(
        connection, predictor="p", instrument="BTC-USD", starting_cash=10_000.0,
        now_iso="2026-09-05T00:50:00+00:00",
    )
    assert not decision.allowed
    assert decision.severity is None  # throttled, not tripped
    assert decision.trades_last_hour == 4

    state = fetch_kill_switch_state(connection, predictor="p", instrument="BTC-USD")
    assert state is None or not state["tripped"]  # no persistent trip from a rate throttle


def test_already_tripped_kill_switch_blocks_without_recomputing() -> None:
    connection = _conn()
    connection.execute(
        "INSERT INTO risk_kill_switch (predictor, instrument, tripped, severity, tripped_at, tripped_reason) "
        "VALUES ('p', 'BTC-USD', 1, 'soft', '2026-09-05T00:00:00+00:00', 'earlier daily loss')"
    )
    connection.commit()

    decision = check_and_enforce(
        connection, predictor="p", instrument="BTC-USD", starting_cash=10_000.0,
        now_iso="2026-09-05T01:00:00+00:00",
    )
    assert not decision.allowed
    assert "earlier daily loss" in decision.reason


def test_rearming_a_switch_that_was_never_tripped_returns_false() -> None:
    connection = _conn()
    rearmed = rearm_kill_switch(
        connection, predictor="p", instrument="BTC-USD", now_iso="2026-09-05T00:00:00+00:00", note="n/a",
    )
    assert rearmed is False


def _seed_two_percent_daily_loss(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO paper_portfolio_state (predictor, instrument, starting_cash, cash, position_qty, updated_at) "
        "VALUES ('p', 'BTC-USD', 10000.0, 9800.0, 0.0, '2026-09-05T08:00:00+00:00')"
    )
    # Day opened at 10000 (00:05), now at 9800 (08:00) -- a real 2% loss *today*,
    # not just a coincidence of total-loss-vs-starting-cash.
    connection.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES ('p', 'BTC-USD', 10000.0, 0.0, 100.0, 0.0, 10000.0, '2026-09-05T00:05:00+00:00')"
    )
    connection.execute(
        "INSERT INTO paper_equity_curve (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at) "
        "VALUES ('p', 'BTC-USD', 9800.0, 0.0, 98.0, 0.0, 9800.0, '2026-09-05T08:00:00+00:00')"
    )
    connection.commit()


def test_limits_are_configurable_per_call() -> None:
    connection = _conn()
    _seed_two_percent_daily_loss(connection)

    # 2% loss is fine under the default 5% limit...
    decision = check_and_enforce(
        connection, predictor="p", instrument="BTC-USD", starting_cash=10_000.0,
        now_iso="2026-09-05T09:00:00+00:00",
    )
    assert decision.allowed
    assert decision.daily_loss_pct == pytest.approx(2.0, abs=0.01)

    # ...but not under a stricter 1% limit passed explicitly, on an identical setup.
    connection2 = _conn()
    _seed_two_percent_daily_loss(connection2)
    strict_decision = check_and_enforce(
        connection2, predictor="p", instrument="BTC-USD", starting_cash=10_000.0,
        now_iso="2026-09-05T09:00:00+00:00",
        limits=RiskLimits(max_daily_loss_pct=1.0),
    )
    assert not strict_decision.allowed


def test_holding_a_losing_long_now_trips_and_forces_an_exit() -> None:
    """CT-10 gap found by an independent review of the initial build: risk was
    only ever evaluated when *opening* a position, so a position that stayed
    long could lose value forever without the interlock ever once looking at
    it. Fixed by evaluating check_and_enforce every cycle and having a trip
    actively force the position flat, not merely block new entries."""
    from andy_trader.portfolio import get_or_create_state, run_paper_cycle

    connection = _conn()
    # Open through the real path (not a direct execute_paper_trade call) so
    # mark_to_market actually seeds the equity curve, exactly as a real
    # scheduled cycle would -- a position opened this way always has at
    # least one recorded equity point before the next cycle evaluates it.
    opening = run_paper_cycle(
        connection, predictor="p", instrument="BTC-USD", probability_up=0.9,
        price=100.0, now_iso="2026-09-05T00:00:00+00:00",
    )
    assert opening.trade is not None and opening.trade.side == "long"

    # Price collapses 60% -- past the 40% permanent-halt threshold -- while the
    # predictor still says "stay long" (0.9 confidence). Before the fix this
    # never even called check_and_enforce because target == current_side.
    result = run_paper_cycle(
        connection, predictor="p", instrument="BTC-USD", probability_up=0.9,
        price=40.0, now_iso="2026-09-05T01:00:00+00:00",
    )

    assert result.trade is not None, "the kill switch must force the position closed, not just block new entries"
    assert result.trade.side == "flat"
    assert result.forced_exit is True
    assert result.risk_allowed is False
    assert "RISK INTERLOCK forced exit" in result.trade.reason

    state = fetch_kill_switch_state(connection, predictor="p", instrument="BTC-USD")
    assert state["tripped"] == 1
    assert state["severity"] == "hard"

    # The position is genuinely flat now -- holding it could never resume
    # silently bleeding further, which was the whole point of the fix.
    post_state = get_or_create_state(
        connection, predictor="p", instrument="BTC-USD", now_iso="2026-09-05T01:00:00+00:00"
    )
    assert post_state.position_qty == 0.0


def test_ordinary_hysteresis_exit_is_never_mislabeled_as_a_forced_exit() -> None:
    """A predictor exiting on its own (probability dropped, nothing was ever
    tripped) must not be confused with a risk-forced exit in the trade log."""
    from andy_trader.portfolio import run_paper_cycle

    connection = _conn()
    opening = run_paper_cycle(
        connection, predictor="p", instrument="BTC-USD", probability_up=0.9,
        price=100.0, now_iso="2026-09-05T00:00:00+00:00",
    )
    assert opening.trade is not None
    # Small gain, well within every limit -- probability drops to an ordinary exit.
    result = run_paper_cycle(
        connection, predictor="p", instrument="BTC-USD", probability_up=0.3,
        price=101.0, now_iso="2026-09-05T01:00:00+00:00",
    )

    assert result.trade is not None
    assert result.trade.side == "flat"
    assert result.forced_exit is False
    assert result.risk_allowed is True
    assert "RISK INTERLOCK" not in result.trade.reason


def test_blocked_entry_is_distinguishable_from_an_ordinary_no_trade_decision() -> None:
    """The second half of the same gap: a risk-blocked entry attempt must not
    look identical to the predictor simply deciding not to trade."""
    from andy_trader.portfolio import get_or_create_state, run_paper_cycle

    connection = _conn()
    connection.execute(
        "INSERT INTO risk_kill_switch (predictor, instrument, tripped, severity, tripped_at, tripped_reason) "
        "VALUES ('p', 'BTC-USD', 1, 'soft', '2026-09-05T00:00:00+00:00', 'earlier daily loss')"
    )
    connection.commit()

    result = run_paper_cycle(
        connection, predictor="p", instrument="BTC-USD", probability_up=0.9,
        price=100.0, now_iso="2026-09-05T01:00:00+00:00",
    )

    assert result.trade is None  # correctly stayed flat -- nothing to force closed
    assert result.risk_allowed is False
    assert "earlier daily loss" in result.risk_reason
    assert result.forced_exit is False  # blocked from entering is not the same as forced out
