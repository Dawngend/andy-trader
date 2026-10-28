"""Simulated copy book for top Polymarket accounts (paper only)."""
from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
import statistics
from typing import Callable
from urllib.parse import urlencode

from andy_trader import bond_book
from andy_trader.bond_book import (
    CLOB_BOOK_URL, CRYPTO_TAKER_FEE_RATE, DATA_ERRORS,
    SLICES, SLICE_HEADER, TIMEOUT_SECONDS, average,
    compute_bootstrap_interval, report_row, settled_winner,
    strategy_slice, walk_ask_book,
)
from andy_trader.wallet_scout import PublicReader

WALLETS = {
    "sharky6999": "0x751a2b86cab503496efd325c8344e10159349ea1",
    "dromedaryy": "0x6575759174ad221bba9947bc3f18750ec769b198",
}

SHARES = 100
MIN_FILL = 0.05
MAX_FILL = 0.98
MAX_OPEN = 60
VERDICT_THRESHOLD = 100
SKIP_REASONS = ("thin_book", "fill_too_low", "fill_too_high", "max_open")
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

BOOK_READS = 0
REQUEST_COUNT = 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _http_json(url: str, timeout: int):
    return bond_book._http_json(url, timeout)


def initialize_copy_book(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_book_positions (
            wallet_name TEXT NOT NULL,
            wallet TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            token_id TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            title TEXT NOT NULL,
            outcome TEXT NOT NULL,
            their_price REAL NOT NULL,
            their_timestamp INTEGER NOT NULL,
            fill_price REAL NOT NULL,
            fee REAL NOT NULL,
            shares REAL NOT NULL,
            opened_at TEXT NOT NULL,
            delay_seconds REAL NOT NULL,
            settled_at TEXT,
            win INTEGER,
            pnl REAL,
            UNIQUE (wallet, tx_hash, token_id)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_book_skips (
            day TEXT NOT NULL,
            wallet_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (day, wallet_name, reason)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_book_cursor (
            wallet_name TEXT PRIMARY KEY,
            newest_timestamp INTEGER NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS copy_book_seen (
            wallet_name TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            token_id TEXT NOT NULL,
            PRIMARY KEY (wallet_name, tx_hash, token_id)
        )
    """)
    connection.commit()


def record_skip(connection: sqlite3.Connection, now: datetime, wallet_name: str, reason: str) -> None:
    with connection:
        connection.execute("""
            INSERT INTO copy_book_skips (day, wallet_name, reason, count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(day, wallet_name, reason) DO UPDATE SET count = count + 1
        """, (now.date().isoformat(), wallet_name, reason))


def fetch_book(token_id: str) -> dict:
    global BOOK_READS, REQUEST_COUNT
    BOOK_READS += 1
    REQUEST_COUNT += 1
    return _http_json(f"{CLOB_BOOK_URL}?{urlencode({'token_id': token_id})}", TIMEOUT_SECONDS)


def fetch_markets(condition_id: str) -> list[dict]:
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    result = _http_json(f"{GAMMA_MARKETS_URL}?{urlencode({'condition_ids': condition_id})}", TIMEOUT_SECONDS)
    if not isinstance(result, list):
        raise ValueError("Expected a list of Gamma markets")
    return result


def settle_positions(connection: sqlite3.Connection, now: datetime, errors: list) -> None:
    cursor = connection.execute(
        "SELECT DISTINCT condition_id FROM copy_book_positions WHERE settled_at IS NULL"
    )
    rows = cursor.fetchall()
    cids = [r[0] if isinstance(r, (list, tuple)) else r["condition_id"] for r in rows]
    for cid in cids:
        try:
            markets = fetch_markets(cid)
        except DATA_ERRORS as exc:
            errors.append({"condition_id": cid, "error": str(exc)})
            continue
        for market in markets:
            if market.get("conditionId") != cid and market.get("condition_id") != cid:
                continue
            try:
                winner = settled_winner(market)
                if winner is None:
                    continue
                with connection:
                    connection.execute("""
                        UPDATE copy_book_positions
                        SET win = (outcome = ?), settled_at = ?,
                            pnl = shares * ((outcome = ?) - fill_price) - fee
                        WHERE condition_id = ? AND settled_at IS NULL
                    """, (winner, now.isoformat(), winner, cid))
            except DATA_ERRORS as exc:
                errors.append({"condition_id": cid, "error": str(exc)})


def mark_seen(connection: sqlite3.Connection, wallet_name: str, tx_hash: str, token_id: str) -> None:
    with connection:
        connection.execute(
            "INSERT OR IGNORE INTO copy_book_seen (wallet_name, tx_hash, token_id) VALUES (?, ?, ?)",
            (wallet_name, tx_hash, token_id),
        )


def copy_wallet_trades(
    connection: sqlite3.Connection,
    wallet_name: str,
    address: str,
    now: datetime | None,
    errors: list,
    reader: PublicReader | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    global REQUEST_COUNT
    reader = reader or PublicReader()
    REQUEST_COUNT += 1
    activity = reader.get("activity", user=address, limit=500)
    if not isinstance(activity, list):
        return

    start = connection.execute(
        "SELECT newest_timestamp FROM copy_book_cursor WHERE wallet_name = ?", (wallet_name,),
    ).fetchone()
    if start is None:
        # First pass: remember the newest trade as the starting point and copy nothing.
        timestamps = [int(row["timestamp"]) for row in activity if row.get("timestamp") is not None]
        if timestamps:
            with connection:
                connection.execute(
                    "INSERT INTO copy_book_cursor (wallet_name, newest_timestamp) VALUES (?, ?)",
                    (wallet_name, max(timestamps)),
                )
        return
    starting_timestamp = start[0]

    seen = {
        (row[0], row[1])
        for row in connection.execute(
            "SELECT tx_hash, token_id FROM copy_book_seen WHERE wallet_name = ?", (wallet_name,),
        )
    }
    new_trades = []
    for row in activity:
        if row.get("type") != "TRADE" or row.get("side") != "BUY":
            continue
        if int(row.get("timestamp") or 0) <= starting_timestamp:
            continue
        key = (str(row.get("transactionHash", "")), str(row.get("asset", "")))
        if all(key) and key not in seen:
            new_trades.append((key, row))
    new_trades.sort(key=lambda item: int(item[1]["timestamp"]))

    # A trade is marked seen once it was copied or skipped for a counted reason. A failed
    # order book read is not marked, so the trade is tried again on the next pass.
    for (tx_hash, token_id), trade in new_trades:
        if (tx_hash, token_id) in seen:
            continue
        seen.add((tx_hash, token_id))

        open_count = connection.execute(
            "SELECT COUNT(*) FROM copy_book_positions WHERE wallet_name = ? AND settled_at IS NULL",
            (wallet_name,),
        ).fetchone()[0]
        if open_count >= MAX_OPEN:
            record_skip(connection, now or _utc_now(), wallet_name, "max_open")
            mark_seen(connection, wallet_name, tx_hash, token_id)
            continue

        try:
            book = fetch_book(token_id)
        except DATA_ERRORS as exc:
            errors.append({"wallet_name": wallet_name, "token_id": token_id, "error": str(exc)})
            seen.discard((tx_hash, token_id))
            continue

        fill_time = clock() if clock is not None else (_utc_now() if now is None else now)

        fill = walk_ask_book(book, SHARES, fee_rate=CRYPTO_TAKER_FEE_RATE)
        if not fill.complete:
            record_skip(connection, fill_time, wallet_name, "thin_book")
            mark_seen(connection, wallet_name, tx_hash, token_id)
            continue

        fill_price = fill.fill_cost / SHARES
        if fill_price < MIN_FILL:
            record_skip(connection, fill_time, wallet_name, "fill_too_low")
            mark_seen(connection, wallet_name, tx_hash, token_id)
            continue
        if fill_price >= MAX_FILL:
            record_skip(connection, fill_time, wallet_name, "fill_too_high")
            mark_seen(connection, wallet_name, tx_hash, token_id)
            continue

        their_timestamp = int(trade["timestamp"])
        with connection:
            connection.execute("""
                INSERT OR IGNORE INTO copy_book_positions (
                    wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                    their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                wallet_name, address, tx_hash, token_id, str(trade.get("conditionId", "")),
                str(trade.get("title", "")), str(trade.get("outcome", "")),
                float(trade["price"]), their_timestamp, fill_price, fill.fee_cost, SHARES,
                fill_time.isoformat(), float(fill_time.timestamp() - their_timestamp),
            ))
        mark_seen(connection, wallet_name, tx_hash, token_id)


def run_once(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    reader: PublicReader | None = None,
) -> list[dict]:
    global BOOK_READS, REQUEST_COUNT
    BOOK_READS = 0
    REQUEST_COUNT = 0
    initialize_copy_book(connection)
    pass_now = now or _utc_now()
    pass_now = pass_now.replace(tzinfo=UTC) if pass_now.tzinfo is None else pass_now.astimezone(UTC)
    errors: list[dict] = []
    settle_positions(connection, pass_now, errors)
    for wallet_name, address in WALLETS.items():
        try:
            copy_wallet_trades(
                connection, wallet_name, address, now, errors,
                reader=reader, clock=clock,
            )
        except DATA_ERRORS as exc:
            errors.append({"wallet_name": wallet_name, "error": str(exc)})
    return errors


def _rows_to_dicts(cursor) -> list[dict]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if hasattr(rows[0], "keys"):
        return [dict(r) for r in rows]
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


def format_report(connection: sqlite3.Connection) -> str:
    lines = [
        "Copy Book (paper only)",
        "Note: Uses crypto taker fee rate (0.07); other categories may have a lower fee, so results are conservative.",
    ]
    wallet_names = list(WALLETS.keys())
    extra_wallets = connection.execute(
        "SELECT DISTINCT wallet_name FROM copy_book_positions ORDER BY wallet_name"
    ).fetchall()
    for row in extra_wallets:
        w = row[0] if isinstance(row, (list, tuple)) else row["wallet_name"]
        if w not in wallet_names:
            wallet_names.append(w)

    for wallet_name in wallet_names:
        settled_rows = _rows_to_dicts(connection.execute(
            "SELECT * FROM copy_book_positions WHERE wallet_name = ? AND settled_at IS NOT NULL ORDER BY opened_at",
            (wallet_name,),
        ))
        open_rows = _rows_to_dicts(connection.execute(
            "SELECT * FROM copy_book_positions WHERE wallet_name = ? AND settled_at IS NULL",
            (wallet_name,),
        ))
        all_wallet_rows = _rows_to_dicts(connection.execute(
            "SELECT * FROM copy_book_positions WHERE wallet_name = ?",
            (wallet_name,),
        ))
        skips = dict(connection.execute(
            "SELECT reason, SUM(count) FROM copy_book_skips WHERE wallet_name = ? GROUP BY reason",
            (wallet_name,),
        ).fetchall())

        lines.extend([
            "",
            f"Wallet: {wallet_name}",
            f"Open positions: {len(open_rows)}",
            f"Settled positions: {len(settled_rows)}",
            "Skipped counts:",
        ])
        for reason in SKIP_REASONS:
            lines.append(f"  {reason}: {skips.get(reason, 0)}")

        lines.append(SLICE_HEADER)
        lines.append(report_row(wallet_name, settled_rows))

        losses = [float(r["pnl"]) for r in settled_rows if not r["win"]]
        lines.append(f"Worst loss: ${min(losses, default=0):.2f}")

        delays = [float(r["delay_seconds"]) for r in all_wallet_rows if r.get("delay_seconds") is not None]
        median_delay = f"{statistics.median(delays):.1f}s" if delays else "n/a"
        lines.append(f"Median delay_seconds: {median_delay}")

        price_diffs = [
            float(r["fill_price"]) - float(r["their_price"])
            for r in all_wallet_rows
            if r.get("fill_price") is not None and r.get("their_price") is not None
        ]
        avg_diff = f"{average(price_diffs):+.4f}" if price_diffs else "n/a"
        lines.append(f"Average price difference (fill - their): {avg_diff}")

        low, high = compute_bootstrap_interval([float(r["pnl"]) for r in settled_rows])
        interval = f"[${low:+.2f}, ${high:+.2f}]" if settled_rows else "n/a"
        lines.append(f"Bootstrap 95 percent interval for average pnl per position: {interval}")

        if len(settled_rows) < VERDICT_THRESHOLD:
            lines.append(f"Verdict: not enough settled positions yet ({len(settled_rows)} of {VERDICT_THRESHOLD})")
        else:
            verdict = "positive edge shown" if low > 0 else "no edge shown"
            lines.append(f"Verdict: {verdict} ({len(settled_rows)} settled, including {len(losses)} losses)")

    all_settled = _rows_to_dicts(connection.execute(
        "SELECT * FROM copy_book_positions WHERE settled_at IS NOT NULL ORDER BY opened_at"
    ))

    by_slice: dict[str, list[dict]] = {}
    for r in all_settled:
        s = strategy_slice(r["title"], r["outcome"])
        by_slice.setdefault(s, []).append(r)

    lines.extend([
        "",
        "By strategy slice",
        SLICE_HEADER,
    ])
    slice_names = sorted(by_slice, key=lambda n: (-len(by_slice[n]), SLICES.index(n) if n in SLICES else 99))
    if not slice_names:
        slice_names = list(SLICES)
    for name in slice_names:
        slice_rows = by_slice.get(name, [])
        lines.append(report_row(name, slice_rows))

    return "\n".join(lines)
