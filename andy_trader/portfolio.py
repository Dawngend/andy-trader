"""CT-08: paper portfolio accounting. Simulated capital only, real prices only.

This is what turns a forecast harness into something that can honestly be
called a trader. Before this module, "Andy Trader" logged predictions and
graded them; nothing ever held a position or had an equity curve. This adds
persistent simulated cash and a position, charges the same trading costs the
walk-forward backtest (CT-04) already uses so paper results are comparable to
backtested ones, and records every trade and every mark-to-market snapshot as
an append-only audit trail -- the same "write before you can lie about it"
discipline the rest of the project follows.

**Long, flat, or short -- 1x unless a book explicitly asks for more.** Every
book defaults to `leverage = 1.0`, which is the whole of what this module did
before leverage existed. A book that sets it higher (today only the
`probe_x10` paper experiment) multiplies its opening notional by it and gains
a liquidation price; see `liquidation_price` and `check_bracket`. Nothing
here can place a real order either way -- it is simulated cash throughout.

Shorting at 1x means borrowing exactly the notional already at risk and
selling it, not borrowing *beyond* your capital -- the risk profile is the
mirror image of going long, not bigger than it. A negative `position_qty`
represents the short: cash rises by the sale proceeds at entry (it is
simulated, not really spendable mid-position -- `cash + position_qty * price`
is still the only honest equity number) and falls by the cost to buy back and
cover at exit. Leverage above 1 does borrow beyond the account, in either
direction, which is why it brings a liquidation price with it and why the
only book using it is a paper experiment. Borrow/funding cost is still not
modelled, so a leveraged book's result is optimistic by exactly that much.

Flipping directly from long to short (or short to long) always passes
through flat first, one trade per cycle, same as everything else here: a
single trade doing two things atomically is exactly the kind of shortcut
that hides a sign error.

**This does not pick which predictor to trade.** `run_paper_cycle` trades
whatever predictor name you point it at -- a baseline, or a CT-07 model, once
one clears the promotion gate. It does not auto-select a promoted model. That
wiring, and only running promoted models live, is intentionally left as a
separate follow-up so an unvetted model is never trading paper capital by
accident.

Fee and slippage default to the same 10bps / 5bps the backtest uses
(`andy_trader.backtest`), so paper results and backtested results are
comparable, not measuring two different cost worlds.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import math
import os
import sqlite3
from typing import Literal, Mapping, Sequence

DEFAULT_STARTING_CASH = 10_000.0
DEFAULT_STOP_PCT = 2.0
DEFAULT_TARGET_PCT = 4.0

# Environment override for the stake a NEW paper portfolio opens with.
#
# The number is deliberately not cosmetic. Every cost in this module is charged
# in basis points, so percentage returns are scale-invariant: $80,000 and $128
# produce the identical return curve. What the stake size actually changes is
# what it makes VISIBLE. A book funded with a real, losable amount exposes
# constraints that a large round number hides -- most importantly that real
# venues enforce a minimum order notional (commonly $5-10 on spot), so a stake
# spread thin enough per instrument could not be executed at all. Simulating a
# size you would not really trade is a quiet way to get an unexecutable result.
STARTING_CASH_ENV = "CRYPTO_PAPER_STARTING_CASH"


def configured_starting_cash(environ: "Mapping[str, str] | None" = None) -> float:
    """Starting stake for a new paper portfolio, overridable by environment.

    Existing portfolios are untouched: `starting_cash` is stored per row, so
    changing this never silently rewrites the basis of a book already running.
    """

    import os

    source = environ if environ is not None else os.environ
    raw = str(source.get(STARTING_CASH_ENV, "")).strip()
    if not raw:
        return DEFAULT_STARTING_CASH
    try:
        value = float(raw)
    except ValueError as exc:
        raise PortfolioError(
            f"{STARTING_CASH_ENV} must be a number, got {raw!r}"
        ) from exc
    if value <= 0:
        raise PortfolioError(f"{STARTING_CASH_ENV} must be positive, got {value!r}")
    return value


def _bracket_percent(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise PortfolioError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or not 0 < value < 100:
        raise PortfolioError(f"{name} must be between 0 and 100, got {value!r}")
    return value


def _bracket_prices(entry: float, side: Side) -> tuple[float, float]:
    stop = _bracket_percent("CRYPTO_STOP_PCT", DEFAULT_STOP_PCT) / 100
    target = _bracket_percent("CRYPTO_TARGET_PCT", DEFAULT_TARGET_PCT) / 100
    if side == "long":
        return entry * (1 - stop), entry * (1 + target)
    return entry * (1 + stop), entry * (1 - target)


DEFAULT_LEVERAGE = 1.0
# How much of the posted margin an adverse move may consume before the
# position is force-closed. Deliberately below 1.0: a venue liquidates while
# some margin is still left, it does not wait for the account to reach exactly
# zero and then hope the fill lands there.
LIQUIDATION_MARGIN_FRACTION = 0.9


def _validate_leverage(leverage: float) -> float:
    if not math.isfinite(leverage) or leverage < 1:
        raise PortfolioError(f"leverage must be at least 1, got {leverage!r}")
    return float(leverage)


def liquidation_price(entry: float, side: Side, leverage: float) -> float | None:
    """Price at which an adverse move has eaten 90% of the posted margin.

    None for an unleveraged book: at 1x there is nothing borrowed, so there is
    nothing to liquidate -- a long can only ever decay toward zero, and a short
    here borrows exactly the notional already at risk (see module docstring).
    """

    if leverage <= 1.0:
        return None
    margin_move = LIQUIDATION_MARGIN_FRACTION / leverage
    if side == "long":
        return entry * (1 - margin_move)
    return entry * (1 + margin_move)


DEFAULT_FEE_BPS = 10.0
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_LONG_THRESHOLD = 0.55  # only go long when a predictor is meaningfully confident
DEFAULT_SHORT_THRESHOLD = 1.0 - DEFAULT_LONG_THRESHOLD  # symmetric: 0.45
DEFAULT_FLAT_THRESHOLD = 0.50  # exit either side back to flat once conviction fades to a coin flip
DEFAULT_MAX_PREDICTION_AGE_MINUTES = 20.0  # the unattended cycle runs every 15 minutes

Side = Literal["long", "flat", "short"]


class PortfolioError(ValueError):
    """Raised when a paper-trading operation cannot be carried out honestly."""


@dataclass(frozen=True)
class PortfolioState:
    """Current simulated holdings. One row per (predictor, instrument) pair."""

    predictor: str
    instrument: str
    starting_cash: float
    cash: float
    position_qty: float
    avg_entry_price: float | None
    updated_at: str
    stop_price: float | None = None
    target_price: float | None = None
    entry_at: str | None = None
    reentry_locked: bool = False
    leverage: float = DEFAULT_LEVERAGE


@dataclass(frozen=True)
class PortfolioSummary:
    """Aggregate P&L across every (predictor, instrument) pair that has ever traded."""

    total_starting_cash: float
    total_equity: float
    total_return_pct: float
    total_trade_count: int
    winners: int
    losers: int
    best: tuple[str, str, float] | None
    worst: tuple[str, str, float] | None


@dataclass(frozen=True)
class Trade:
    """One executed paper trade, immutable once written."""

    predictor: str
    instrument: str
    side: Side
    qty: float
    price: float
    fee_cost: float
    slippage_cost: float
    cash_after: float
    executed_at: str
    reason: str


def initialize_portfolio(connection: sqlite3.Connection) -> None:
    from andy_trader.order_check import initialize_order_checks

    initialize_order_checks(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_portfolio_state (
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            starting_cash REAL NOT NULL,
            cash REAL NOT NULL,
            position_qty REAL NOT NULL,
            avg_entry_price REAL,
            updated_at TEXT NOT NULL,
            stop_price REAL,
            target_price REAL,
            entry_at TEXT,
            reentry_locked INTEGER NOT NULL DEFAULT 0,
            leverage REAL NOT NULL DEFAULT 1,
            PRIMARY KEY (predictor, instrument)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            fee_cost REAL NOT NULL,
            slippage_cost REAL NOT NULL,
            cash_after REAL NOT NULL,
            executed_at TEXT NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_equity_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            cash REAL NOT NULL,
            position_qty REAL NOT NULL,
            mark_price REAL NOT NULL,
            position_value REAL NOT NULL,
            equity REAL NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS paper_equity_curve_lookup "
        "ON paper_equity_curve(predictor, instrument, recorded_at)"
    )
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(paper_portfolio_state)")
    }
    if "starting_cash" not in columns:
        # CT-08 initially inferred return from the first mark, which hides the
        # entry cost. Preserve the one already-created default portfolio while
        # adding the real denominator for every future custom bankroll.
        connection.execute(
            "ALTER TABLE paper_portfolio_state "
            f"ADD COLUMN starting_cash REAL NOT NULL DEFAULT {DEFAULT_STARTING_CASH}"
        )
    if "basis_started_at" not in columns:
        # When a book's capital basis is reset, its old equity points belong to
        # a different book and must not be drawn against the new starting value.
        # Without this, rebasing from $10,000 to $15.97 rendered as a vertical
        # cliff that read like a 99.8% loss instead of an accounting change.
        connection.execute(
            "ALTER TABLE paper_portfolio_state ADD COLUMN basis_started_at TEXT"
        )
    for name, definition in (
        ("stop_price", "REAL"),
        ("target_price", "REAL"),
        ("entry_at", "TEXT"),
        ("reentry_locked", "INTEGER NOT NULL DEFAULT 0"),
        # Every book that existed before leverage did was 1x, which is exactly
        # what this default says -- so an old database migrates without any
        # row changing meaning.
        ("leverage", "REAL NOT NULL DEFAULT 1"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE paper_portfolio_state ADD COLUMN {name} {definition}")
    connection.commit()


def get_or_create_state(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    starting_cash: float | None = None,
    leverage: float | None = None,
    now_iso: str,
) -> PortfolioState:
    # Resolved here, not as a default argument, because this function is the
    # single place a portfolio row is born. A hardcoded default here silently
    # wins over the configured stake for any caller that does not pass one --
    # which is exactly how the skill gate, whose only interest is "is there a
    # position open", ended up creating eight books at $10,000 apiece after
    # the stake had been set to PHP 1,000.
    if starting_cash is None:
        starting_cash = configured_starting_cash()
    if leverage is not None:
        leverage = _validate_leverage(leverage)
    initialize_portfolio(connection)
    row = connection.execute(
        "SELECT * FROM paper_portfolio_state WHERE predictor = ? AND instrument = ?",
        (predictor, instrument),
    ).fetchone()
    if row is not None:
        stored_leverage = (
            float(row["leverage"]) if row["leverage"] is not None else DEFAULT_LEVERAGE
        )
        position_qty = float(row["position_qty"])
        # A configured change only takes effect once the book is flat. An open
        # position was sized at the leverage it was opened with, so adopting a
        # new one mid-trade would check liquidation against a margin that was
        # never posted.
        effective_leverage = (
            leverage if leverage is not None and position_qty == 0 else stored_leverage
        )
        state = PortfolioState(
            predictor=row["predictor"],
            instrument=row["instrument"],
            starting_cash=float(row["starting_cash"]),
            cash=float(row["cash"]),
            position_qty=position_qty,
            avg_entry_price=row["avg_entry_price"],
            updated_at=row["updated_at"],
            stop_price=row["stop_price"],
            target_price=row["target_price"],
            entry_at=row["entry_at"],
            reentry_locked=bool(row["reentry_locked"]),
            leverage=effective_leverage,
        )
        if effective_leverage != stored_leverage:
            # The book's configured leverage is the authority, so a row created
            # before the book was leveraged (or migrated from an older schema)
            # picks it up instead of silently trading at 1x forever.
            _save_state(connection, state)
        return state
    row_leverage = leverage if leverage is not None else DEFAULT_LEVERAGE
    connection.execute(
        """
        INSERT INTO paper_portfolio_state
        (predictor, instrument, starting_cash, cash, position_qty, avg_entry_price, updated_at, leverage)
        VALUES (?, ?, ?, ?, 0.0, NULL, ?, ?)
        """,
        (predictor, instrument, starting_cash, starting_cash, now_iso, row_leverage),
    )
    connection.commit()
    return PortfolioState(
        predictor=predictor,
        instrument=instrument,
        starting_cash=starting_cash,
        cash=starting_cash,
        position_qty=0.0,
        avg_entry_price=None,
        updated_at=now_iso,
        leverage=row_leverage,
    )


def _save_state(connection: sqlite3.Connection, state: PortfolioState) -> None:
    connection.execute(
        """
        UPDATE paper_portfolio_state
        SET cash = ?, position_qty = ?, avg_entry_price = ?, updated_at = ?,
            stop_price = ?, target_price = ?, entry_at = ?, reentry_locked = ?,
            leverage = ?
        WHERE predictor = ? AND instrument = ?
        """,
        (
            state.cash,
            state.position_qty,
            state.avg_entry_price,
            state.updated_at,
            state.stop_price,
            state.target_price,
            state.entry_at,
            int(state.reentry_locked),
            state.leverage,
            state.predictor,
            state.instrument,
        ),
    )
    connection.commit()


def decide_side(probability_up: float, *, current_side: Side = "flat") -> Side:
    """Deterministic, deliberately simple v1 decision rule, with real hysteresis
    on both sides, symmetric between going long and going short.

    From flat: enter long above DEFAULT_LONG_THRESHOLD, enter short below
    DEFAULT_SHORT_THRESHOLD (0.45, the mirror of 0.55), otherwise stay flat.
    From long: only exit to flat once conviction drops to DEFAULT_FLAT_THRESHOLD
    (0.50) or below -- never jump straight to short. From short: only exit to
    flat once conviction rises to 0.50 or above -- never jump straight to long.
    Every direction change passes through flat first, one trade per cycle.

    The hysteresis needs the current side as an input: a single shared
    threshold per direction would let a predictor oscillating right around
    that one number flip a trade (and pay costs) every single cycle. The gap
    between the entry and exit thresholds is what stops that, and it only
    works if the function knows which side of it it's already on.
    """

    if not (0.0 <= probability_up <= 1.0):
        raise PortfolioError(f"probability_up out of range: {probability_up}")
    if current_side == "long":
        return "flat" if probability_up <= DEFAULT_FLAT_THRESHOLD else "long"
    if current_side == "short":
        return "flat" if probability_up >= DEFAULT_FLAT_THRESHOLD else "short"
    if probability_up >= DEFAULT_LONG_THRESHOLD:
        return "long"
    if probability_up <= DEFAULT_SHORT_THRESHOLD:
        return "short"
    return "flat"


def execute_paper_trade(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    target_side: Side,
    price: float,
    now_iso: str,
    reason: str,
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    starting_cash: float | None = None,
    size_fraction: float = 1.0,
    leverage: float | None = None,
) -> Trade | None:
    """Move the paper position to target_side if it is not already there.

    Returns None if no trade was needed (already at target_side). Fee and
    slippage are both charged against the notional traded, exactly once,
    deducted from cash -- this is real simulated cost, not a cosmetic number.

    `leverage` multiplies the opening notional: at 10x, a PHP 1,000 stake
    opens PHP 10,000 of exposure, so a 1% move is a 10% move on the stake.
    Because fee and slippage are charged against notional, both scale with it
    automatically. It defaults to the book's stored value (1x for every book
    that existed before leverage did), so an unleveraged caller is unaffected.
    """

    if price <= 0 or not price == price:  # noqa: PLR0133 - nan-safe without importing math
        raise PortfolioError(f"Cannot trade at a non-positive or NaN price: {price}")
    if not math.isfinite(size_fraction) or not 0 < size_fraction <= 1:
        raise PortfolioError(f"size_fraction must be between 0 and 1, got {size_fraction!r}")

    state = get_or_create_state(
        connection,
        predictor=predictor,
        instrument=instrument,
        starting_cash=starting_cash,
        leverage=leverage,
        now_iso=now_iso,
    )
    # The book's own stored leverage, not the argument: a close must use
    # whatever the position was actually opened at, even when the caller that
    # happens to be closing it passed nothing.
    book_leverage = state.leverage
    current_side: Side = (
        "long" if state.position_qty > 0 else "short" if state.position_qty < 0 else "flat"
    )
    if target_side == current_side:
        return None  # already where we want to be; no churn, no cost
    if current_side != "flat" and target_side != "flat":
        # Every direction change passes through flat first -- see module docstring.
        raise PortfolioError(
            f"Cannot jump directly from {current_side} to {target_side}; close to flat first"
        )

    cost_rate = (fee_bps + slippage_bps) / 10_000.0

    if target_side == "long":
        # Open long from flat: deploy the chosen share of cash, net of costs.
        notional = state.cash * size_fraction * book_leverage
        total_cost = notional * cost_rate
        deployable = notional - total_cost
        if deployable <= 0:
            raise PortfolioError("Insufficient cash to open a position after costs")
        qty = deployable / price
        fee_cost = notional * (fee_bps / 10_000.0)
        slippage_cost = notional * (slippage_bps / 10_000.0)
        stop_price, target_price = _bracket_prices(price, "long")
        new_state = PortfolioState(
            predictor=predictor,
            instrument=instrument,
            starting_cash=state.starting_cash,
            cash=state.cash - notional,
            position_qty=qty,
            avg_entry_price=price,
            updated_at=now_iso,
            stop_price=stop_price,
            target_price=target_price,
            entry_at=now_iso,
            leverage=book_leverage,
        )
        trade = Trade(
            predictor=predictor,
            instrument=instrument,
            side="long",
            qty=qty,
            price=price,
            fee_cost=fee_cost,
            slippage_cost=slippage_cost,
            cash_after=new_state.cash,
            executed_at=now_iso,
            reason=reason,
        )
    elif target_side == "short":
        # Open short from flat: sell borrowed qty sized off the same cash
        # fraction convention long uses, so long and short take on
        # identical notional risk, mirrored, not one bigger than the other.
        # Cash rises by the sale proceeds -- this is bookkeeping for a
        # simulated borrow, not literally spendable mid-position; only
        # `cash + position_qty * price` is ever the real equity number.
        notional = state.cash * size_fraction * book_leverage
        total_cost = notional * cost_rate
        proceeds = notional - total_cost
        if proceeds <= 0:
            raise PortfolioError("Insufficient cash to open a short after costs")
        qty = notional / price
        fee_cost = notional * (fee_bps / 10_000.0)
        slippage_cost = notional * (slippage_bps / 10_000.0)
        new_cash = state.cash + proceeds
        stop_price, target_price = _bracket_prices(price, "short")
        new_state = PortfolioState(
            predictor=predictor,
            instrument=instrument,
            starting_cash=state.starting_cash,
            cash=new_cash,
            position_qty=-qty,
            avg_entry_price=price,
            updated_at=now_iso,
            stop_price=stop_price,
            target_price=target_price,
            entry_at=now_iso,
            leverage=book_leverage,
        )
        trade = Trade(
            predictor=predictor,
            instrument=instrument,
            side="short",
            qty=qty,
            price=price,
            fee_cost=fee_cost,
            slippage_cost=slippage_cost,
            cash_after=new_cash,
            executed_at=now_iso,
            reason=reason,
        )
    elif current_side == "long":
        # Close a long to flat: sell the whole position, net of costs.
        notional = state.position_qty * price
        total_cost = notional * cost_rate
        proceeds = notional - total_cost
        fee_cost = notional * (fee_bps / 10_000.0)
        slippage_cost = notional * (slippage_bps / 10_000.0)
        new_cash = _floor_at_zero(state.cash + proceeds, book_leverage)
        new_state = PortfolioState(
            predictor=predictor,
            instrument=instrument,
            starting_cash=state.starting_cash,
            cash=new_cash,
            position_qty=0.0,
            avg_entry_price=None,
            updated_at=now_iso,
            reentry_locked="bracket " in reason,
            leverage=book_leverage,
        )
        trade = Trade(
            predictor=predictor,
            instrument=instrument,
            side="flat",
            qty=state.position_qty,
            price=price,
            fee_cost=fee_cost,
            slippage_cost=slippage_cost,
            cash_after=new_cash,
            executed_at=now_iso,
            reason=reason,
        )
    else:
        # Cover a short to flat: buy back the borrowed qty, net of costs.
        # Unlike closing a long (costs reduce what you receive), covering a
        # short costs MORE than the raw notional -- fees add to the outlay,
        # they never subtract from it. Getting this sign backwards would let
        # a short manufacture free money out of trading costs.
        qty = abs(state.position_qty)
        notional = qty * price
        total_cost = notional * cost_rate
        outlay = notional + total_cost
        fee_cost = notional * (fee_bps / 10_000.0)
        slippage_cost = notional * (slippage_bps / 10_000.0)
        new_cash = _floor_at_zero(state.cash - outlay, book_leverage)
        new_state = PortfolioState(
            predictor=predictor,
            instrument=instrument,
            starting_cash=state.starting_cash,
            cash=new_cash,
            position_qty=0.0,
            avg_entry_price=None,
            updated_at=now_iso,
            reentry_locked="bracket " in reason,
            leverage=book_leverage,
        )
        trade = Trade(
            predictor=predictor,
            instrument=instrument,
            side="flat",
            qty=qty,
            price=price,
            fee_cost=fee_cost,
            slippage_cost=slippage_cost,
            cash_after=new_cash,
            executed_at=now_iso,
            reason=reason,
        )

    _save_state(connection, new_state)
    connection.execute(
        """
        INSERT INTO paper_trades
        (predictor, instrument, side, qty, price, fee_cost, slippage_cost, cash_after, executed_at, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade.predictor, trade.instrument, trade.side, trade.qty, trade.price,
            trade.fee_cost, trade.slippage_cost, trade.cash_after, trade.executed_at, trade.reason,
        ),
    )
    connection.commit()
    return trade


