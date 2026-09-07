"""Measure whether both sides of a Polymarket round cost less than their payout.

The best quotes alone are not executable evidence: a cheap level may contain
only a few shares. This module therefore walks both ask books for the same
number of shares and records the observation without predicting the outcome or
creating any order path.

**A "mispriced" combined cost under $1 is not yet a net edge.** Polymarket
charges a taker fee on the main CLOB (confirmed at docs.polymarket.com/trading/fees,
2026-09-07): `fee = shares * feeRate * price * (1 - price)`, feeRate 0.07 for
Crypto-category markets, makers pay zero. Buying both outcomes means crossing
two separate asks, so this module treats the fee as charged independently on
each leg with no netting for holding a complete set -- the docs do not confirm
this, but it is the standard assumption absent evidence that Polymarket
recognizes "this trader intends to hold both sides" as a single unit, and it
is the conservative assumption for deciding whether an opportunity is real.

Measured on the first night of collection (2026-09-06/07, 97 rounds, $10
target): 15 rounds showed combined_cost under $1. Applying the fee above,
only 3 survived as `net_mispriced`. The other 12 were real, gross,
publicly-visible price gaps that the actual cost of trading fully consumed --
the same shape as every other finding in this project. Report both numbers,
never only the gross one.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from andy_trader.store import connect, default_database_path


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
USER_AGENT = "andy-trader-complete-set/1.0 (personal research)"
DEFAULT_TARGET_NOTIONAL = 10.0
NO_ORDER_BOOK_ERROR = "No orderbook exists for the requested token id"

# docs.polymarket.com/trading/fees, confirmed 2026-09-07. Taker-only; makers pay
# zero. feeRate is category-specific -- 0.07 is Crypto, which a BTC Up/Down
# market falls under by subject matter, though the docs do not name this
# specific market series. See the module docstring for what is and is not
# confirmed here.
CRYPTO_TAKER_FEE_RATE = 0.07


def taker_fee(shares: object, price: object, fee_rate: object = CRYPTO_TAKER_FEE_RATE) -> Decimal:
    """Polymarket's own formula: fee = shares * feeRate * price * (1 - price).

    Symmetric around p=0.50 (where it peaks) and near zero at either extreme.
    Returned as a Decimal so callers summing many small per-level fees do not
    accumulate binary floating-point error into a number this small.
    """

    shares_d = _decimal(shares, "shares")
    price_d = _decimal(price, "price")
    rate_d = _decimal(fee_rate, "fee_rate")
    if shares_d < 0:
        raise CompleteSetError(f"shares must not be negative, got {shares_d}")
    if not Decimal("0") <= price_d <= Decimal("1"):
        raise CompleteSetError(f"price must be in [0, 1], got {price_d}")
    return shares_d * rate_d * price_d * (Decimal("1") - price_d)


class CompleteSetError(RuntimeError):
    """Raised when public market data cannot support an honest observation."""


@dataclass(frozen=True)
class BookFill:
    """The executable result of walking one outcome's asks for equal shares."""

    best_ask: float | None
    best_ask_depth_shares: float | None
    best_ask_depth_notional: float | None
    filled_shares: float
    fill_cost: float | None
    fee_cost: float | None
    complete: bool


@dataclass(frozen=True)
class CompleteSetObservation:
    """One contemporaneous, append-only fact about a complementary pair.

    A $10 target means ten Up+Down pairs because each pair settles to exactly
    $1. `combined_cost` is the average paid per complete pair after walking both
    books, before trading fees -- a structural fact about the quotes. `mispriced`
    is None when either side cannot fill the target; None is essential because
    an unmeasurable market is not evidence of no mispricing.

    `net_combined_cost`/`net_mispriced` add Polymarket's own taker fee (see the
    module docstring) on top of `combined_cost`/`mispriced`. These are the
    numbers that answer "is this actually free money," and they are frequently
    a different answer: on this project's own first night of collection, 15
    rounds were `mispriced` and only 3 were `net_mispriced`.
    """

    round_id: str
    observed_at: str
    target_notional: float
    up_best_ask: float | None
    down_best_ask: float | None
    up_best_ask_depth_shares: float | None
    down_best_ask_depth_shares: float | None
    up_best_ask_depth_notional: float | None
    down_best_ask_depth_notional: float | None
    naive_combined_cost: float | None
    up_fill_shares: float
    down_fill_shares: float
    up_fill_cost: float | None
    down_fill_cost: float | None
    up_fee_cost: float | None
    down_fee_cost: float | None
    combined_cost: float | None
    mispriced: bool | None
    net_combined_cost: float | None
    net_mispriced: bool | None
    unmeasurable_reason: str | None


