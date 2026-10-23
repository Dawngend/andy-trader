"""The trend40 book: a slow, once-a-day, paper-only trend-following rule.

Every other book in this project trades the AI's direction calls many times
a day. Measured on the ledger, those calls have no edge over the next 15 to
480 minutes, and every round trip costs 30 bps (10 bps fee and 5 bps
slippage per leg), so the churn loses money. An offline test on 18 months of
daily Bybit candles found that a 40-day momentum rule at 1x was net positive
after the same costs in both halves of the data, with very low turnover.
This book trades that rule forward, on paper, to see if it holds up live.

It reuses `andy_trader.portfolio` exactly as the probe books do: same
`get_or_create_state`, same `execute_paper_trade`, same `mark_to_market`, same
costs. It also runs the same `check_and_enforce` risk interlock the probe
loop runs, so a kill switch tripped against this book's own equity still
forces it flat. It never touches the sender, the gated book, or the probe
books.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3

from andy_trader.portfolio import execute_paper_trade, get_or_create_state, mark_to_market
from andy_trader.risk import check_and_enforce

TREND_BOOK = "trend40"
STARTING_CASH = 1000.0
LEVERAGE = 1.0
LOOKBACK_DAYS = 40
STALE_PRICE_SECONDS = 150

# Same universe as run_probe_watch.INSTRUMENTS, duplicated rather than
# imported so this paper-only module never depends on the probe watch script.
INSTRUMENTS = tuple(f"{coin}-USD" for coin in ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"))


def initialize_trend_book(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS trend_rebalance_claims "
        "(day TEXT PRIMARY KEY, claimed_at TEXT NOT NULL)"
    )
    connection.commit()


def _completed_daily_closes(connection: sqlite3.Connection, instrument: str, *, today: str) -> list[float]:
    """Latest close per completed Bybit daily candle, oldest first.

    `degraded = 0` rows only, one per `open_time` (a candle re-observed later
    in the day is kept once, at its latest-seen value), and today's own
    candle is excluded -- it has not closed yet.
    """

    rows = connection.execute(
        """
        SELECT open_time, close FROM crypto_observations
        WHERE instrument = ? AND venue = 'bybit' AND interval = '1d' AND degraded = 0
        ORDER BY open_time ASC, last_seen_at ASC
        """,
        (instrument,),
    ).fetchall()
    by_day: dict[str, float] = {}
    for row in rows:
        if datetime.fromisoformat(row["open_time"]).date().isoformat() == today:
            continue  # today's candle has not closed yet
        by_day[row["open_time"]] = float(row["close"])
    return [by_day[day] for day in sorted(by_day)]


def _target_side(closes: list[float]) -> tuple[str, float] | None:
    if len(closes) < LOOKBACK_DAYS + 1:
        return None
    latest = closes[-1]
    earlier = closes[-1 - LOOKBACK_DAYS]
    if earlier == 0:
        return None
    change = latest / earlier - 1.0
    return ("long" if latest > earlier else "short"), change


def run_once(connection: sqlite3.Connection, *, now: datetime | None = None) -> None:
    initialize_trend_book(connection)
    moment = now or datetime.now(UTC)
    moment_iso = moment.isoformat()
    today = moment.date().isoformat()

    claimed_today = connection.execute(
        "SELECT 1 FROM trend_rebalance_claims WHERE day = ?", (today,)
    ).fetchone() is not None

    evaluated = False
    for instrument in INSTRUMENTS:
        price_row = connection.execute(
            "SELECT open_time, close FROM crypto_observations WHERE instrument = ? "
            "AND venue = 'binance' AND interval = '1m' AND degraded = 0 AND close > 0 "
            "AND open_time <= ? ORDER BY open_time DESC, first_seen_at DESC LIMIT 1",
            (instrument, (moment - timedelta(minutes=1)).isoformat()),
        ).fetchone()
        if price_row is None or (moment - datetime.fromisoformat(price_row["open_time"])).total_seconds() > STALE_PRICE_SECONDS:
            continue
        price = float(price_row["close"])

        state = get_or_create_state(connection, predictor=TREND_BOOK, instrument=instrument,
                                     starting_cash=STARTING_CASH, leverage=LEVERAGE, now_iso=moment_iso)
        decision = check_and_enforce(connection, predictor=TREND_BOOK, instrument=instrument,
                                      starting_cash=state.starting_cash, now_iso=moment_iso,
                                      current_equity=max(0.0, state.cash + state.position_qty * price))
        if not decision.allowed:
            if state.position_qty:
                execute_paper_trade(connection, predictor=TREND_BOOK, instrument=instrument,
                                     target_side="flat", price=price, now_iso=moment_iso,
                                     reason=f"trend40 RISK INTERLOCK forced exit: {decision.reason}")
        elif not claimed_today:
            evaluated = True
            closes = _completed_daily_closes(connection, instrument, today=today)
            target = _target_side(closes)
            if target is not None:
                side, change = target
                current_side = ("long" if state.position_qty > 0
                                 else "short" if state.position_qty < 0 else "flat")
                if side != current_side:
                    execute_paper_trade(
                        connection, predictor=TREND_BOOK, instrument=instrument,
                        target_side=side, price=price, now_iso=moment_iso,
                        reason=f"trend40: {LOOKBACK_DAYS}d return {change * 100:+.1f}% -> {side}",
                    )

        mark_to_market(connection, predictor=TREND_BOOK, instrument=instrument, price=price,
                        now_iso=moment_iso, starting_cash=STARTING_CASH)

    if evaluated and not claimed_today:
        connection.execute(
            "INSERT OR IGNORE INTO trend_rebalance_claims VALUES (?, ?)", (today, moment_iso)
        )
        connection.commit()