def _floor_at_zero(equity: float, leverage: float) -> float:
    """Never let a leveraged book report negative equity.

    A leveraged position is closed at its liquidation price long before the
    margin is gone, so this should not bind; it is here because "you owe the
    venue money" is a claim a paper book has no business making, and a single
    gap through the liquidation level must not be able to invent one. Left
    alone at 1x so every unleveraged book's arithmetic is byte-for-byte what
    it was before leverage existed.
    """

    if leverage <= 1.0:
        return equity
    return max(0.0, equity)


def _tag_reason(reason_prefix: str, reason: str) -> str:
    return f"{reason_prefix}: {reason}" if reason_prefix else reason


def check_bracket(
    connection: sqlite3.Connection,
    predictor: str,
    instrument: str,
    now_iso: str,
    *,
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    reason_prefix: str = "",
    fresh_minutes: float | None = None,
) -> Trade | None:
    """Close at the first touched bracket on the finest stored candle series.

    For a leveraged book the bracket has a third level: the liquidation price
    (see `liquidation_price`). Within a bar it is only taken when it sits
    strictly closer to the entry than the stop does -- otherwise the price must
    have crossed the stop on its way there, so the stop is what fills. At 10x
    the stop (2%) is far tighter than liquidation (9%), so a 10x book is
    normally stopped out and never liquidated; the check exists for the
    leverage high enough that the order reverses.

    The level order only holds while price moves continuously. A bar that
    *opens* already past liquidation gapped over both levels at once, so the
    stop never had a chance to fill and liquidation is what happened -- that
    case is checked first.
    """

    state = get_or_create_state(
        connection, predictor=predictor, instrument=instrument, now_iso=now_iso
    )
    if state.position_qty == 0:
        return None
    if state.stop_price is None or state.target_price is None or state.entry_at is None:
        side: Side = "long" if state.position_qty > 0 else "short"
        stop_price, target_price = _bracket_prices(state.avg_entry_price, side)
        state = replace(
            state, stop_price=stop_price, target_price=target_price,
            entry_at=state.updated_at,
        )
        _save_state(connection, state)

    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'crypto_observations'"
    ).fetchone():
        return None
    bars = connection.execute(
        """
        SELECT interval, open_time, open, high, low FROM crypto_observations
        WHERE instrument = ? AND open_time >= ? AND open_time <= ?
          AND degraded = 0 AND high IS NOT NULL AND low IS NOT NULL
        """,
        (instrument, state.entry_at, now_iso),
    ).fetchall()
    if fresh_minutes is not None:
        moment = datetime.fromisoformat(now_iso)
        bars = [bar for bar in bars if bar["interval"] == "1m"
                and 60 <= (moment - datetime.fromisoformat(bar["open_time"])).total_seconds() <= fresh_minutes * 60]
    if not bars:
        return None

    def interval_minutes(interval: str) -> int:
        unit = interval[-1]
        return int(interval[:-1]) * {"m": 1, "h": 60, "d": 1440}[unit]

    open_side: Side = "long" if state.position_qty > 0 else "short"
    liquidation = (
        liquidation_price(float(state.avg_entry_price), open_side, state.leverage)
        if state.avg_entry_price is not None
        else None
    )

    finest = min(interval_minutes(bar["interval"]) for bar in bars)
    for bar in sorted(bars, key=lambda row: row["open_time"]):
        if interval_minutes(bar["interval"]) != finest:
            continue
        bar_open = bar["open"]
        if state.position_qty > 0:
            stop_hit = bar["low"] <= state.stop_price
            target_hit = bar["high"] >= state.target_price
            gapped = (
                liquidation is not None
                and bar_open is not None
                and bar_open <= liquidation
            )
            liquidated = (
                liquidation is not None
                and bar["low"] <= liquidation
                and (gapped or liquidation > state.stop_price)
            )
        else:
            stop_hit = bar["high"] >= state.stop_price
            target_hit = bar["low"] <= state.target_price
            gapped = (
                liquidation is not None
                and bar_open is not None
                and bar_open >= liquidation
            )
            liquidated = (
                liquidation is not None
                and bar["high"] >= liquidation
                and (gapped or liquidation < state.stop_price)
            )
        if liquidated:
            exit_price, exit_reason = liquidation, "liquidated"
        elif stop_hit:
            exit_price, exit_reason = state.stop_price, "bracket stop"
        elif target_hit:
            exit_price, exit_reason = state.target_price, "bracket target"
        else:
            continue
        return execute_paper_trade(
            connection, predictor=predictor, instrument=instrument,
            target_side="flat", price=exit_price, now_iso=now_iso,
            reason=_tag_reason(reason_prefix, exit_reason),
            fee_bps=fee_bps, slippage_bps=slippage_bps,
        )
    return None


