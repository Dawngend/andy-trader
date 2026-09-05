"""CT-08: paper portfolio accounting. Simulated capital only, real prices only.

This is what turns a forecast harness into something that can honestly be
called a trader. Before this module, "Andy Trader" logged predictions and
graded them; nothing ever held a position or had an equity curve. This adds
persistent simulated cash and a position, charges the same trading costs the
walk-forward backtest (CT-04) already uses so paper results are comparable to
backtested ones, and records every trade and every mark-to-market snapshot as
an append-only audit trail -- the same "write before you can lie about it"
discipline the rest of the project follows.

**Deliberately long-or-flat, no leverage, no shorting, in this first version.**
Shorting a leveraged instrument needs a borrow/margin model this project has
no data for yet, and getting that quietly wrong is a worse failure mode than
not having it. Long-or-flat is honest about being step one.

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

from dataclasses import dataclass
from datetime import UTC, datetime
import sqlite3
from typing import Literal, Sequence

DEFAULT_STARTING_CASH = 10_000.0
DEFAULT_FEE_BPS = 10.0
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_LONG_THRESHOLD = 0.55  # only go long when a predictor is meaningfully confident
DEFAULT_FLAT_THRESHOLD = 0.50  # exit back to flat once conviction fades to a coin flip
DEFAULT_MAX_PREDICTION_AGE_MINUTES = 20.0  # the unattended cycle runs every 15 minutes

Side = Literal["long", "flat"]


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
    connection.commit()


def get_or_create_state(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    starting_cash: float = DEFAULT_STARTING_CASH,
    now_iso: str,
) -> PortfolioState:
    initialize_portfolio(connection)
    row = connection.execute(
        "SELECT * FROM paper_portfolio_state WHERE predictor = ? AND instrument = ?",
        (predictor, instrument),
    ).fetchone()
    if row is not None:
        return PortfolioState(
            predictor=row["predictor"],
            instrument=row["instrument"],
            starting_cash=float(row["starting_cash"]),
            cash=float(row["cash"]),
            position_qty=float(row["position_qty"]),
            avg_entry_price=row["avg_entry_price"],
            updated_at=row["updated_at"],
        )
    connection.execute(
        """
        INSERT INTO paper_portfolio_state
        (predictor, instrument, starting_cash, cash, position_qty, avg_entry_price, updated_at)
        VALUES (?, ?, ?, ?, 0.0, NULL, ?)
        """,
        (predictor, instrument, starting_cash, starting_cash, now_iso),
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
    )


def _save_state(connection: sqlite3.Connection, state: PortfolioState) -> None:
    connection.execute(
        """
        UPDATE paper_portfolio_state
        SET cash = ?, position_qty = ?, avg_entry_price = ?, updated_at = ?
        WHERE predictor = ? AND instrument = ?
        """,
        (
            state.cash,
            state.position_qty,
            state.avg_entry_price,
            state.updated_at,
            state.predictor,
            state.instrument,
        ),
    )
    connection.commit()


def decide_side(probability_up: float, *, current_side: Side = "flat") -> Side:
    """Deterministic, deliberately simple v1 decision rule, with real hysteresis.

    From flat, only enter long above DEFAULT_LONG_THRESHOLD. From long, only exit
    back to flat once conviction drops to DEFAULT_FLAT_THRESHOLD or below. This
    needs the current side as an input: a single shared threshold would let a
    predictor oscillating right around that one number flip a trade (and pay
    costs) every single cycle. The gap between the two thresholds is what stops
    that, and it only works if the function knows which side of it it's already on.
    """

    if not (0.0 <= probability_up <= 1.0):
        raise PortfolioError(f"probability_up out of range: {probability_up}")
    if current_side == "long":
        return "flat" if probability_up <= DEFAULT_FLAT_THRESHOLD else "long"
    return "long" if probability_up >= DEFAULT_LONG_THRESHOLD else "flat"


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
    starting_cash: float = DEFAULT_STARTING_CASH,
) -> Trade | None:
    """Move the paper position to target_side if it is not already there.

    Returns None if no trade was needed (already at target_side). Fee and
    slippage are both charged against the notional traded, exactly once,
    deducted from cash -- this is real simulated cost, not a cosmetic number.
    """

    if price <= 0 or not price == price:  # noqa: PLR0133 - nan-safe without importing math
        raise PortfolioError(f"Cannot trade at a non-positive or NaN price: {price}")

    state = get_or_create_state(
        connection,
        predictor=predictor,
        instrument=instrument,
        starting_cash=starting_cash,
        now_iso=now_iso,
    )
    currently_long = state.position_qty > 0
    if (target_side == "long") == currently_long:
        return None  # already where we want to be; no churn, no cost

    cost_rate = (fee_bps + slippage_bps) / 10_000.0

    if target_side == "long":
        # Go long: commit all cash to the position, net of costs.
        notional = state.cash
        total_cost = notional * cost_rate
        deployable = notional - total_cost
        if deployable <= 0:
            raise PortfolioError("Insufficient cash to open a position after costs")
        qty = deployable / price
        fee_cost = notional * (fee_bps / 10_000.0)
        slippage_cost = notional * (slippage_bps / 10_000.0)
        new_state = PortfolioState(
            predictor=predictor,
            instrument=instrument,
            starting_cash=state.starting_cash,
            cash=0.0,
            position_qty=qty,
            avg_entry_price=price,
            updated_at=now_iso,
        )
        trade = Trade(
            predictor=predictor,
            instrument=instrument,
            side="long",
            qty=qty,
            price=price,
            fee_cost=fee_cost,
            slippage_cost=slippage_cost,
            cash_after=0.0,
            executed_at=now_iso,
            reason=reason,
        )
    else:
        # Close to flat: sell the whole position, net of costs.
        notional = state.position_qty * price
        total_cost = notional * cost_rate
        proceeds = notional - total_cost
        fee_cost = notional * (fee_bps / 10_000.0)
        slippage_cost = notional * (slippage_bps / 10_000.0)
        new_cash = state.cash + proceeds
        new_state = PortfolioState(
            predictor=predictor,
            instrument=instrument,
            starting_cash=state.starting_cash,
            cash=new_cash,
            position_qty=0.0,
            avg_entry_price=None,
            updated_at=now_iso,
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


def mark_to_market(
    connection: sqlite3.Connection,
    *,
    predictor: str,
    instrument: str,
    price: float,
    now_iso: str,
    starting_cash: float = DEFAULT_STARTING_CASH,
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
    equity = state.cash + position_value
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
    starting_cash: float = DEFAULT_STARTING_CASH,
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
    """

    from andy_trader.risk import check_and_enforce  # local import: risk owns portfolio's risk, not the reverse

    current_state = get_or_create_state(
        connection,
        predictor=predictor,
        instrument=instrument,
        starting_cash=starting_cash,
        now_iso=now_iso,
    )
    current_side: Side = "long" if current_state.position_qty > 0 else "flat"

    # Mark-to-market at the live price, computed now rather than read from
    # history, so the interlock reacts to what this cycle's price is doing to
    # the position it is currently holding -- not to last cycle's number.
    live_equity = current_state.cash + current_state.position_qty * price

    decision = check_and_enforce(
        connection,
        predictor=predictor,
        instrument=instrument,
        starting_cash=current_state.starting_cash,
        now_iso=now_iso,
        current_equity=live_equity,
    )

    desired = decide_side(probability_up, current_side=current_side)
    forced_exit = False

    if not decision.allowed and current_side == "long":
        target: Side = "flat"
        forced_exit = True
        reason = f"RISK INTERLOCK forced exit: {decision.reason}"
    elif not decision.allowed and desired == "long" and current_side == "flat":
        target = "flat"
        reason = f"probability_up={probability_up:.4f}; risk-blocked entry: {decision.reason}"
    else:
        target = desired
        reason = f"probability_up={probability_up:.4f} (was {current_side}) -> {target}"

    trade: Trade | None = None
    if target != current_side:
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
) -> PaperTradeAttempt:
    """Paper-trade the latest live prediction for one (predictor, instrument), if fresh enough.

    Refuses (returns a `skipped_reason`, never raises) rather than trade on a
    stale prediction or a stale price -- exactly the scenario the 2026-09-05
    outage created, where reference data was hours old during a certificate
    failure. A refusal here is not an error; it is the correct behavior.
    """

    from andy_trader.predict import DEFAULT_MAX_DATA_AGE_MINUTES, load_closes

    now = now or datetime.now(UTC)
    now_iso = now.isoformat()
    max_data_age_minutes = (
        max_data_age_minutes if max_data_age_minutes is not None else DEFAULT_MAX_DATA_AGE_MINUTES
    )

    row = connection.execute(
        """
        SELECT probability_up, reference_price, created_at FROM crypto_predictions
        WHERE predictor = ? AND instrument = ? AND horizon = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (predictor, instrument, horizon),
    ).fetchone()
    if row is None:
        return PaperTradeAttempt(
            predictor=predictor, instrument=instrument, horizon=horizon,
            price=None, price_at=None, prediction_created_at=None, executed_at=now_iso,
            trade=None, equity=None,
            skipped_reason=f"no {horizon} predictions found for predictor={predictor!r} instrument={instrument!r}",
        )

    closes = load_closes(connection, instrument, interval=interval, limit=1)
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

    result = run_paper_cycle(
        connection,
        predictor=predictor,
        instrument=instrument,
        probability_up=float(row["probability_up"]),
        price=latest_price,
        now_iso=now_iso,
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
