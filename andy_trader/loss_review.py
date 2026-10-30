"""Read settled paper positions and describe their loss patterns."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from andy_trader.bond_book import strategy_slice
from andy_trader.env import REPO_ROOT

DEFAULT_OUTPUT_PATH = REPO_ROOT / "research" / "loss_review.md"
TABLES = ("bond_book_positions", "price_target_book_positions")


def read_positions(database_path: Path) -> tuple[list[dict], dict[str, int]]:
    if not database_path.is_file():
        raise FileNotFoundError(f"Database file not found: {database_path}")
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = []
    counts = dict.fromkeys(TABLES, 0)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in TABLES:
            if table not in tables:
                continue
            positions = connection.execute(f"""
                SELECT market_id, title, side, entry_band, fill_price, shares,
                       opened_at, ends_at, win, pnl
                FROM {table} WHERE pnl IS NOT NULL
            """).fetchall()
            counts[table] = len(positions)
            rows.extend(dict(row) for row in positions)
    finally:
        connection.close()
    return rows, counts


def utc_time(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def time_bucket(hours: float) -> str:
    if hours < 1:
        return "under 1h"
    if hours <= 6:
        return "1h to 6h"
    if hours <= 24:
        return "6h to 24h"
    return "over 24h"


def group_by_end(rows: list[dict]) -> dict[str, list[dict]]:
    groups = {}
    for row in rows:
        groups.setdefault(row["ends_at"], []).append(row)
    return groups


def row_tags(row: dict, end_count: int) -> dict[str, str]:
    opened = utc_time(row["opened_at"])
    hours = (utc_time(row["ends_at"]) - opened).total_seconds() / 3600
    hour_start = opened.hour // 4 * 4
    words = row["title"].split()
    return {
        "slice": strategy_slice(row["title"], row["side"]),
        "entry_band": row["entry_band"],
        "asset": words[0] if words else "",
        "time_to_end": time_bucket(hours),
        "open_hour": f"{hour_start:02d}-{hour_start + 3:02d}",
        "same_end_count": str(end_count) if end_count < 3 else "3 or more",
    }


def summarize(rows: list[dict]) -> dict:
    wins = [row["pnl"] for row in rows if row["win"]]
    losses = [row["pnl"] for row in rows if not row["win"]]
    return {
        "settled": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "loss_rate": len(losses) / len(rows) if rows else 0.0,
        "pnl": sum(row["pnl"] for row in rows),
        "average_loss": sum(losses) / len(losses) if losses else 0.0,
        "average_win": sum(wins) / len(wins) if wins else 0.0,
    }


def tag_summaries(rows: list[dict], ends: dict) -> list[tuple[str, str, dict]]:
    groups = {}
    for row in rows:
        for tag, value in row_tags(row, len(ends[row["ends_at"]])).items():
            groups.setdefault((tag, value), []).append(row)
    return [(tag, value, summarize(group)) for (tag, value), group in sorted(groups.items())
            if len(group) >= 20]


def loss_clusters(ends: dict) -> list[tuple[str, list[dict], dict]]:
    clusters = []
    for end, rows in ends.items():
        stats = summarize(rows)
        if stats["losses"] >= 2:
            clusters.append((end, rows, stats))
    return sorted(clusters, key=lambda cluster: (cluster[2]["pnl"], cluster[0]))[:10]


def money(value: float) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def build_report(rows: list[dict], counts: dict[str, int]) -> str:
    overall = summarize(rows)
    ends = group_by_end(rows)
    lines = [
        "# Loss Review Report", "",
        f"Run time: {datetime.now(UTC).isoformat()} | Settled rows: {len(rows)} | "
        + " | ".join(f"{table}: {count}" for table, count in counts.items()),
        "", "Paper money only. Suggested rules are not applied.", "",
        "## Overall", "",
        "| Settled | Wins | Losses | Loss rate | Total pnl | Average loss | Average win |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        f"| {overall['settled']} | {overall['wins']} | {overall['losses']} | "
        f"{overall['loss_rate']:.1%} | {money(overall['pnl'])} | "
        f"{money(overall['average_loss'])} | {money(overall['average_win'])} |",
        "", "## By tag", "", "Only values with at least 20 settled rows are listed.", "",
        "| Tag | Value | Settled | Losses | Loss rate | Total pnl | Status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    rules = []
    for tag, value, stats in tag_summaries(rows, ends):
        flagged = stats["loss_rate"] > overall["loss_rate"] and stats["pnl"] < 0
        lines.append(
            f"| {tag} | {cell(value)} | {stats['settled']} | {stats['losses']} | "
            f"{stats['loss_rate']:.1%} | {money(stats['pnl'])} | {'FLAGGED' if flagged else ''} |"
        )
        if flagged:
            rules.append(
                f"Skip {tag} {cell(value)}: {stats['settled']} settled, loss rate "
                f"{stats['loss_rate'] * 100:.1f} percent vs {overall['loss_rate'] * 100:.1f} "
                f"percent overall, pnl {money(stats['pnl'])} if kept, would have been $0 if skipped."
            )
    lines.extend(["", "## Clusters", "",
                  "Combined pnl and count include all settled rows sharing the stored ends_at.", "",
                  "| Ends at | Titles | Count | Losses | Combined pnl |",
                  "| --- | --- | --- | --- | --- |"])
    clusters = loss_clusters(ends)
    for end, group, stats in clusters:
        titles = "; ".join(cell(row["title"]) for row in group)
        lines.append(f"| {cell(end)} | {titles} | {stats['settled']} | {stats['losses']} | {money(stats['pnl'])} |")
    if not clusters:
        lines.extend(["", "No clusters with two or more losses."])
    lines.extend(["", "## Suggested rules (not applied)", "",
                  "In-sample hindsight: a forward paper test is needed before any rule is trusted.", ""])
    lines.extend(rules or ["No tag stands out yet."])
    return "\n".join(lines) + "\n"
