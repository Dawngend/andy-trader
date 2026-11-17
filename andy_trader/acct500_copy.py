"""Paper-only 500 dollar account built from copy book positions.

It places no trades of its own. Each position the copy book opens after
ACCOUNT_START is resized to 25 dollars of cost, so the account shows what a
small bankroll copying the same wallets would hold.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import sqlite3
from typing import Callable

from andy_trader.copy_book import PAUSED_WALLETS, SHARES, fetch_book

ACCOUNT_NAME = "acct500_copy"
ACCOUNT_START = "2026-11-17T15:45:00+00:00"
STARTING_CASH = 500.0
COST_PER_POSITION = 25.0


@dataclass
class Account:
    cash: float = STARTING_CASH
    open_value: float = 0.0
    positions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def equity(self) -> float:
        return self.cash + self.open_value

    @property
    def open_count(self) -> int:
        return sum(1 for p in self.positions if p["status"] == "open")

    @property
    def settled_count(self) -> int:
        return sum(1 for p in self.positions if p["status"] == "settled")


def initialize_account_tables(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS acct500_copy_equity (
            recorded_at TEXT NOT NULL,
            cash REAL NOT NULL,
            open_value REAL NOT NULL,
            equity REAL NOT NULL,
            open_positions INTEGER NOT NULL,
            settled_positions INTEGER NOT NULL,
            note TEXT NOT NULL
        )
    """)
    connection.commit()


def eligible_positions(connection: sqlite3.Connection) -> list[dict]:
    cursor = connection.execute(
        "SELECT wallet_name, tx_hash, token_id, title, outcome, fill_price, fee, opened_at, settled_at, pnl "
        "FROM copy_book_positions WHERE opened_at >= ? ORDER BY opened_at, tx_hash, token_id",
        (ACCOUNT_START,),
    )
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return [row for row in rows if row["wallet_name"] not in PAUSED_WALLETS]


def best_bid(book: dict) -> float | None:
    prices = [float(level["price"]) for level in book.get("bids") or []]
    return max(prices) if prices else None


def build_account(rows: list[dict], read_book: Callable[[str], dict] = fetch_book) -> Account:
    """Replay the rows in opening order. Same rows always give the same account."""
    account = Account()
    cash = STARTING_CASH
    taken: list[dict] = []
    for row in rows:
        # Positions that settled before this one opened have returned their money.
        for earlier in taken:
            if earlier["status"] == "settled" and not earlier["returned"] and earlier["settled_at"] <= row["opened_at"]:
                cash += earlier["payout"]
                earlier["returned"] = True
        fill_price = float(row["fill_price"])
        shares = COST_PER_POSITION / fill_price
        fee = float(row["fee"]) * shares / SHARES
        if cash < COST_PER_POSITION + fee:
            continue
        cash -= COST_PER_POSITION + fee
        position = {
            "wallet_name": row["wallet_name"], "token_id": row["token_id"], "title": row["title"],
            "shares": shares, "fee": fee, "settled_at": row["settled_at"], "returned": False,
        }
        if row["settled_at"]:
            position["status"] = "settled"
            position["payout"] = COST_PER_POSITION + fee + float(row["pnl"]) * shares / SHARES
            position["value"] = position["payout"]
        else:
            position["status"] = "open"
            position["value"] = COST_PER_POSITION + fee
            try:
                bid = best_bid(read_book(row["token_id"]))
            except Exception as exc:
                bid = None
                account.notes.append(f"book read failed for {row['token_id'][:10]}: {type(exc).__name__}")
            if bid is None:
                account.notes.append(f"no bid for {row['token_id'][:10]}, held at cost")
            else:
                position["value"] = bid * shares
        taken.append(position)
    for position in taken:
        if position["status"] == "settled" and not position["returned"]:
            cash += position["payout"]
    account.cash = cash
    account.open_value = sum(p["value"] for p in taken if p["status"] == "open")
    account.positions = taken
    return account


def record_snapshot(connection: sqlite3.Connection, account: Account, now: datetime | None = None) -> None:
    initialize_account_tables(connection)
    moment = (now or datetime.now(UTC)).isoformat()
    with connection:
        connection.execute(
            "INSERT INTO acct500_copy_equity VALUES (?, ?, ?, ?, ?, ?, ?)",
            (moment, account.cash, account.open_value, account.equity,
             account.open_count, account.settled_count, "; ".join(account.notes)),
        )


def format_account(connection: sqlite3.Connection) -> str:
    initialize_account_tables(connection)
    rows = connection.execute(
        "SELECT recorded_at, cash, open_value, equity, open_positions, settled_positions, note "
        "FROM acct500_copy_equity ORDER BY recorded_at DESC LIMIT 24"
    ).fetchall()
    if not rows:
        return f"{ACCOUNT_NAME}: no snapshots yet"
    latest = rows[0]
    lines = [
        f"{ACCOUNT_NAME} (paper only, started {ACCOUNT_START}, {STARTING_CASH:.2f} dollars)",
        f"Equity: {latest[3]:.2f} | P&L: {latest[3] - STARTING_CASH:+.2f} | Cash: {latest[1]:.2f} | "
        f"Open value: {latest[2]:.2f} | Open: {latest[4]} | Settled: {latest[5]}",
        "Recorded at | Equity | Open | Settled | Note",
    ]
    lines += [f"{row[0]} | {row[3]:.2f} | {row[4]} | {row[5]} | {row[6]}" for row in rows]
    return "\n".join(lines)


if __name__ == "__main__":
    from andy_trader.store import connect, default_database_path

    with connect(default_database_path()) as connection:
        print(format_account(connection))
