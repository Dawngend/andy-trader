"""Paper-only purchases of NO on far price-target crypto markets, held to settlement."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
import sqlite3
from urllib.parse import urlencode

from andy_trader import bond_book
from andy_trader.bond_book import (
    CLOB_BOOK_URL, CRYPTO_TAG_ID, CRYPTO_TAKER_FEE_RATE, DATA_ERRORS,
    PAGE_SIZE, TIMEOUT_SECONDS, active_events, average,
    compute_bootstrap_interval, fetch_events, gamma_price, list_field,
    open_position as bond_open_position, record_skip as bond_record_skip,
    report_row, settle_positions as bond_settle_positions,
    settled_winner, walk_ask_book,
)

# Reference bond_book._http_json directly so mocking bond_book._http_json works
def _http_json(url: str, timeout: int):
    return bond_book._http_json(url, timeout)

SHARES = 100
MIN_ASK = 0.85
MAX_ASK = 0.96
MAX_OPEN = 30
MAX_OPEN_PER_EVENT = 2
MIN_DAYS = 3
MAX_DAYS = 21
PREFILTER_BELOW = 0.70
PRICE_TARGET_WORDS = r"\b(reach|dip|hit)\b"
ENTRY_BANDS = ((0.90, "0.85 to 0.90"), (0.96, "0.90 to 0.96"))
VERDICT_THRESHOLD = 100
SKIP_REASONS = ("ask_too_low", "ask_too_high", "thin_book")
POSITIONS_TABLE = "price_target_book_positions"
SKIPS_TABLE = "price_target_book_skips"
BOOK_READS = 0


def initialize_price_target_book(connection: sqlite3.Connection) -> None:
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {POSITIONS_TABLE} (
            market_id TEXT NOT NULL, event_id TEXT NOT NULL, title TEXT NOT NULL,
            side TEXT NOT NULL, entry_band TEXT NOT NULL, fill_price REAL NOT NULL,
            fee REAL NOT NULL, shares REAL NOT NULL, opened_at TEXT NOT NULL,
            ends_at TEXT NOT NULL, settled_at TEXT, win INTEGER, pnl REAL,
            UNIQUE (market_id, side)
        )
    """)
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {SKIPS_TABLE} (
            day TEXT NOT NULL, reason TEXT NOT NULL, count INTEGER NOT NULL,
            PRIMARY KEY (day, reason)
        )
    """)
    connection.commit()


def eligible_market(market: dict, now: datetime) -> bool:
    if not market.get("active") or market.get("closed"):
        return False
    if not re.search(PRICE_TARGET_WORDS, market.get("question", ""), re.IGNORECASE):
        return False
    ends = datetime.fromisoformat(market["endDate"])
    if ends.tzinfo is None and now.tzinfo is not None:
        ends = ends.replace(tzinfo=UTC)
    elif ends.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if not now + timedelta(days=MIN_DAYS) <= ends <= now + timedelta(days=MAX_DAYS):
        return False
    outcomes = list_field(market["outcomes"])
    tokens = list_field(market["clobTokenIds"])
    if not (len(outcomes) == len(tokens) == 2 and len(set(outcomes)) == 2 and len(set(tokens)) == 2):
        return False
    return any(side.lower() == "no" for side in outcomes)


def record_skip(connection: sqlite3.Connection, now: datetime, reason: str) -> None:
    bond_record_skip(connection, now, reason, table=SKIPS_TABLE)


def open_position(connection: sqlite3.Connection, event_id: str, market: dict,
                  side: str, price: float, fee: float, now: datetime) -> None:
    bond_open_position(
        connection, event_id, market, side, price, fee, now,
        table=POSITIONS_TABLE, bands=ENTRY_BANDS,
        max_open=MAX_OPEN, max_open_per_event=MAX_OPEN_PER_EVENT,
    )


def consider_side(connection: sqlite3.Connection, event_id: str, market: dict,
                  side: str, token: str, now: datetime) -> None:
    global BOOK_READS
    if connection.execute(
        f"SELECT 1 FROM {POSITIONS_TABLE} WHERE market_id = ?",
        (str(market["id"]),),
    ).fetchone():
        return
    gamma = gamma_price(market, side)
    if gamma is not None and gamma < PREFILTER_BELOW:
        record_skip(connection, now, "ask_too_low")
        return
    BOOK_READS += 1
    book = _http_json(f"{CLOB_BOOK_URL}?{urlencode({'token_id': token})}", TIMEOUT_SECONDS)
    fill = walk_ask_book(book, SHARES, fee_rate=CRYPTO_TAKER_FEE_RATE)
    if not fill.complete:
        record_skip(connection, now, "thin_book")
        return
    price = fill.fill_cost / SHARES
    fee = fill.fee_cost
    if price < MIN_ASK:
        reason = "ask_too_low"
    elif price >= MAX_ASK:
        reason = "ask_too_high"
    else:
        open_position(connection, event_id, market, side, price, fee, now)
        return
    record_skip(connection, now, reason)


def settle_positions(connection: sqlite3.Connection, now: datetime, errors: list) -> None:
    bond_settle_positions(connection, now, errors, table=POSITIONS_TABLE)


def run_once(connection: sqlite3.Connection, *, now: datetime | None = None) -> list[dict]:
    global BOOK_READS
    BOOK_READS = 0
    initialize_price_target_book(connection)
    now = now or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    errors = []
    settle_positions(connection, now, errors)
    try:
        for event in active_events(now, errors, min_days=MIN_DAYS, max_days=MAX_DAYS):
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
                        if side.lower() == "no":
                            try:
                                consider_side(connection, str(event["id"]), market, side, token, now)
                            except DATA_ERRORS as exc:
                                errors.append({"market_id": market["id"], "side": side, "error": str(exc)})
                except DATA_ERRORS as exc:
                    errors.append({"market_id": market.get("id"), "error": str(exc)})
    except DATA_ERRORS as exc:
        errors.append({"error": str(exc)})
    return errors


def format_report(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        f"SELECT * FROM {POSITIONS_TABLE} WHERE settled_at IS NOT NULL ORDER BY opened_at, market_id, side"
    ).fetchall()
    open_rows = connection.execute(
        f"SELECT title, side FROM {POSITIONS_TABLE} WHERE settled_at IS NULL"
    ).fetchall()
    open_count = len(open_rows)
    skips = dict(connection.execute(f"SELECT reason, SUM(count) FROM {SKIPS_TABLE} GROUP BY reason"))
    lines = [
        "Price Target NO Book (paper only)",
        "Positions are held to resolution (emergency stop below 0.70 is not simulated in this version).",
        f"Open positions: {open_count}",
        f"Settled positions: {len(rows)}",
        "Skipped counts (side checks, including repeat checks):",
    ]
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
    return "\n".join(lines)