@dataclass(frozen=True)
class CompleteSetReport:
    """Round-level counts plus the observed distribution of executable costs."""

    observations: int
    rounds_observed: int
    two_sided_rounds: int
    naive_mispriced_rounds: int
    depth_measurable_rounds: int
    mispriced_rounds: int
    mispriced_costs: tuple[float, ...]
    net_mispriced_rounds: int
    net_mispriced_costs: tuple[float, ...]
    target_notional: float | None = None


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CompleteSetError(f"invalid {label}: {value!r}") from exc
    if not parsed.is_finite():
        raise CompleteSetError(f"invalid {label}: {value!r}")
    return parsed


def _ask_levels(book: Mapping[str, object]) -> list[tuple[Decimal, Decimal]]:
    asks = book.get("asks")
    if asks is None:
        raise CompleteSetError("order book is missing its asks field")
    if not isinstance(asks, list):
        raise CompleteSetError(f"order-book asks must be a list, got {type(asks).__name__}")

    levels: list[tuple[Decimal, Decimal]] = []
    for index, level in enumerate(asks):
        if not isinstance(level, Mapping):
            raise CompleteSetError(f"ask level {index} must be an object")
        if "price" not in level or "size" not in level:
            raise CompleteSetError(f"ask level {index} is missing price or size")
        price = _decimal(level["price"], f"ask[{index}].price")
        size = _decimal(level["size"], f"ask[{index}].size")
        if not Decimal("0") < price <= Decimal("1"):
            raise CompleteSetError(f"ask[{index}].price must be in (0, 1], got {price}")
        if size <= 0:
            raise CompleteSetError(f"ask[{index}].size must be positive, got {size}")
        levels.append((price, size))
    return sorted(levels, key=lambda item: item[0])


def walk_ask_book(
    book: Mapping[str, object],
    target_shares: float,
    *,
    fee_rate: float = CRYPTO_TAKER_FEE_RATE,
) -> BookFill:
    """Walk ascending asks for `target_shares` without inventing missing depth.

    The fee is accumulated per level as it is walked, at that level's own
    price, rather than approximated from the final average fill price. The fee
    formula is concave in price, so pricing it off an average would be a
    biased (if conservative) shortcut when a fill spans more than one level;
    walking it exactly costs nothing extra since the loop is already here.
    """

    target = _decimal(target_shares, "target_shares")
    if target <= 0:
        raise CompleteSetError(f"target_shares must be positive, got {target}")

    levels = _ask_levels(book)
    if not levels:
        return BookFill(None, None, None, 0.0, None, None, False)

    best_price, best_size = levels[0]
    remaining = target
    filled = Decimal("0")
    cost = Decimal("0")
    fee = Decimal("0")
    for price, size in levels:
        take = min(size, remaining)
        filled += take
        cost += take * price
        fee += taker_fee(take, price, fee_rate)
        remaining -= take
        if remaining == 0:
            break

    complete = remaining == 0
    return BookFill(
        best_ask=float(best_price),
        best_ask_depth_shares=float(best_size),
        best_ask_depth_notional=float(best_price * best_size),
        filled_shares=float(filled),
        fill_cost=float(cost) if complete else None,
        fee_cost=float(fee) if complete else None,
        complete=complete,
    )


