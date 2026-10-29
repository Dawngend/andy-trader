"""Paper-only purchases of likely crypto outcomes, held to settlement."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import math
import re
import sqlite3
from urllib.parse import urlencode

from andy_trader.cheap_ask_book import compute_bootstrap_interval
from andy_trader.complete_set import (
    CLOB_BOOK_URL, CRYPTO_TAKER_FEE_RATE, GAMMA_EVENTS_URL,
    CompleteSetError, _http_json, walk_ask_book,
)

SHARES = 100
MIN_ASK = 0.80
MAX_ASK = 0.95  # cut from 0.98 on 2026-10-28: the 0.95 to 0.98 band lost money over 18 settled positions
MIN_MARGIN = 0.015
MAX_OPEN = 30
MAX_OPEN_PER_EVENT = 2
MAX_DAYS = 3
WINDOW_HOURS = 6
PREFILTER_BELOW = 0.70
CRYPTO_TAG_ID = 21
PRICE_WORDS = r"\b(reach|dip|above|below|up or down)\b"
PRICE_TARGET_WORDS = r"\b(reach|dip|hit)\b"
ABOVE_BELOW_WORDS = r"\b(above|below)\b"
PRICE_TARGET_NO = "price_target_no"
PRICE_TARGET_YES = "price_target_yes"
UP_DOWN = "up_down"
ABOVE_BELOW = "above_below"
OTHER = "other"
SLICES = (PRICE_TARGET_NO, PRICE_TARGET_YES, UP_DOWN, ABOVE_BELOW, OTHER)
PAUSED_SLICES = (UP_DOWN,)  # paused on 2026-10-29 after 220 settled positions totalled minus 790 dollars; remove the slice from the tuple to resume it.
SLICE_HEADER = ("Slice | Settled | Wins | Average entry price | Win rate | "
                "Gap before fees | Average pnl/share after fees | Average win | Average loss")
ENTRY_BANDS = ((0.90, "0.80 to 0.90"), (0.95, "0.90 to 0.95"), (1.0, "0.95 to 0.98"))
VERDICT_THRESHOLD = 100
PAGE_SIZE = 100
TIMEOUT_SECONDS = 10
SKIP_REASONS = ("ask_too_low", "ask_too_high", "thin_book", "margin_too_small")
DATA_ERRORS = (CompleteSetError, OSError, ValueError, TypeError, KeyError)
BOOK_READS = 0


def initialize_bond_book(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS bond_book_positions (
            market_id TEXT NOT NULL, event_id TEXT NOT NULL, title TEXT NOT NULL,
            side TEXT NOT NULL, entry_band TEXT NOT NULL, fill_price REAL NOT NULL,
            fee REAL NOT NULL, shares REAL NOT NULL, opened_at TEXT NOT NULL,
            ends_at TEXT NOT NULL, settled_at TEXT, win INTEGER, pnl REAL,
            UNIQUE (market_id, side)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS bond_book_skips (
            day TEXT NOT NULL, reason TEXT NOT NULL, count INTEGER NOT NULL,
            PRIMARY KEY (day, reason)
        )
    """)
    connection.commit()


def list_field(value: object) -> list:
    values = json.loads(value) if isinstance(value, str) else value
    if not isinstance(values, list):
        raise ValueError("Expected a Gamma list field")
    return values


def gamma_price(market: dict, side: str) -> float | None:
    if market.get("outcomePrices") is None:
        return None
    try:
        prices = [float(price) for price in list_field(market["outcomePrices"])]
        price = prices[list_field(market["outcomes"]).index(side)]
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    return price if math.isfinite(price) else None


def fetch_events(**params: object) -> list:
    payload = _http_json(f"{GAMMA_EVENTS_URL}?{urlencode(params)}", TIMEOUT_SECONDS)
    if not isinstance(payload, list):
        raise ValueError("Expected Gamma events")
    return payload


