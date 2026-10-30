"""Read settled paper positions and describe their loss patterns."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from andy_trader.bond_book import strategy_slice
from andy_trader.env import REPO_ROOT
from andy_trader.loss_rules import learn_rules, load_rules

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


def group_by_end(rows: list[dict]) -> dict[datetime, list[dict]]:
    groups: dict[datetime, list[dict]] = {}
    for row in rows:
        end_val = row.get("ends_at")
        if not end_val:
            continue
        try:
            key = utc_time(end_val).replace(microsecond=0)
        except Exception:
            continue
        groups.setdefault(key, []).append(row)
    return groups


def row_tags(row: dict, end_count: int = 1) -> dict[str, str]:
    title = row.get("title") or "unknown"
    side = row.get("side") or ""
    entry_band = row.get("entry_band") or "unknown"
    words = title.split()
    asset = words[0] if words else "unknown"

    opened_raw = row.get("opened_at")
    ends_raw = row.get("ends_at")

    if opened_raw:
        try:
            opened = utc_time(opened_raw)
            hour_start = opened.hour // 4 * 4
            open_hour = f"{hour_start:02d}-{hour_start + 3:02d}"
        except Exception:
            opened = None
            open_hour = "unknown"
    else:
        opened = None
        open_hour = "unknown"

    if opened and ends_raw:
        try:
            ends = utc_time(ends_raw)
            hours = (ends - opened).total_seconds() / 3600
            time_tag = time_bucket(hours)
        except Exception:
            time_tag = "unknown"
    else:
        time_tag = "unknown"

    if not ends_raw or end_count <= 0:
        same_end = "unknown"
    elif end_count < 3:
        same_end = str(end_count)
    else:
        same_end = "3 or more"

    return {
        "slice": strategy_slice(title, side),
        "entry_band": entry_band,
        "asset": asset,
        "time_to_end": time_tag,
        "open_hour": open_hour,
        "same_end_count": same_end,
    }


def summarize(rows: list[dict]) -> dict:
    settled = [row for row in rows if row.get("pnl") is not None]
    wins = [row["pnl"] for row in settled if row["pnl"] >= 0]
    losses = [row["pnl"] for row in settled if row["pnl"] < 0]
    return {
        "settled": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "loss_rate": len(losses) / len(settled) if settled else 0.0,
        "pnl": sum(row["pnl"] for row in settled),
        "average_loss": sum(losses) / len(losses) if losses else 0.0,
        "average_win": sum(wins) / len(wins) if wins else 0.0,
    }


def tag_summaries(rows: list[dict], ends: dict) -> list[tuple[str, str, dict]]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        end_count = 1
        end_val = row.get("ends_at")
        if end_val:
            try:
                key = utc_time(end_val).replace(microsecond=0)
                end_count = len(ends.get(key, []))
            except Exception:
                end_count = 1
        for tag, value in row_tags(row, end_count).items():
            groups.setdefault((tag, value), []).append(row)
    return [(tag, value, summarize(group)) for (tag, value), group in sorted(groups.items())
            if len(group) >= 20]


def loss_clusters(ends: dict) -> list[tuple[str | datetime, list[dict], dict]]:
    clusters = []
    for end, rows in ends.items():
        stats = summarize(rows)
        if stats["losses"] >= 2:
            clusters.append((end, rows, stats))
    return sorted(clusters, key=lambda cluster: (cluster[2]["pnl"], str(cluster[0])))[:10]


def money(value: float) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def build_report(rows: list[dict], counts: dict[str, int], rules: list[dict] | None = None) -> str:
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
    suggested_rules = []
    for tag, value, stats in tag_summaries(rows, ends):
        flagged = stats["loss_rate"] > overall["loss_rate"] and stats["pnl"] < 0
        lines.append(
            f"| {tag} | {cell(value)} | {stats['settled']} | {stats['losses']} | "
            f"{stats['loss_rate']:.1%} | {money(stats['pnl'])} | {'FLAGGED' if flagged else ''} |"
        )
        if flagged:
            suggested_rules.append(
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
        titles = "; ".join(cell(row.get("title") or "unknown") for row in group)
        end_str = end.isoformat() if isinstance(end, datetime) else str(end)
        lines.append(f"| {cell(end_str)} | {titles} | {stats['settled']} | {stats['losses']} | {money(stats['pnl'])} |")
    if not clusters:
        lines.extend(["", "No clusters with two or more losses."])
    lines.extend(["", "## Suggested rules (not applied)", "",
                  "In-sample hindsight: a forward paper test is needed before any rule is trusted.", ""])
    lines.extend(suggested_rules or ["No tag stands out yet."])

    active_rules = load_rules() if rules is None else rules
    lines.extend(["", "## Rules now active", ""])
    if active_rules:
        lines.extend(f"- Skip {r['tag']} {cell(r['value'])}: {r['reason']}" for r in active_rules)
    else:
        lines.append("No rules active.")

    return "\n".join(lines) + "\n"
