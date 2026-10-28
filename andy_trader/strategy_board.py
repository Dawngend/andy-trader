"""Weekly Strategy Board for comparing Polymarket paper books."""
from __future__ import annotations

import sqlite3

from andy_trader.bond_book import (
    SLICES,
    compute_bootstrap_interval,
    strategy_slice,
)

HEADER = (
    "Strategy | Settled | Open | Wins | Win rate | Average entry price | "
    "Average pnl per position after fees | "
    "95 percent bootstrap interval of the average pnl per position | Status"
)
SEPARATOR = "---|---|---|---|---|---|---|---|---"
VERDICT_THRESHOLD = 100


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _fetch_positions(connection: sqlite3.Connection, table_name: str) -> tuple[list[dict], list[dict]]:
    if not _table_exists(connection, table_name):
        return [], []
    cursor = connection.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()
    col_names = [d[0] for d in cursor.description]
    dict_rows = [
        dict(row) if hasattr(row, "keys") else dict(zip(col_names, row))
        for row in rows
    ]
    settled = [r for r in dict_rows if r.get("settled_at") is not None]
    open_pos = [r for r in dict_rows if r.get("settled_at") is None]
    return settled, open_pos


def _strategy_stats(
    name: str,
    settled_rows: list[dict],
    open_rows: list[dict],
) -> tuple[str, bool]:
    settled_count = len(settled_rows)
    open_count = len(open_rows)
    wins = sum(int(bool(r.get("win"))) for r in settled_rows)

    if settled_count > 0:
        win_rate_str = f"{wins / settled_count:.2%}"
        avg_entry = sum(float(r["fill_price"]) for r in settled_rows) / settled_count
        avg_entry_str = f"{avg_entry:.4f}"
        pnls = [float(r["pnl"]) for r in settled_rows]
        avg_pnl = sum(pnls) / settled_count
        avg_pnl_str = f"${avg_pnl:+.2f}"
        low, high = compute_bootstrap_interval(pnls)
        ci_str = f"[${low:+.2f}, ${high:+.2f}]"
    else:
        win_rate_str = "n/a"
        avg_entry_str = "n/a"
        avg_pnl_str = "n/a"
        ci_str = "n/a"
        low, high = 0.0, 0.0

    if settled_count < VERDICT_THRESHOLD:
        status = "collecting"
    elif low > 0:
        status = "positive"
    elif high < 0:
        status = "negative"
    else:
        status = "unclear"

    row_line = (
        f"{name} | {settled_count} | {open_count} | {wins} | {win_rate_str} | "
        f"{avg_entry_str} | {avg_pnl_str} | {ci_str} | {status}"
    )

    losses = [float(r["pnl"]) for r in settled_rows if not r.get("win")]
    win_pnls = [float(r["pnl"]) for r in settled_rows if r.get("win")]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    worst_loss_mag = abs(min(losses)) if losses else 0.0

    worst_loss_trigger = (worst_loss_mag > 10 * avg_win) if losses else False
    needs_review = (status == "negative") or worst_loss_trigger

    return row_line, needs_review


def format_board(connection: sqlite3.Connection) -> str:
    rows_to_display: list[str] = []
    reviews: list[str] = []

    # 1. Bond book overall
    if _table_exists(connection, "bond_book_positions"):
        settled_bond, open_bond = _fetch_positions(connection, "bond_book_positions")
        row_str, review = _strategy_stats("bond_book", settled_bond, open_bond)
        rows_to_display.append(row_str)
        if review:
            reviews.append("review: bond_book")

        # 2. Each bond book slice that has at least one settled position
        by_slice_settled: dict[str, list[dict]] = {}
        for r in settled_bond:
            s = strategy_slice(r["title"], r["side"])
            by_slice_settled.setdefault(s, []).append(r)

        by_slice_open: dict[str, list[dict]] = {}
        for r in open_bond:
            s = strategy_slice(r["title"], r["side"])
            by_slice_open.setdefault(s, []).append(r)

        active_slices = [s for s in by_slice_settled if len(by_slice_settled[s]) > 0]
        active_slices.sort(
            key=lambda s: (-len(by_slice_settled[s]), SLICES.index(s) if s in SLICES else 99)
        )

        for s in active_slices:
            slice_settled = by_slice_settled[s]
            slice_open = by_slice_open.get(s, [])
            row_str, review = _strategy_stats(s, slice_settled, slice_open)
            rows_to_display.append(row_str)
            if review:
                reviews.append(f"review: {s}")

    # 3. Price target NO book (skip without error if table does not exist)
    if _table_exists(connection, "price_target_book_positions"):
        settled_pt, open_pt = _fetch_positions(connection, "price_target_book_positions")
        row_str, review = _strategy_stats("price_target_no_book", settled_pt, open_pt)
        rows_to_display.append(row_str)
        if review:
            reviews.append("review: price_target_no_book")

    lines = [HEADER, SEPARATOR, *rows_to_display]
    if reviews:
        lines.append("")
        lines.extend(reviews)
    return "\n".join(lines)