def observe_complete_set(
    round_id: str,
    observed_at: str,
    up_book: Mapping[str, object],
    down_book: Mapping[str, object],
    *,
    target_notional: float = DEFAULT_TARGET_NOTIONAL,
    fee_rate: float = CRYPTO_TAKER_FEE_RATE,
) -> CompleteSetObservation:
    """Price equal Up and Down shares from snapshots, with no network access."""

    target = _decimal(target_notional, "target_notional")
    if target <= 0:
        raise CompleteSetError(f"target_notional must be positive, got {target}")
    if not round_id:
        raise CompleteSetError("round_id must not be empty")

    up = walk_ask_book(up_book, float(target), fee_rate=fee_rate)
    down = walk_ask_book(down_book, float(target), fee_rate=fee_rate)
    naive = (
        float(Decimal(str(up.best_ask)) + Decimal(str(down.best_ask)))
        if up.best_ask is not None and down.best_ask is not None
        else None
    )

    unavailable: list[str] = []
    if not up.complete:
        unavailable.append(f"Up filled {up.filled_shares:g}/{float(target):g} shares")
    if not down.complete:
        unavailable.append(f"Down filled {down.filled_shares:g}/{float(target):g} shares")

    combined: float | None = None
    mispriced: bool | None = None
    net_combined: float | None = None
    net_mispriced: bool | None = None
    if not unavailable:
        if up.fill_cost is None or down.fill_cost is None:  # pragma: no cover - guarded by complete
            raise CompleteSetError("a complete fill is missing its cost")
        if up.fee_cost is None or down.fee_cost is None:  # pragma: no cover - guarded by complete
            raise CompleteSetError("a complete fill is missing its fee")
        combined_decimal = (
            Decimal(str(up.fill_cost)) + Decimal(str(down.fill_cost))
        ) / target
        combined = float(combined_decimal)
        mispriced = combined_decimal < Decimal("1")

        net_decimal = combined_decimal + (
            Decimal(str(up.fee_cost)) + Decimal(str(down.fee_cost))
        ) / target
        net_combined = float(net_decimal)
        net_mispriced = net_decimal < Decimal("1")

    return CompleteSetObservation(
        round_id=round_id,
        observed_at=observed_at,
        target_notional=float(target),
        up_best_ask=up.best_ask,
        down_best_ask=down.best_ask,
        up_best_ask_depth_shares=up.best_ask_depth_shares,
        down_best_ask_depth_shares=down.best_ask_depth_shares,
        up_best_ask_depth_notional=up.best_ask_depth_notional,
        down_best_ask_depth_notional=down.best_ask_depth_notional,
        naive_combined_cost=naive,
        up_fill_shares=up.filled_shares,
        down_fill_shares=down.filled_shares,
        up_fill_cost=up.fill_cost,
        down_fill_cost=down.fill_cost,
        up_fee_cost=up.fee_cost,
        down_fee_cost=down.fee_cost,
        combined_cost=combined,
        mispriced=mispriced,
        net_combined_cost=net_combined,
        net_mispriced=net_mispriced,
        unmeasurable_reason="; ".join(unavailable) or None,
    )