def end_date_windows(now: datetime, min_days: int = 0, max_days: int | None = None):
    end = now + timedelta(days=MAX_DAYS if max_days is None else max_days)
    step = timedelta(hours=WINDOW_HOURS)
    start = now + timedelta(days=min_days)
    while start < end:
        stop = min(start + step, end)
        yield start, stop
        start = stop


def active_events(now: datetime, errors: list | None = None, min_days: int = 0, max_days: int | None = None):
    errors = [] if errors is None else errors
    seen = set()
    for start, stop in end_date_windows(now, min_days=min_days, max_days=max_days):
        offset = 0
        try:
            while True:
                events = fetch_events(
                    tag_id=CRYPTO_TAG_ID, active="true", closed="false",
                    end_date_min=start.isoformat(), end_date_max=stop.isoformat(),
                    limit=PAGE_SIZE, offset=offset, order="id", ascending="true",
                )
                for event in events:
                    key = str(event.get("id"))
                    if key not in seen:
                        seen.add(key)
                        yield event
                if len(events) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
        except DATA_ERRORS as exc:
            errors.append({"error": str(exc)})


def eligible_market(market: dict, now: datetime) -> bool:
    if not market.get("active") or market.get("closed"):
        return False
    if not re.search(PRICE_WORDS, market.get("question", ""), re.IGNORECASE):
        return False
    ends = datetime.fromisoformat(market["endDate"])
    if not now < ends <= now + timedelta(days=MAX_DAYS):
        return False
    outcomes = list_field(market["outcomes"])
    tokens = list_field(market["clobTokenIds"])
    return len(outcomes) == len(tokens) == 2 and len(set(outcomes)) == 2 and len(set(tokens)) == 2


def record_skip(connection: sqlite3.Connection, now: datetime, reason: str, table: str = "bond_book_skips") -> None:
    with connection:
        connection.execute(f"""
            INSERT INTO {table} VALUES (?, ?, 1)
            ON CONFLICT(day, reason) DO UPDATE SET count = count + 1
        """, (now.date().isoformat(), reason))


def open_position(
    connection, event_id, market, side, price, fee, now,
    table: str = "bond_book_positions", bands=None,
    max_open: int | None = None, max_open_per_event: int | None = None,
) -> None:
    # Serialize the limit check and insert, without holding a lock during HTTP reads.
    effective_max_open = MAX_OPEN if max_open is None else max_open
    effective_max_event = MAX_OPEN_PER_EVENT if max_open_per_event is None else max_open_per_event
    effective_bands = ENTRY_BANDS if bands is None else bands
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        count, event_count = connection.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(event_id = ?), 0)
            FROM {table} WHERE settled_at IS NULL
        """, (event_id,)).fetchone()
        if count >= effective_max_open or event_count >= effective_max_event:
            return
        band = next(label for upper, label in effective_bands if price < upper)
        connection.execute(f"""
            INSERT OR IGNORE INTO {table}
            (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(market["id"]), event_id, market["question"], side, band,
              price, fee, SHARES, now.isoformat(), market["endDate"]))


def consider_side(connection, event_id, market, side, token, now) -> None:
    global BOOK_READS
    if connection.execute(
        "SELECT 1 FROM bond_book_positions WHERE market_id = ? AND side = ?",
        (str(market["id"]), side),
    ).fetchone():
        return
    if strategy_slice(market["question"], side) in PAUSED_SLICES:
        record_skip(connection, now, "slice_paused")
        return
    gamma = gamma_price(market, side)
    if gamma is not None and gamma < PREFILTER_BELOW:
        record_skip(connection, now, "ask_too_low")
        return
    BOOK_READS += 1
    book = _http_json(f"{CLOB_BOOK_URL}?{urlencode({'token_id': token})}", TIMEOUT_SECONDS)
    # walk_ask_book calls taker_fee for each filled level.
    fill = walk_ask_book(book, SHARES, fee_rate=CRYPTO_TAKER_FEE_RATE)
    if not fill.complete:
        record_skip(connection, now, "thin_book")
        return
    price = fill.fill_cost / SHARES
    fee = fill.fee_cost
    if price < MIN_ASK:
        reason = "ask_too_low"
    elif price > MAX_ASK:
        reason = "ask_too_high"
    elif 1 - price - fee / SHARES < MIN_MARGIN:
        reason = "margin_too_small"
    else:
        open_position(connection, event_id, market, side, price, fee, now)
        return
    record_skip(connection, now, reason)