def mark_to_market(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    price: float,
    now_iso: str,
    starting_cash: float | None = None,
) -> float:
    """Record one equity-curve snapshot at the current price. Returns total equity."""

    state = get_or_create_state(
        connection,
        predictor=predictor,
        instrument=instrument,
        starting_cash=starting_cash,
        now_iso=now_iso,
    )
    position_value = state.position_qty * price
    equity = _floor_at_zero(state.cash + position_value, state.leverage)
    connection.execute(
        """
        INSERT INTO paper_equity_curve
        (predictor, instrument, cash, position_qty, mark_price, position_value, equity, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (predictor, instrument, state.cash, state.position_qty, price, position_value, equity, now_iso),
    )
    connection.commit()
    return equity


@dataclass(frozen=True)
class PaperCycleResult:
    """Everything a caller needs to tell a risk-caused non-event apart from an
    ordinary one. Before this existed, a risk-blocked entry and a predictor
    simply deciding to stay flat looked identical -- both were `trade is None`
    with no further explanation anywhere in the journal."""

    trade: Trade | None
    equity: float
    risk_allowed: bool
    risk_reason: str
    forced_exit: bool


def run_paper_cycle(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    probability_up: float,
    price: float,
    now_iso: str,
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    starting_cash: float | None = None,
    reason_prefix: str = "",
    leverage: float | None = None,
    fresh_minutes: float | None = None,
    min_hold_minutes: float = 0,
) -> PaperCycleResult:
    """One full cycle: check the risk interlock, decide, trade if allowed, mark to market.

    The risk gate is evaluated every single cycle, not only when opening a
    new position -- a position that is already open and simply being held
    still needs to be able to trip the kill switch. This was a real bug,
    found by an independent review after the initial CT-10 build: a losing
    long position that never triggered a hysteresis exit could lose value
    forever without the interlock ever once looking at it.

    Once tripped, the interlock does not merely block new entries -- it
    actively forces an existing long position closed. A kill switch that
    only stops things from getting worse, while leaving the existing damage
    to sit there until the predictor's own logic happens to exit on its own,
    is not much of a kill switch.

    Exiting from an *unforced*, ordinary hysteresis-driven decision is never
    blocked by risk regardless of the interlock's verdict -- reducing risk is
    always allowed. The interlock's only powers are: block a new entry, or
    force an existing position flat. It can never trap capital in a position.

    **Order of operations differs by leverage, deliberately.** An unleveraged
    book evaluates the interlock first, exactly as it always has. A leveraged
    book resolves its bracket first, because at 10x the bar CLOSE and the
    price the stop actually filled at are two very different losses: a 5%
    adverse close is a 50% loss on the stake, well past the permanent halt,
    while the 2% stop that fired on the way there only cost 20%. Letting the
    interlock judge a leveraged book on the close would hard-halt it for a
    loss the stop had already prevented it from taking. So the bracket fills,
    and then the interlock sees the real realized number.
    """

    from andy_trader.risk import check_and_enforce  # local import: risk owns portfolio's risk, not the reverse
    from andy_trader.predict import load_closes
    from andy_trader.sizing import realized_volatility, size_fraction as fraction_for_volatility
    from andy_trader.order_check import check_order, recent_stopouts
    import statistics

    # Resolved here rather than as a default argument: a default is bound once at
    # import, so an environment change would not take effect until the process
    # restarted, and the two would silently disagree.
    if starting_cash is None:
        starting_cash = configured_starting_cash()

    current_state = get_or_create_state(
        connection,
        predictor=predictor,
        instrument=instrument,
        starting_cash=starting_cash,
        leverage=leverage,
        now_iso=now_iso,
    )
    current_side: Side = (
        "long" if current_state.position_qty > 0
        else "short" if current_state.position_qty < 0
        else "flat"
    )

    def _interlock(equity_now: float):
        return check_and_enforce(
            connection,
            predictor=predictor,
            instrument=instrument,
            starting_cash=current_state.starting_cash,
            now_iso=now_iso,
            current_equity=equity_now,
        )

    def _bracket() -> Trade | None:
        return check_bracket(
            connection, predictor, instrument, now_iso,
            fee_bps=fee_bps, slippage_bps=slippage_bps, reason_prefix=reason_prefix,
            fresh_minutes=fresh_minutes,
        )

    if current_state.leverage > 1.0:
        # Leveraged: bracket first, then the interlock on the realized fill.
        # See the order-of-operations note in this function's docstring.
        bracket_trade = _bracket()
        if bracket_trade is not None:
            filled_state = get_or_create_state(
                connection, predictor=predictor, instrument=instrument, now_iso=now_iso
            )
            decision = _interlock(
                _floor_at_zero(
                    filled_state.cash + filled_state.position_qty * price,
                    filled_state.leverage,
                )
            )
            equity = mark_to_market(
                connection, predictor=predictor, instrument=instrument,
                price=price, now_iso=now_iso, starting_cash=starting_cash,
            )
            return PaperCycleResult(
                trade=bracket_trade, equity=equity, risk_allowed=decision.allowed,
                risk_reason=decision.reason, forced_exit=False,
            )
        decision = _interlock(
            _floor_at_zero(
                current_state.cash + current_state.position_qty * price,
                current_state.leverage,
            )
        )
    else:
        # Mark-to-market at the live price, computed now rather than read from
        # history, so the interlock reacts to what this cycle's price is doing to
        # the position it is currently holding -- not to last cycle's number.
        live_equity = current_state.cash + current_state.position_qty * price

        decision = _interlock(live_equity)

        bracket_trade = _bracket()
        if bracket_trade is not None:
            equity = mark_to_market(
                connection, predictor=predictor, instrument=instrument,
                price=price, now_iso=now_iso, starting_cash=starting_cash,
            )
            return PaperCycleResult(
                trade=bracket_trade, equity=equity, risk_allowed=decision.allowed,
                risk_reason=decision.reason, forced_exit=False,
            )

    if current_side == "flat" and current_state.reentry_locked:
        if DEFAULT_SHORT_THRESHOLD < probability_up < DEFAULT_LONG_THRESHOLD:
            _save_state(connection, replace(current_state, reentry_locked=False))
        equity = mark_to_market(
            connection, predictor=predictor, instrument=instrument,
            price=price, now_iso=now_iso, starting_cash=starting_cash,
        )
        return PaperCycleResult(
            trade=None, equity=equity, risk_allowed=decision.allowed,
            risk_reason=decision.reason, forced_exit=False,
        )

    desired = decide_side(probability_up, current_side=current_side)
    forced_exit = False

    if not decision.allowed and current_side != "flat":
        # A trip forces ANY open position flat, long or short -- the kill
        # switch doesn't care which direction the damage came from.
        target: Side = "flat"
        forced_exit = True
        reason = f"RISK INTERLOCK forced exit: {decision.reason}"
    elif not decision.allowed and desired in ("long", "short") and current_side == "flat":
        target = "flat"
        reason = f"probability_up={probability_up:.4f}; risk-blocked entry ({desired}): {decision.reason}"
    else:
        target = desired
        reason = f"probability_up={probability_up:.4f} (was {current_side}) -> {target}"
    if (predictor in ("probe", "probe_x10") and target != current_side
            and current_side != "flat" and not forced_exit
            and current_state.entry_at is not None
            and datetime.fromisoformat(now_iso) - datetime.fromisoformat(current_state.entry_at)
            < timedelta(minutes=min_hold_minutes)):
        target = current_side
    reason = _tag_reason(reason_prefix, reason)

    trade: Trade | None = None
    if target != current_side:
        fraction = 1.0
        if target in ("long", "short"):
            has_observations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'crypto_observations'"
            ).fetchone()
            closes = (
                load_closes(connection, instrument, interval="1h", limit=169)
                if has_observations else []
            )
            volatility = realized_volatility([close for _, close in closes])
            fraction = fraction_for_volatility(volatility)
            vol_text = "unavailable" if volatility is None else f"{volatility:.1%}"
            reason += f"; size {fraction:.2f} at hourly vol {vol_text}"
            seven_day_vol = None
            week_start = datetime.fromisoformat(now_iso) - timedelta(days=7)
            week_closes = [(at, close) for at, close in closes if datetime.fromisoformat(at) >= week_start]
            if len(week_closes) >= 7:
                returns = [math.log(b / a) for (_, a), (_, b) in zip(week_closes, week_closes[1:])]
                seven_day_vol = statistics.pstdev(returns)
            relative_vol = volatility / seven_day_vol if volatility is not None and seven_day_vol else 0.0
            data_age = max(0.0, (datetime.fromisoformat(now_iso) - datetime.fromisoformat(closes[-1][0])).total_seconds() / 60) if closes else 60.0
            fraction = check_order(
                connection, predictor=predictor, instrument=instrument, side=target,
                now_iso=now_iso, volatility=relative_vol, size_fraction=fraction,
                data_age_minutes=data_age,
                recent_stopouts=recent_stopouts(connection, predictor, instrument, now_iso),
            )
        trade = execute_paper_trade(
            connection,
            predictor=predictor,
            instrument=instrument,
            target_side=target,
            price=price,
            now_iso=now_iso,
            reason=reason,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            starting_cash=starting_cash,
            size_fraction=fraction,
            leverage=leverage,
        )

    equity = mark_to_market(
        connection,
        predictor=predictor,
        instrument=instrument,
        price=price,
        now_iso=now_iso,
        starting_cash=starting_cash,
    )
    return PaperCycleResult(
        trade=trade,
        equity=equity,
        risk_allowed=decision.allowed,
        risk_reason=decision.reason,
        forced_exit=forced_exit,
    )


def fetch_equity_curve(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    limit: int = 500,
) -> list[dict[str, object]]:
    initialize_portfolio(connection)
    rows = connection.execute(
        """
        SELECT * FROM paper_equity_curve
        WHERE predictor = ? AND instrument = ?
        ORDER BY recorded_at DESC
        LIMIT ?
        """,
        (predictor, instrument, limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def portfolio_summary(
    connection: sqlite3.Connection,
    *,
    include_predictors: Sequence[str] | None = None,
    exclude_predictors: Sequence[str] = (),
) -> PortfolioSummary:
    """Roll up portfolio rows without creating a second source of live equity.

    Each pair's latest recorded equity is already the value shown by its chart,
    so the aggregate reads that same mark instead of reconstructing one from a
    separate price table. A pair with no mark yet is still real allocated capital
    and therefore contributes its current cash rather than zero. This read path
    intentionally does not initialize or migrate schema because the dashboard
    must remain unable to mutate the trading database.

    `include_predictors` and `exclude_predictors` let a caller roll up a
    subset of books -- the dashboard uses `exclude_predictors` to keep the
    probe book's ungated results out of the gated summary, and
    `include_predictors` to summarize the probe book on its own. Neither is
    set by default, so an ordinary call still rolls up every book.
    """

    empty_summary = PortfolioSummary(
        total_starting_cash=0.0,
        total_equity=0.0,
        total_return_pct=0.0,
        total_trade_count=0,
        winners=0,
        losers=0,
        best=None,
        worst=None,
    )
    state_table_exists = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'paper_portfolio_state'
        """
    ).fetchone()
    if state_table_exists is None:
        return empty_summary
    rows = connection.execute(
        """
        SELECT predictor, instrument, starting_cash, cash
        FROM paper_portfolio_state
        ORDER BY predictor, instrument
        """
    ).fetchall()
    if include_predictors is not None:
        rows = [row for row in rows if row["predictor"] in include_predictors]
    elif exclude_predictors:
        rows = [row for row in rows if row["predictor"] not in exclude_predictors]
    if not rows:
        return empty_summary

    total_starting_cash = 0.0
    total_equity = 0.0
    total_trade_count = 0
    returns: list[tuple[str, str, float]] = []

    for row in rows:
        predictor = str(row[0])
        instrument = str(row[1])
        latest_mark = connection.execute(
            """
            SELECT equity
            FROM paper_equity_curve
            WHERE predictor = ? AND instrument = ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
            """,
            (predictor, instrument),
        ).fetchone()
        latest_equity = (
            float(latest_mark[0]) if latest_mark is not None else float(row[3])
        )
        starting_equity = float(row[2])
        return_pct = (
            (latest_equity / starting_equity - 1.0) * 100.0 if starting_equity else 0.0
        )

        total_starting_cash += starting_equity
        total_equity += latest_equity
        total_trade_count += int(
            connection.execute(
                """
                SELECT COUNT(*) AS trade_count
                FROM paper_trades
                WHERE predictor = ? AND instrument = ?
                """,
                (predictor, instrument),
            ).fetchone()[0]
        )
        returns.append((predictor, instrument, return_pct))

    total_return_pct = (
        (total_equity / total_starting_cash - 1.0) * 100.0
        if total_starting_cash
        else 0.0
    )
    return PortfolioSummary(
        total_starting_cash=total_starting_cash,
        total_equity=total_equity,
        total_return_pct=total_return_pct,
        total_trade_count=total_trade_count,
        winners=sum(return_pct > 0.0 for _, _, return_pct in returns),
        losers=sum(return_pct < 0.0 for _, _, return_pct in returns),
        best=max(returns, key=lambda item: item[2]),
        worst=min(returns, key=lambda item: item[2]),
    )


