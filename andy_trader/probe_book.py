"""The probe book: trade `llm:agent` predictions automatically, with no skill
gate and no approval step.

The gated paper book (`andy_trader.paper_gate`) exists so that nothing opens
a position until it has beaten the base rate and covered its own costs on a
real, independent sample -- `llm:agent` cannot earn that until it has traded
long enough to accumulate one. The probe book is the thing that lets it
start accumulating: it paper-trades every `llm:agent` call, every cycle, for
every scheduled instrument, with no configuration and no gate, so there is a
visible, honestly-costed record of what the raw model would have done from
day one.

There are two probe books, the 1x `probe` and the 10x `probe_x10`. They read
the same predictions and differ only in leverage, so putting the same calls
through both is what makes the cost of leverage visible rather than argued
about. Both are paper: simulated money, real prices, no real order anywhere.

It reuses `andy_trader.portfolio` entirely -- same sizing, same 2% stop / 4%
target bracket, same fees and slippage, same hysteresis that
forces a long-to-short (or short-to-long) swing through flat first. The only
differences from a gated book are: its capital is booked under the "probe"
name instead of "llm:agent", so it can never be mistaken for, or merged
into, a gated book's own track record; its stake is a fixed PHP 1,000 per
coin, independent of whatever CRYPTO_PAPER_STARTING_CASH the gated book is
configured with; the skill gate is bypassed outright; and every trade it
makes carries "ungated" in its reason, so the audit trail itself says why a
trade exists with no evidence behind it.

Because its rows live under a different predictor name, and it never writes
to `crypto_predictions`, the probe book is invisible to `paper_gate` (which
only ever scores `crypto_predictions` rows by predictor name) and to any
real-money evidence bar built on top of it -- it cannot accidentally count
toward either.
"""

from __future__ import annotations

from datetime import datetime
import sqlite3

from andy_trader.portfolio import PaperTradeAttempt, paper_trade_once

PROBE_BOOK = "probe"
SOURCE_PREDICTOR = "llm:agent"
STARTING_CASH_PHP = 1_000.0
REASON_TAG = "ungated"

# The 10x paper experiment. Same predictor, same stake, same bracket, same
# interlock -- the single difference is that every position it opens is ten
# times the notional, so a 2% move is a 20% move on the stake. It is a
# separate book name so its results can never be mixed into the 1x probe's
# track record, and it is paper only: nothing in this module can place a real
# order.
PROBE_X10_BOOK = "probe_x10"
PROBE_X10_LEVERAGE = 10.0
REASON_TAG_X10 = "ungated x10"


def probe_trade_once(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    interval: str = "1h",
    horizon: str = "1h",
    now: datetime | None = None,
    max_data_age_minutes: float | None = None,
) -> PaperTradeAttempt:
    """Paper-trade the latest `llm:agent` prediction into the probe book.

    Shares `paper_trade_once`'s staleness refusal (stale prediction, stale
    price) with the gated path -- a probe with no skill gate still has no
    business trading a stale decision. The only things it overrides are
    which book the trade is booked under, the stake, and the skill gate.
    """

    return paper_trade_once(
        connection,
        predictor=PROBE_BOOK,
        source_predictor=SOURCE_PREDICTOR,
        instrument=instrument,
        interval=interval,
        horizon=horizon,
        now=now,
        max_data_age_minutes=max_data_age_minutes,
        skill_gate_disabled=True,
        starting_cash=STARTING_CASH_PHP,
        reason_prefix=REASON_TAG,
    )


def probe_x10_trade_once(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    interval: str = "1h",
    horizon: str = "1h",
    now: datetime | None = None,
    max_data_age_minutes: float | None = None,
) -> PaperTradeAttempt:
    """Paper-trade the same `llm:agent` prediction into the 10x book.

    Identical to `probe_trade_once` in every respect except the book name, the
    leverage, and the reason tag -- same source predictor, same PHP 1,000
    stake, same staleness refusals, same risk interlock and kill switch. The
    10x stake buys a liquidation price the 1x book does not have; at the
    default 2% stop the stop is far tighter than it, so this book is normally
    stopped out rather than liquidated.
    """

    return paper_trade_once(
        connection,
        predictor=PROBE_X10_BOOK,
        source_predictor=SOURCE_PREDICTOR,
        instrument=instrument,
        interval=interval,
        horizon=horizon,
        now=now,
        max_data_age_minutes=max_data_age_minutes,
        skill_gate_disabled=True,
        starting_cash=STARTING_CASH_PHP,
        reason_prefix=REASON_TAG_X10,
        leverage=PROBE_X10_LEVERAGE,
    )