def _http_json(url: str, timeout_seconds: float) -> object:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404 and url.partition("?")[0] == CLOB_BOOK_URL:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, Mapping) and payload.get("error") == NO_ORDER_BOOK_ERROR:
                return {"asks": [], "bids": []}
        raise CompleteSetError(f"GET {url} failed: HTTPError: {exc}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise CompleteSetError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc


def _list_field(value: object, label: str) -> list[object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CompleteSetError(f"{label} is not valid JSON") from exc
    if not isinstance(value, list):
        raise CompleteSetError(f"{label} must be a list")
    return value


def _up_down_market(payload: object) -> Mapping[str, object]:
    """The one market object inside a Gamma event whose outcomes are Up/Down.

    Shared by token lookup (for fetching order books) and resolution lookup
    (for settling paper trades), so both read the exact same event structure
    rather than two independently-maintained parsers drifting apart.
    """

    if not isinstance(payload, list) or not payload:
        raise CompleteSetError("Gamma returned no event for the current round")
    event = payload[0]
    if not isinstance(event, Mapping):
        raise CompleteSetError("Gamma event must be an object")
    markets = event.get("markets")
    if not isinstance(markets, list) or not markets:
        raise CompleteSetError("Gamma event contains no markets")

    for market in markets:
        if not isinstance(market, Mapping) or "outcomes" not in market:
            continue
        outcomes = _list_field(market.get("outcomes"), "market outcomes")
        if {str(o).casefold() for o in outcomes} >= {"up", "down"}:
            return market
    raise CompleteSetError("Gamma event contains no complementary Up/Down market")


def _outcome_tokens(payload: object) -> tuple[str, str]:
    market = _up_down_market(payload)
    outcomes = _list_field(market.get("outcomes"), "market outcomes")
    tokens = _list_field(market.get("clobTokenIds"), "market clobTokenIds")
    if len(outcomes) != len(tokens):
        raise CompleteSetError("market outcomes and token IDs have different lengths")
    by_outcome = {str(outcome).casefold(): str(token) for outcome, token in zip(outcomes, tokens)}
    return by_outcome["up"], by_outcome["down"]


def resolve_round_outcome(payload: object) -> str | None:
    """Which side actually won, read from Gamma's own settlement record.

    Returns "up", "down", or None when the round has not resolved yet (either
    still open, or resolved too recently for Gamma to have published it). A
    paper trade is settled only once this returns a real side -- never
    inferred from the market having merely closed, and never assumed from the
    structural "exactly one side must pay $1" argument, because that argument
    is exactly the kind of assumption a real settlement dispute or a void
    round would violate. Read the actual record; do not compute the answer
    from what "should" be true.
    """

    market = _up_down_market(payload)
    if not market.get("closed"):
        return None
    raw_prices = market.get("outcomePrices")
    if raw_prices is None:
        return None
    outcomes = _list_field(market.get("outcomes"), "market outcomes")
    prices = _list_field(raw_prices, "market outcomePrices")
    if len(outcomes) != len(prices):
        raise CompleteSetError("market outcomes and outcomePrices have different lengths")
    by_outcome = {
        str(outcome).casefold(): _decimal(price, "outcomePrices entry")
        for outcome, price in zip(outcomes, prices)
    }
    up_price, down_price = by_outcome.get("up"), by_outcome.get("down")
    if up_price is None or down_price is None:
        return None
    # A genuinely settled round has one side at (or essentially at) 1 and the
    # other at 0. Anything else -- both near 0.5, both near 0 -- is not a
    # result this module is willing to interpret, and is left unresolved
    # rather than guessed at.
    if up_price >= Decimal("0.99") and down_price <= Decimal("0.01"):
        return "up"
    if down_price >= Decimal("0.99") and up_price <= Decimal("0.01"):
        return "down"
    return None


def collect_current_round(
    *,
    target_notional: float = DEFAULT_TARGET_NOTIONAL,
    timeout_seconds: float = 8.0,
    now: float | None = None,
    http: Callable[[str, float], object] | None = None,
    fee_rate: float = CRYPTO_TAKER_FEE_RATE,
) -> CompleteSetObservation:
    """Fetch only the currently open 300-second round and price both books."""

    if timeout_seconds <= 0:
        raise CompleteSetError("timeout_seconds must be positive")
    target = _decimal(target_notional, "target_notional")
    if target <= 0:
        raise CompleteSetError(f"target_notional must be positive, got {target}")
    current_time = time.time() if now is None else now
    round_start = int(current_time) // 300 * 300
    round_id = f"btc-updown-5m-{round_start}"
    getter = http or _http_json

    event_url = f"{GAMMA_EVENTS_URL}?{urlencode({'slug': round_id})}"
    up_token, down_token = _outcome_tokens(getter(event_url, timeout_seconds))
    up_url = f"{CLOB_BOOK_URL}?{urlencode({'token_id': up_token})}"
    down_url = f"{CLOB_BOOK_URL}?{urlencode({'token_id': down_token})}"
    up_book = getter(up_url, timeout_seconds)
    down_book = getter(down_url, timeout_seconds)
    if not isinstance(up_book, Mapping) or not isinstance(down_book, Mapping):
        raise CompleteSetError("CLOB order-book payloads must be objects")

    return observe_complete_set(
        round_id,
        datetime.now(UTC).isoformat(),
        up_book,
        down_book,
        target_notional=float(target),
        fee_rate=fee_rate,
    )


# ---------------------------------------------------------------------------
# Paper trading: simulated cash against the real market, no order ever placed.
#
# A complete set is not a directional bet. Once bought, exactly one leg
# settles at $1/share, so opening the position at cost C for `target_notional`
# shares of each side has a KNOWN, deterministic payout once resolved -- there
# is no forecast to grade and no calibration to score, which is why this does
# not go through the skill gate the rest of the project uses for directional
# predictors. The only question worth asking here is "did net_mispriced say
# yes," and settlement is read from Polymarket's own resolution, never assumed
# from the structural argument that one side "must" pay out -- a disputed or
# void round would violate exactly that assumption.
# ---------------------------------------------------------------------------

DEFAULT_PAPER_STARTING_CASH = 100.0


class PaperAccountError(CompleteSetError):
    """Raised when a paper account operation cannot be carried out honestly."""


@dataclass(frozen=True)
class PaperAccountState:
    cash: float
    starting_cash: float


@dataclass(frozen=True)
class PaperTrade:
    id: int
    round_id: str
    opened_at: str
    cost: float
    fee: float
    total_debit: float
    target_notional: float
    settled_at: str | None
    outcome: str | None
    payout: float | None
    pnl: float | None


def initialize_paper_account(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS complete_set_paper_account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            starting_cash REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS complete_set_paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id TEXT NOT NULL UNIQUE,
            opened_at TEXT NOT NULL,
            cost REAL NOT NULL,
            fee REAL NOT NULL,
            total_debit REAL NOT NULL,
            target_notional REAL NOT NULL,
            settled_at TEXT,
            outcome TEXT CHECK (outcome IN ('up', 'down') OR outcome IS NULL),
            payout REAL,
            pnl REAL
        )
        """
    )
    connection.commit()


def get_or_create_paper_account(
    connection: sqlite3.Connection,
    *,
    starting_cash: float = DEFAULT_PAPER_STARTING_CASH,
    now_iso: str | None = None,
) -> PaperAccountState:
    initialize_paper_account(connection)
    row = connection.execute(
        "SELECT cash, starting_cash FROM complete_set_paper_account WHERE id = 1"
    ).fetchone()
    if row is not None:
        return PaperAccountState(cash=float(row["cash"]), starting_cash=float(row["starting_cash"]))
    if starting_cash <= 0:
        raise PaperAccountError(f"starting_cash must be positive, got {starting_cash!r}")
    connection.execute(
        "INSERT INTO complete_set_paper_account (id, cash, starting_cash, updated_at) "
        "VALUES (1, ?, ?, ?)",
        (starting_cash, starting_cash, now_iso or datetime.now(UTC).isoformat()),
    )
    connection.commit()
    return PaperAccountState(cash=starting_cash, starting_cash=starting_cash)


def open_paper_trade(
    connection: sqlite3.Connection,
    observation: CompleteSetObservation,
    *,
    now_iso: str | None = None,
) -> PaperTrade | None:
    """Open a paper position on one round, if and only if it is net_mispriced.

    Returns None (never raises) for every reason a real trader would simply
    not act: the round is not net_mispriced, it was already traded, or the
    account cannot afford it. Opening is refused rather than partially filled
    -- there is no partial version of "buy a complete set."
    """

    if not observation.net_mispriced:
        return None
    if observation.up_fill_cost is None or observation.down_fill_cost is None:
        return None  # pragma: no cover - net_mispriced implies these exist
    if observation.up_fee_cost is None or observation.down_fee_cost is None:
        return None  # pragma: no cover - net_mispriced implies these exist

    account = get_or_create_paper_account(connection)
    existing = connection.execute(
        "SELECT id FROM complete_set_paper_trades WHERE round_id = ?",
        (observation.round_id,),
    ).fetchone()
    if existing is not None:
        return None  # this round already has a paper position; never double up

    cost = observation.up_fill_cost + observation.down_fill_cost
    fee = observation.up_fee_cost + observation.down_fee_cost
    total_debit = cost + fee
    if total_debit > account.cash:
        return None  # cannot invent capital the account does not have

    moment = now_iso or datetime.now(UTC).isoformat()
    cursor = connection.execute(
        """
        INSERT INTO complete_set_paper_trades
        (round_id, opened_at, cost, fee, total_debit, target_notional)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (observation.round_id, moment, cost, fee, total_debit, observation.target_notional),
    )
    connection.execute(
        "UPDATE complete_set_paper_account SET cash = cash - ?, updated_at = ? WHERE id = 1",
        (total_debit, moment),
    )
    connection.commit()
    return PaperTrade(
        id=int(cursor.lastrowid),
        round_id=observation.round_id,
        opened_at=moment,
        cost=cost,
        fee=fee,
        total_debit=total_debit,
        target_notional=observation.target_notional,
        settled_at=None,
        outcome=None,
        payout=None,
        pnl=None,
    )


def settle_due_paper_trades(
    connection: sqlite3.Connection,
    *,
    timeout_seconds: float = 8.0,
    http: Callable[[str, float], object] | None = None,
    now_iso: str | None = None,
) -> dict[str, int]:
    """Credit every open paper trade whose round has genuinely resolved.

    A trade is left open, not guessed at, when Polymarket has not yet
    published a clean settlement for its round -- see `resolve_round_outcome`.
    """

    get_or_create_paper_account(connection)
    getter = http or _http_json
    pending = connection.execute(
        "SELECT id, round_id, total_debit, target_notional FROM complete_set_paper_trades "
        "WHERE settled_at IS NULL"
    ).fetchall()

    settled = 0
    unresolved = 0
    for row in pending:
        event_url = f"{GAMMA_EVENTS_URL}?{urlencode({'slug': row['round_id']})}"
        try:
            payload = getter(event_url, timeout_seconds)
            outcome = resolve_round_outcome(payload)
        except CompleteSetError:
            unresolved += 1
            continue
        if outcome is None:
            unresolved += 1
            continue

        # Exactly `target_notional` shares of each side were bought, and the
        # winning side pays $1/share -- the payout is target_notional itself.
        # This is the one place that structural fact is USED, and only after
        # `resolve_round_outcome` has confirmed a real side actually won.
        payout = row["target_notional"]
        pnl = payout - row["total_debit"]
        moment = now_iso or datetime.now(UTC).isoformat()
        connection.execute(
            "UPDATE complete_set_paper_trades "
            "SET settled_at = ?, outcome = ?, payout = ?, pnl = ? "
            "WHERE id = ? AND settled_at IS NULL",
            (moment, outcome, payout, pnl, row["id"]),
        )
        connection.execute(
            "UPDATE complete_set_paper_account SET cash = cash + ?, updated_at = ? WHERE id = 1",
            (payout, moment),
        )
        settled += 1
    connection.commit()
    return {"due": len(pending), "settled": settled, "unresolved": unresolved}


def paper_account_summary(connection: sqlite3.Connection) -> dict[str, object]:
    account = get_or_create_paper_account(connection)
    rows = connection.execute(
        "SELECT settled_at, pnl FROM complete_set_paper_trades"
    ).fetchall()
    settled_rows = [r for r in rows if r["settled_at"] is not None]
    wins = sum(1 for r in settled_rows if r["pnl"] is not None and r["pnl"] > 0)
    total_pnl = sum(float(r["pnl"]) for r in settled_rows if r["pnl"] is not None)
    return {
        "cash": account.cash,
        "starting_cash": account.starting_cash,
        "return_pct": (account.cash / account.starting_cash - 1.0) * 100.0,
        "total_trades": len(rows),
        "open_trades": len(rows) - len(settled_rows),
        "settled_trades": len(settled_rows),
        "wins": wins,
        "losses": len(settled_rows) - wins,
        "total_pnl": total_pnl,
    }


def record_complete_set_observation(
    connection: sqlite3.Connection, observation: CompleteSetObservation
) -> int:
    """Append one observation even when its round was observed before."""

    cursor = connection.execute(
        """
        INSERT INTO complete_set_observations
        (round_id, observed_at, target_notional, up_best_ask, down_best_ask,
         up_best_ask_depth_shares, down_best_ask_depth_shares,
         up_best_ask_depth_notional, down_best_ask_depth_notional,
         naive_combined_cost, up_fill_shares, down_fill_shares, up_fill_cost,
         down_fill_cost, up_fee_cost, down_fee_cost, combined_cost, mispriced,
         net_combined_cost, net_mispriced, unmeasurable_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation.round_id,
            observation.observed_at,
            observation.target_notional,
            observation.up_best_ask,
            observation.down_best_ask,
            observation.up_best_ask_depth_shares,
            observation.down_best_ask_depth_shares,
            observation.up_best_ask_depth_notional,
            observation.down_best_ask_depth_notional,
            observation.naive_combined_cost,
            observation.up_fill_shares,
            observation.down_fill_shares,
            observation.up_fill_cost,
            observation.down_fill_cost,
            observation.up_fee_cost,
            observation.down_fee_cost,
            observation.combined_cost,
            None if observation.mispriced is None else int(observation.mispriced),
            observation.net_combined_cost,
            None if observation.net_mispriced is None else int(observation.net_mispriced),
            observation.unmeasurable_reason,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _effective_net(row: sqlite3.Row, *, fee_rate: float) -> tuple[float | None, bool | None]:
    """The best available net-of-fee verdict for one stored observation.

    Rows collected after fee-awareness was added carry an exact, per-level
    `net_combined_cost` and are used as-is. Rows collected before it (this
    project's entire first night of data) have no such column, but they do
    have enough raw fields -- fill cost and fill shares per leg -- to derive an
    average-price approximation rather than silently reporting these rounds as
    "not net-mispriced," which a bare COUNT would otherwise do by mistaking
    "never computed" for "computed and false." The approximation is a safe
    (if slightly conservative) stand-in: the fee curve is concave in price, so
    pricing a multi-level fill at its average price cannot understate the true
    per-level fee. See `walk_ask_book` for the exact version used going forward.
    """

    if row["net_mispriced"] is not None:
        value = row["net_combined_cost"]
        return (None if value is None else float(value), bool(row["net_mispriced"]))

    if row["mispriced"] is None:
        return None, None  # unmeasurable at collection time; still unmeasurable now

    up_shares, down_shares = row["up_fill_shares"], row["down_fill_shares"]
    up_cost, down_cost = row["up_fill_cost"], row["down_fill_cost"]
    target = row["target_notional"]
    if not up_shares or not down_shares or up_cost is None or down_cost is None or not target:
        return None, None  # pragma: no cover - defensive; mispriced implies these exist

    up_price = up_cost / up_shares
    down_price = down_cost / down_shares
    fee = (
        up_shares * fee_rate * up_price * (1 - up_price)
        + down_shares * fee_rate * down_price * (1 - down_price)
    )
    net = float(row["combined_cost"]) + fee / target
    return net, net < 1.0


def summarize_history(
    connection: sqlite3.Connection,
    *,
    target_notional: float | None = None,
    fee_rate: float = CRYPTO_TAKER_FEE_RATE,
) -> CompleteSetReport:
    """Summarize distinct rounds without mixing observations at different sizes."""

    where = ""
    params: tuple[object, ...] = ()
    if target_notional is not None:
        target = _decimal(target_notional, "target_notional")
        if target <= 0:
            raise CompleteSetError(f"target_notional must be positive, got {target}")
        where = "WHERE target_notional = ?"
        params = (float(target),)

    row = connection.execute(
        f"""
        SELECT COUNT(*) AS observations,
               COUNT(DISTINCT round_id) AS rounds_observed,
               COUNT(DISTINCT CASE
                   WHEN up_best_ask IS NOT NULL AND down_best_ask IS NOT NULL THEN round_id
               END) AS two_sided_rounds,
               COUNT(DISTINCT CASE WHEN naive_combined_cost < 1 THEN round_id END)
                   AS naive_mispriced_rounds,
               COUNT(DISTINCT CASE WHEN combined_cost IS NOT NULL THEN round_id END)
                   AS depth_measurable_rounds,
               COUNT(DISTINCT CASE WHEN mispriced = 1 THEN round_id END) AS mispriced_rounds
        FROM complete_set_observations
        {where}
        """,
        params,
    ).fetchone()
    if row is None:  # pragma: no cover - aggregate SELECT always returns one row
        raise CompleteSetError("complete-set history query returned no row")
    costs = tuple(
        float(item["combined_cost"])
        for item in connection.execute(
            f"""
            SELECT combined_cost
            FROM complete_set_observations
            WHERE mispriced = 1 AND combined_cost IS NOT NULL
              {"AND target_notional = ?" if target_notional is not None else ""}
            ORDER BY combined_cost
            """,
            params,
        )
    )

    # Net-of-fee verdicts cannot be pushed down into SQL for the pre-fee rows,
    # since deriving them needs Python-side arithmetic (_effective_net). Every
    # row is read once, in whichever direction (exact or approximated) applies.
    all_rows = connection.execute(
        f"""
        SELECT round_id, target_notional, mispriced, combined_cost,
               net_combined_cost, net_mispriced,
               up_fill_cost, up_fill_shares, down_fill_cost, down_fill_shares
        FROM complete_set_observations
        {where}
        """,
        params,
    ).fetchall()
    net_mispriced_round_ids: set[str] = set()
    net_costs_list: list[float] = []
    for item in all_rows:
        net_cost, net_flag = _effective_net(item, fee_rate=fee_rate)
        if net_flag:
            net_mispriced_round_ids.add(item["round_id"])
            if net_cost is not None:
                net_costs_list.append(net_cost)
    net_costs = tuple(sorted(net_costs_list))

    return CompleteSetReport(
        observations=int(row["observations"]),
        rounds_observed=int(row["rounds_observed"]),
        two_sided_rounds=int(row["two_sided_rounds"]),
        naive_mispriced_rounds=int(row["naive_mispriced_rounds"]),
        depth_measurable_rounds=int(row["depth_measurable_rounds"]),
        mispriced_rounds=int(row["mispriced_rounds"]),
        mispriced_costs=costs,
        net_mispriced_rounds=len(net_mispriced_round_ids),
        net_mispriced_costs=net_costs,
        target_notional=None if target_notional is None else float(target_notional),
    )


def _percentile(sorted_values: tuple[float, ...], fraction: float) -> float:
    if not sorted_values:
        raise CompleteSetError("cannot compute a percentile of no values")
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _print_observation(observation: CompleteSetObservation) -> None:
    print(f"round             : {observation.round_id}")
    print(f"observed at       : {observation.observed_at}")
    print(f"target payout     : ${observation.target_notional:.2f}")
    for label, price, shares, notional in (
        (
            "Up best ask",
            observation.up_best_ask,
            observation.up_best_ask_depth_shares,
            observation.up_best_ask_depth_notional,
        ),
        (
            "Down best ask",
            observation.down_best_ask,
            observation.down_best_ask_depth_shares,
            observation.down_best_ask_depth_notional,
        ),
    ):
        if price is None:
            print(f"{label:<18}: no asks")
        else:
            print(f"{label:<18}: ${price:.4f} ({shares:g} shares / ${notional:.4f})")
    if observation.naive_combined_cost is not None:
        print(f"naive combined    : ${observation.naive_combined_cost:.4f}")
    if observation.combined_cost is None:
        print(f"depth-walked      : unmeasurable ({observation.unmeasurable_reason})")
    else:
        print(f"depth-walked      : ${observation.combined_cost:.4f} per complete set (before fees)")
        print(f"mispriced         : {'yes' if observation.mispriced else 'no'} (before fees)")
        print(f"net of taker fee  : ${observation.net_combined_cost:.4f} per complete set")
        print(f"net mispriced     : {'yes' if observation.net_mispriced else 'no'} "
              "(the number that actually matters)")


def _print_report(report: CompleteSetReport) -> None:
    if report.target_notional is not None:
        print(f"target payout filter          : ${report.target_notional:.2f}")
    print(f"observations                 : {report.observations}")
    print(f"rounds observed              : {report.rounds_observed}")
    print(f"rounds with two-sided quotes : {report.two_sided_rounds}")
    print(f"naive sum under $1           : {report.naive_mispriced_rounds}")
    print(f"depth-measurable rounds      : {report.depth_measurable_rounds}")
    print(f"depth-walked under $1        : {report.mispriced_rounds}  (before fees)")
    print(f"net of taker fee, under $1   : {report.net_mispriced_rounds}  "
          "(the number that actually matters)")
    if not report.mispriced_costs:
        print("mispriced cost distribution  : none")
    else:
        values = report.mispriced_costs
        print(
            "gross cost distribution      : "
            f"min={values[0]:.4f}, p25={_percentile(values, 0.25):.4f}, "
            f"median={_percentile(values, 0.5):.4f}, p75={_percentile(values, 0.75):.4f}, "
            f"max={values[-1]:.4f}"
        )
    if not report.net_mispriced_costs:
        print("net cost distribution        : none")
    else:
        values = report.net_mispriced_costs
        print(
            "net cost distribution        : "
            f"min={values[0]:.4f}, p25={_percentile(values, 0.25):.4f}, "
            f"median={_percentile(values, 0.5):.4f}, p75={_percentile(values, 0.75):.4f}, "
            f"max={values[-1]:.4f}"
        )


def _print_paper_summary(summary: Mapping[str, object]) -> None:
    print("\n--- paper account (simulated cash, real market, no order ever placed) ---")
    print(f"cash               : ${summary['cash']:.4f} (started at ${summary['starting_cash']:.2f})")
    print(f"return             : {summary['return_pct']:+.2f}%")
    print(f"trades             : {summary['total_trades']} total, "
          f"{summary['open_trades']} open, {summary['settled_trades']} settled")
    if summary["settled_trades"]:
        print(f"wins / losses      : {summary['wins']} / {summary['losses']}")
    print(f"total pnl          : ${summary['total_pnl']:+.4f}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument(
        "--target-notional",
        type=float,
        default=DEFAULT_TARGET_NOTIONAL,
        help="Guaranteed complete-set payout to fill; default: $10",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--report", action="store_true", help="Summarize stored observations")
    parser.add_argument(
        "--paper",
        action="store_true",
        help=(
            "Also settle any due paper trades and open one for this observation "
            "if it is net_mispriced. Simulated cash only; never places an order."
        ),
    )
    parser.add_argument(
        "--paper-starting-cash",
        type=float,
        default=DEFAULT_PAPER_STARTING_CASH,
        help="Starting balance for a brand-new paper account; default: $100",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.database) if args.database else default_database_path()
    with connect(db_path) as connection:
        if args.report:
            _print_report(summarize_history(connection, target_notional=args.target_notional))
            if args.paper:
                _print_paper_summary(paper_account_summary(connection))
            return 0
        try:
            observation = collect_current_round(
                target_notional=args.target_notional,
                timeout_seconds=args.timeout,
            )
        except CompleteSetError as exc:
            print(f"complete-set collection failed: {exc}", file=sys.stderr)
            return 1
        record_complete_set_observation(connection, observation)
        if args.paper:
            get_or_create_paper_account(connection, starting_cash=args.paper_starting_cash)
            settlement = settle_due_paper_trades(connection, timeout_seconds=args.timeout)
            trade = open_paper_trade(connection, observation)
    _print_observation(observation)
    if args.paper:
        print(f"\npaper settlement  : {settlement['settled']} settled, "
              f"{settlement['unresolved']} still unresolved")
        if trade is not None:
            print(f"paper trade opened: round {trade.round_id}, debit ${trade.total_debit:.4f}")
        _print_paper_summary(paper_account_summary(connection))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