def fetch_recent_trades(
    connection: sqlite3.Connection,
    *,
    predictor: str | None = None,
    instrument: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    initialize_portfolio(connection)
    clauses: list[str] = []
    params: list[object] = []
    if predictor is not None:
        clauses.append("predictor = ?")
        params.append(predictor)
    if instrument is not None:
        clauses.append("instrument = ?")
        params.append(instrument)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM paper_trades {where} ORDER BY executed_at DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = connection.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


@dataclass(frozen=True)
class PaperTradeAttempt:
    """Outcome of one staleness-checked attempt to paper-trade a live prediction.

    `skipped_reason` is set (and `trade`/`equity` are None) whenever the
    attempt refused to trade rather than trade on a stale decision or a stale
    price -- this is the one function both the manual CLI and the unattended
    cycle share, so that refusal logic can never drift between the two.
    """

    predictor: str
    instrument: str
    horizon: str
    price: float | None
    price_at: str | None
    prediction_created_at: str | None
    executed_at: str
    trade: Trade | None
    equity: float | None
    skipped_reason: str | None = None
    risk_allowed: bool = True
    risk_reason: str | None = None
    forced_exit: bool = False


def paper_trade_once(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    interval: str = "1h",
    horizon: str = "1h",
    now: datetime | None = None,
    max_prediction_age_minutes: float = DEFAULT_MAX_PREDICTION_AGE_MINUTES,
    max_data_age_minutes: float | None = None,
    skill_gate_disabled: bool = False,
    source_predictor: str | None = None,
    starting_cash: float | None = None,
    reason_prefix: str = "",
    leverage: float | None = None,
    batch_id: str | None = None,
    min_hold_minutes: float = 0,
) -> PaperTradeAttempt:
    """Paper-trade the latest live prediction for one (predictor, instrument), if fresh enough.

    Refuses (returns a `skipped_reason`, never raises) rather than trade on a
    stale prediction or a stale price -- exactly the scenario the 2026-09-05
    outage created, where reference data was hours old during a certificate
    failure. A refusal here is not an error; it is the correct behavior.

    `source_predictor` lets a book trade a different predictor's calls than
    the name its own capital is booked under -- the probe book (see
    `andy_trader.probe_book`) reads `llm:agent` predictions while keeping its
    own portfolio rows under the "probe" name, so it can never be mistaken
    for, or mixed into, the gated book that same predictor might also trade
    under its own name. Defaults to `predictor` so every other caller is
    unaffected.

    `leverage` is the book's leverage, stored on its portfolio row and applied
    to every position it opens. Defaults to the row's own value, 1x for every
    book that has one.
    """

    from andy_trader.predict import DEFAULT_MAX_DATA_AGE_MINUTES, load_closes

    now = now or datetime.now(UTC)
    now_iso = now.isoformat()
    max_data_age_minutes = (
        max_data_age_minutes if max_data_age_minutes is not None else DEFAULT_MAX_DATA_AGE_MINUTES
    )
    source_predictor = source_predictor or predictor

    if batch_id is None:
        row = connection.execute(
            """
            SELECT probability_up, reference_price, created_at FROM crypto_predictions
            WHERE predictor = ? AND instrument = ? AND horizon = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (source_predictor, instrument, horizon),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT p_up AS probability_up, created_at FROM llm_decisions "
            "WHERE batch_id = ? AND instrument = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
            (batch_id, instrument),
        ).fetchone()
    if row is None:
        return PaperTradeAttempt(
            predictor=predictor, instrument=instrument, horizon=horizon,
            price=None, price_at=None, prediction_created_at=None, executed_at=now_iso,
            trade=None, equity=None,
            skipped_reason=f"no {horizon} predictions found for predictor={source_predictor!r} instrument={instrument!r}",
        )

    closes = load_closes(connection, instrument, interval=interval, limit=1)
    if batch_id is not None:
        closes = [(row["open_time"], float(row["close"])) for row in connection.execute(
            "SELECT open_time, close FROM crypto_observations WHERE instrument = ? "
            "AND venue = 'binance' AND interval = '1m' AND degraded = 0 AND close > 0 "
            "AND open_time <= ? ORDER BY open_time DESC, first_seen_at DESC LIMIT 1",
            (instrument, (now - timedelta(minutes=1)).isoformat()),
        )]
    if not closes:
        return PaperTradeAttempt(
            predictor=predictor, instrument=instrument, horizon=horizon,
            price=None, price_at=None, prediction_created_at=row["created_at"], executed_at=now_iso,
            trade=None, equity=None,
            skipped_reason=f"no live price available for {instrument}",
        )
    latest_time, latest_price = closes[-1]

    prediction_age_minutes = (now - datetime.fromisoformat(row["created_at"])).total_seconds() / 60.0
    data_age_minutes = (now - datetime.fromisoformat(latest_time)).total_seconds() / 60.0

    if prediction_age_minutes > max_prediction_age_minutes:
        return PaperTradeAttempt(
            predictor=predictor, instrument=instrument, horizon=horizon,
            price=latest_price, price_at=latest_time, prediction_created_at=row["created_at"], executed_at=now_iso,
            trade=None, equity=None,
            skipped_reason=(
                f"latest {horizon} prediction is {prediction_age_minutes:.1f}m old; "
                f"refusing to trade a stale decision"
            ),
        )
    if data_age_minutes > max_data_age_minutes:
        return PaperTradeAttempt(
            predictor=predictor, instrument=instrument, horizon=horizon,
            price=latest_price, price_at=latest_time, prediction_created_at=row["created_at"], executed_at=now_iso,
            trade=None, equity=None,
            skipped_reason=(
                f"latest {interval} close is {data_age_minutes:.1f}m old; limit is {max_data_age_minutes:.1f}m"
            ),
        )

    # A predictor that has not beaten the base rate may not OPEN a position, but
    # is never prevented from closing one. Same asymmetry as the risk interlock:
    # an entry adds exposure and needs permission, an exit removes exposure and
    # must always be allowed through. Trapping a position inside a gate that has
    # just closed would be a new failure mode invented by the safety check.
    if not skill_gate_disabled:
        from andy_trader.paper_gate import evaluate_paper_eligibility

        state = get_or_create_state(
            connection, predictor=predictor, instrument=instrument,
            leverage=leverage, now_iso=now_iso,
        )
        if state.position_qty == 0:
            verdict = evaluate_paper_eligibility(
                connection, predictor=predictor, instrument=instrument, horizon=horizon
            )
            if not verdict.eligible:
                return PaperTradeAttempt(
                    predictor=predictor, instrument=instrument, horizon=horizon,
                    price=latest_price, price_at=latest_time,
                    prediction_created_at=row["created_at"], executed_at=now_iso,
                    trade=None, equity=None,
                    skipped_reason=f"skill gate: {verdict.reason}",
                )

    result = run_paper_cycle(
        connection,
        predictor=predictor,
        instrument=instrument,
        probability_up=float(row["probability_up"]),
        price=latest_price,
        now_iso=now_iso,
        starting_cash=starting_cash,
        reason_prefix=reason_prefix,
        leverage=leverage,
        fresh_minutes=max_data_age_minutes if batch_id is not None else None,
        min_hold_minutes=min_hold_minutes,
    )
    return PaperTradeAttempt(
        predictor=predictor, instrument=instrument, horizon=horizon,
        price=latest_price, price_at=latest_time, prediction_created_at=row["created_at"], executed_at=now_iso,
        trade=result.trade, equity=result.equity,
        risk_allowed=result.risk_allowed, risk_reason=result.risk_reason, forced_exit=result.forced_exit,
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path

    from andy_trader.env import REPO_ROOT, load_env_file
    from andy_trader.store import connect, default_database_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictor", required=True, help="Predictor name to paper-trade, e.g. baseline:momentum")
    parser.add_argument("--instrument", default="BTC-USD")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--horizon", default="1h")
    parser.add_argument("--database", help="Override database path")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--ignore-skill-gate",
        action="store_true",
        help=(
            "Open a position even if the predictor has not beaten the base rate. "
            "For backtests and debugging: the gate exists because a predictor that "
            "loses to the base rate lost real (simulated) money for days before "
            "anything objected."
        ),
    )
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    db_path = Path(args.database) if args.database else default_database_path()

    with connect(db_path) as connection:
        attempt = paper_trade_once(
            connection,
            predictor=args.predictor,
            instrument=args.instrument,
            interval=args.interval,
            horizon=args.horizon,
            skill_gate_disabled=args.ignore_skill_gate,
        )
        if attempt.skipped_reason:
            print(attempt.skipped_reason)
            return 1

        trade, equity = attempt.trade, attempt.equity
        result = {
            "predictor": args.predictor,
            "instrument": args.instrument,
            "horizon": args.horizon,
            "price": attempt.price,
            "price_at": attempt.price_at,
            "prediction_created_at": attempt.prediction_created_at,
            "executed_at": attempt.executed_at,
            "traded": trade is not None,
            "trade": trade.__dict__ if trade else None,
            "equity": equity,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Equity: {equity:.2f}  Traded: {trade.side if trade else 'no change'}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