def settled_winner(market: dict) -> str | None:
    if not market.get("closed") or market.get("outcomePrices") is None:
        return None
    outcomes = list_field(market["outcomes"])
    prices = [float(p) for p in list_field(market["outcomePrices"])]
    if len(outcomes) != 2 or len(prices) != 2 or len(set(outcomes)) != 2:
        return None
    if not all(math.isfinite(p) and 0 <= p <= 1 for p in prices):
        return None
    for index in (0, 1):
        if prices[index] >= 0.99 and prices[1 - index] <= 0.01:
            return outcomes[index]
    return None


def settle_positions(connection: sqlite3.Connection, now: datetime, errors: list, table: str = "bond_book_positions") -> None:
    events = connection.execute(
        f"SELECT DISTINCT event_id FROM {table} WHERE settled_at IS NULL"
    ).fetchall()
    for row in events:
        try:
            payload = fetch_events(id=row["event_id"])
        except DATA_ERRORS as exc:
            errors.append({"event_id": row["event_id"], "error": str(exc)})
            continue
        for event in payload:
            if str(event["id"]) != row["event_id"]:
                continue
            for market in event.get("markets", []):
                try:
                    winner = settled_winner(market)
                    if winner is None:
                        continue
                    outcomes = list_field(market["outcomes"])
                    with connection:
                        connection.execute(f"""
                            UPDATE {table}
                            SET win = (side = ?), settled_at = ?,
                                pnl = shares * ((side = ?) - fill_price) - fee
                            WHERE market_id = ? AND event_id = ? AND settled_at IS NULL
                              AND side IN (?, ?)
                        """, (winner, now.isoformat(), winner, str(market["id"]),
                              row["event_id"], *outcomes))
                except DATA_ERRORS as exc:
                    errors.append({"market_id": market.get("id"), "error": str(exc)})


def run_once(connection: sqlite3.Connection, *, now: datetime | None = None) -> list[dict]:
    global BOOK_READS
    BOOK_READS = 0
    initialize_bond_book(connection)
    now = now or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    errors = []
    settle_positions(connection, now, errors)
    try:
        for event in active_events(now, errors):
            if not event.get("active") or event.get("closed"):
                continue
            if not any(tag.get("slug") == "crypto" for tag in event.get("tags", [])):
                continue
            for market in event.get("markets", []):
                try:
                    if not eligible_market(market, now):
                        continue
                    sides = zip(list_field(market["outcomes"]), list_field(market["clobTokenIds"]))
                    for side, token in sides:
                        try:
                            consider_side(connection, str(event["id"]), market, side, token, now)
                        except DATA_ERRORS as exc:
                            errors.append({"market_id": market["id"], "side": side, "error": str(exc)})
                except DATA_ERRORS as exc:
                    errors.append({"market_id": market.get("id"), "error": str(exc)})
    except DATA_ERRORS as exc:
        errors.append({"error": str(exc)})
    return errors


def average(values) -> float:
    return sum(values) / len(values) if values else 0.0


def strategy_slice(title: str, side: str) -> str:
    price_target = re.search(PRICE_TARGET_WORDS, title, re.IGNORECASE)
    if price_target and side == "No":
        return PRICE_TARGET_NO
    if price_target and side == "Yes":
        return PRICE_TARGET_YES
    if "up or down" in title.lower():
        return UP_DOWN
    if re.search(ABOVE_BELOW_WORDS, title, re.IGNORECASE):
        return ABOVE_BELOW
    return OTHER


def report_row(label: str, rows: list) -> str:
    wins = sum(row["win"] for row in rows)
    price = average([row["fill_price"] for row in rows])
    rate = wins / len(rows) if rows else 0.0
    profit = average([row["pnl"] / row["shares"] for row in rows])
    win_dollars = average([row["pnl"] for row in rows if row["win"]])
    loss_dollars = average([row["pnl"] for row in rows if not row["win"]])
    if not rows:
        return f"{label} | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a"
    return (f"{label} | {len(rows)} | {wins} | {price:.4f} | {rate:.2%} | "
            f"{rate - price:+.2%} | ${profit:+.4f} | ${win_dollars:+.2f} | ${loss_dollars:+.2f}")


def format_report(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT * FROM bond_book_positions WHERE settled_at IS NOT NULL ORDER BY opened_at, market_id, side"
    ).fetchall()
    open_rows = connection.execute(
        "SELECT title, side FROM bond_book_positions WHERE settled_at IS NULL"
    ).fetchall()
    open_count = len(open_rows)
    skips = dict(connection.execute("SELECT reason, SUM(count) FROM bond_book_skips GROUP BY reason"))
    lines = ["Bond Book (paper only)", f"Open positions: {open_count}", f"Settled positions: {len(rows)}",
             "Skipped counts (side checks, including repeat checks):"]
    lines.extend(f"  {reason}: {skips.get(reason, 0)}" for reason in SKIP_REASONS)
    lines.append("\nEntry band | Settled | Wins | Average entry price | Win rate | Gap before fees | Average pnl/share after fees | Average win | Average loss")
    for _, label in ENTRY_BANDS:
        lines.append(report_row(label, [r for r in rows if r["entry_band"] == label]))
    lines.append(report_row("Overall", rows))
    losses = [r["pnl"] for r in rows if not r["win"]]
    wins = [r["pnl"] for r in rows if r["win"]]
    lines.append(f"Worst loss: ${min(losses, default=0):.2f}")
    ratio = f"{abs(average(losses)) / average(wins):.2f}" if losses and wins and average(wins) > 0 else "n/a"
    lines.append(f"Average wins wiped out by one average loss: {ratio}")
    low, high = compute_bootstrap_interval([r["pnl"] for r in rows])
    interval = f"[${low:+.2f}, ${high:+.2f}]" if rows else "n/a"
    lines.append(f"Bootstrap 95 percent interval for average pnl per position: {interval}")
    if len(rows) < VERDICT_THRESHOLD:
        lines.append(f"not enough settled positions yet ({len(rows)} of {VERDICT_THRESHOLD})")
    else:
        verdict = "positive edge shown" if low > 0 else "no edge shown"
        lines.append(f"Verdict: {verdict} ({len(rows)} settled, including {len(losses)} losses)")
    by_slice = {}
    for row in rows:
        by_slice.setdefault(strategy_slice(row["title"], row["side"]), []).append(row)
    open_by_slice = {}
    for row in open_rows:
        name = strategy_slice(row["title"], row["side"])
        open_by_slice[name] = open_by_slice.get(name, 0) + 1
    lines.extend(["", "By strategy slice",
                  "A slice needs 100 settled positions before it counts; "
                  "the slice with the most settled positions so far is shown first.",
                  SLICE_HEADER])
    for name in sorted(by_slice, key=lambda n: (-len(by_slice[n]), SLICES.index(n))):
        slice_rows = by_slice[name]
        lines.append(report_row(name, slice_rows))
        slice_losses = [r["pnl"] for r in slice_rows if not r["win"]]
        lines.append(f"  worst loss: ${min(slice_losses, default=0):.2f}")
    shown = [name for name in SLICES if name in by_slice or name in open_by_slice]
    if shown:
        open_counts = ", ".join(f"{name} {open_by_slice.get(name, 0)}" for name in shown)
        lines.append(f"Open positions by slice: {open_counts}")
    return "\n".join(lines)
