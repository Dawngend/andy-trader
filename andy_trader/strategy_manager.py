"""Strategy Manager for Polymarket paper books.

Scores each paper trading strategy on a schedule, maintains the live strategy
list, and logs every state change to an append-only journal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Callable, Sequence

# Note: compute_bootstrap_interval, strategy_slice, and SLICES are imported
# through bond_book on purpose to match strategy_board.py.
# Note: compute_bootstrap_interval returns (0.0, 0.0) for an empty list. An empty
# list never promotes or demotes because settled-count minimums are checked first.
from andy_trader.bond_book import (
    SLICES,
    compute_bootstrap_interval,
    strategy_slice,
)
from andy_trader.env import REPO_ROOT
from andy_trader.store import default_database_path

DEFAULT_CONFIG_PATH = REPO_ROOT / "strategy_manager_config.json"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "research" / "strategy_manager.md"
DEFAULT_TIMEOUT = 5.0

# Proposals that the operator can edit in strategy_manager_config.json.
DEFAULT_CONFIG = {
    "promote_min_settled": 100,
    "demote_min_settled": 50,
    "demote_window_days": 30,
}

# The three book sources and their grouping functions.
# A new book is added by adding a line here.
# Strategy keys come from the data inside each source; a slice/key with no
# positions (open or settled) has no key yet.
SOURCES = (
    ("bond_book_positions", lambda r: f"bond:{strategy_slice(r['title'], r['side'])}"),
    ("price_target_book_positions", lambda r: "price_target"),
    ("copy_book_positions", lambda r: f"copy:{r['wallet_name']}" if r.get("wallet_name") else None),
)


@dataclass(frozen=True)
class StrategyDecision:
    strategy_key: str
    current_status: str | None
    new_status: str
    action: str | None  # "new", "promote", "demote", or None (hold)
    settled: int
    avg_pnl: float | None
    interval_low: float | None
    interval_high: float | None
    reason: str
    is_change: bool


def is_valid_config_int(val: object) -> bool:
    """A value is valid only when it is a JSON integer, not a boolean, and is at least 1."""
    return type(val) is int and not isinstance(val, bool) and val >= 1


def load_config(config_path: Path, dry_run: bool = False) -> tuple[dict[str, int], list[str]]:
    """Load configuration from config_path, validating keys and falling back to defaults.

    In normal runs, writes the defaults if the file is missing. In dry_run, uses
    the defaults in memory and does not write the file.
    Raises ValueError on malformed JSON or if the JSON root is not an object.
    """
    notes: list[str] = []
    if not config_path.is_file():
        if dry_run:
            notes.append("Config file missing; using default proposals in memory.")
            return dict(DEFAULT_CONFIG), notes
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        return dict(DEFAULT_CONFIG), notes

    content = config_path.read_text(encoding="utf-8")
    try:
        raw_config = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON in config file {config_path}: {exc}") from exc

    if not isinstance(raw_config, dict):
        raise ValueError(f"Invalid config in {config_path}: expected JSON object")

    config: dict[str, int] = {}
    for key, default_val in DEFAULT_CONFIG.items():
        val = raw_config.get(key)
        if is_valid_config_int(val):
            config[key] = val
        else:
            config[key] = default_val
            notes.append(
                f"Config key '{key}' is invalid or missing ({val!r}); using default proposal {default_val}."
            )

    return config, notes


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def parse_timestamp(val: object) -> datetime | None:
    """Parse an ISO format timestamp string. Naive timestamps are treated as UTC."""
    if not val or not isinstance(val, str):
        return None
    try:
        dt = datetime.fromisoformat(val.strip())
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def read_positions(
    connection: sqlite3.Connection,
) -> tuple[dict[str, list[tuple[datetime, float]]], set[str], int]:
    """Read positions from all book sources.

    Returns:
      - positions_by_strategy: mapping strategy_key -> list of (settled_dt, pnl)
      - discovered_keys: set of strategy keys with at least one position (open or settled)
      - ignored_count: count of settled positions with unparseable/empty settled_at or invalid/null pnl
    """
    positions_by_strategy: dict[str, list[tuple[datetime, float]]] = {}
    discovered_keys: set[str] = set()
    ignored_count = 0

    for table_name, group_fn in SOURCES:
        if not table_exists(connection, table_name):
            continue
        cursor = connection.execute(f"SELECT * FROM {table_name}")
        rows = cursor.fetchall()
        col_names = [d[0] for d in cursor.description]
        for row in rows:
            r = dict(row) if hasattr(row, "keys") else dict(zip(col_names, row))
            key = group_fn(r)
            if not key:
                continue
            discovered_keys.add(key)
            settled_raw = r.get("settled_at")
            if settled_raw is None:
                # Open position
                continue
            if not isinstance(settled_raw, str) or not settled_raw.strip():
                ignored_count += 1
                continue
            settled_dt = parse_timestamp(settled_raw)
            if settled_dt is None:
                ignored_count += 1
                continue
            pnl_raw = r.get("pnl")
            if pnl_raw is None:
                ignored_count += 1
                continue
            try:
                pnl = float(pnl_raw)
            except (ValueError, TypeError):
                ignored_count += 1
                continue
            positions_by_strategy.setdefault(key, []).append((settled_dt, pnl))

    return positions_by_strategy, discovered_keys, ignored_count


def initialize_manager_tables(connection: sqlite3.Connection) -> None:
    """Create live_strategy_list and strategy_journal inside the active transaction."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS live_strategy_list (
            strategy_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            since TEXT NOT NULL,
            reason TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS strategy_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            strategy_key TEXT NOT NULL,
            action TEXT NOT NULL,
            settled INTEGER NOT NULL,
            avg_pnl REAL,
            interval_low REAL,
            interval_high REAL,
            reason TEXT NOT NULL
        )
    """)


def read_live_strategies(connection: sqlite3.Connection) -> dict[str, dict]:
    """Read current live_strategy_list rows into a dict keyed by strategy_key."""
    if not table_exists(connection, "live_strategy_list"):
        return {}
    cursor = connection.execute(
        "SELECT strategy_key, status, since, reason FROM live_strategy_list"
    )
    rows = cursor.fetchall()
    col_names = [d[0] for d in cursor.description]
    return {
        r["strategy_key"]: r
        for r in (
            dict(row) if hasattr(row, "keys") else dict(zip(col_names, row))
            for row in rows
        )
    }


def read_journal_entries(connection: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Read recent journal entries, newest first."""
    if not table_exists(connection, "strategy_journal"):
        return []
    cursor = connection.execute(
        """
        SELECT id, recorded_at, strategy_key, action, settled, avg_pnl, interval_low, interval_high, reason
        FROM strategy_journal
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    col_names = [d[0] for d in cursor.description]
    return [
        dict(row) if hasattr(row, "keys") else dict(zip(col_names, row))
        for row in rows
    ]


def read_recent_changes(connection: sqlite3.Connection, since_dt: datetime) -> list[dict]:
    """Read journal changes recorded strictly after since_dt."""
    if not table_exists(connection, "strategy_journal"):
        return []
    cursor = connection.execute(
        """
        SELECT id, recorded_at, strategy_key, action, settled, avg_pnl, interval_low, interval_high, reason
        FROM strategy_journal
        ORDER BY id DESC
        """
    )
    rows = cursor.fetchall()
    col_names = [d[0] for d in cursor.description]
    dict_rows = [
        dict(row) if hasattr(row, "keys") else dict(zip(col_names, row))
        for row in rows
    ]
    recent = []
    for r in dict_rows:
        dt = parse_timestamp(r["recorded_at"])
        if dt is not None and dt > since_dt:
            recent.append(r)
    return recent


def evaluate_strategy(
    key: str,
    existing: dict | None,
    positions: list[tuple[datetime, float]],
    now: datetime,
    promote_min_settled: int,
    demote_min_settled: int,
    demote_window_days: int,
) -> StrategyDecision:
    """Evaluate scoring and state transitions for a single strategy."""
    lifetime_pnls = [pnl for _, pnl in positions]
    lifetime_count = len(lifetime_pnls)
    lifetime_avg = sum(lifetime_pnls) / lifetime_count if lifetime_count > 0 else 0.0
    lifetime_low, lifetime_high = (
        compute_bootstrap_interval(lifetime_pnls) if lifetime_count > 0 else (0.0, 0.0)
    )

    one_day_ago = now - timedelta(hours=24)

    # First appearance: seen for the first time
    if existing is None:
        if lifetime_count >= promote_min_settled and lifetime_low > 0:
            return StrategyDecision(
                strategy_key=key,
                current_status=None,
                new_status="live",
                action="promote",
                settled=lifetime_count,
                avg_pnl=lifetime_avg,
                interval_low=lifetime_low,
                interval_high=lifetime_high,
                reason=(
                    f"Promoted: {lifetime_count} settled positions, avg pnl "
                    f"${lifetime_avg:+.4f}, 95% bootstrap CI [${lifetime_low:+.4f}, ${lifetime_high:+.4f}]"
                ),
                is_change=True,
            )
        else:
            return StrategyDecision(
                strategy_key=key,
                current_status=None,
                new_status="candidate",
                action="new",
                settled=lifetime_count,
                avg_pnl=lifetime_avg if lifetime_count > 0 else None,
                interval_low=lifetime_low if lifetime_count > 0 else None,
                interval_high=lifetime_high if lifetime_count > 0 else None,
                reason=(
                    f"New candidate: {lifetime_count} settled positions, avg pnl "
                    f"${lifetime_avg:+.4f}, 95% bootstrap CI [${lifetime_low:+.4f}, ${lifetime_high:+.4f}]"
                    if lifetime_count > 0
                    else "New candidate: 0 settled positions"
                ),
                is_change=True,
            )

    current_status = existing["status"]
    stored_since_dt = parse_timestamp(existing.get("since"))

    # Candidate: promotion is never blocked by the 24-hour rule
    if current_status == "candidate":
        if lifetime_count >= promote_min_settled and lifetime_low > 0:
            return StrategyDecision(
                strategy_key=key,
                current_status="candidate",
                new_status="live",
                action="promote",
                settled=lifetime_count,
                avg_pnl=lifetime_avg,
                interval_low=lifetime_low,
                interval_high=lifetime_high,
                reason=(
                    f"Promoted: {lifetime_count} settled positions, avg pnl "
                    f"${lifetime_avg:+.4f}, 95% bootstrap CI [${lifetime_low:+.4f}, ${lifetime_high:+.4f}]"
                ),
                is_change=True,
            )
        return StrategyDecision(
            strategy_key=key,
            current_status="candidate",
            new_status="candidate",
            action=None,
            settled=lifetime_count,
            avg_pnl=lifetime_avg if lifetime_count > 0 else None,
            interval_low=lifetime_low if lifetime_count > 0 else None,
            interval_high=lifetime_high if lifetime_count > 0 else None,
            reason=existing["reason"],
            is_change=False,
        )

    # Live strategy
    if current_status == "live":
        # Blocked when stored time of last change is later than now minus 24 hours
        if stored_since_dt is not None and stored_since_dt > one_day_ago:
            return StrategyDecision(
                strategy_key=key,
                current_status="live",
                new_status="live",
                action=None,
                settled=lifetime_count,
                avg_pnl=lifetime_avg if lifetime_count > 0 else None,
                interval_low=lifetime_low if lifetime_count > 0 else None,
                interval_high=lifetime_high if lifetime_count > 0 else None,
                reason=existing["reason"],
                is_change=False,
            )

        # 30-day window: positions with settled_at strictly later than now minus 30 days
        cutoff_30d = now - timedelta(days=demote_window_days)
        window_pnls = [pnl for dt, pnl in positions if dt > cutoff_30d]
        window_count = len(window_pnls)
        if window_count >= demote_min_settled:
            window_avg = sum(window_pnls) / window_count
            window_low, window_high = compute_bootstrap_interval(window_pnls)
            if window_high < 0:
                return StrategyDecision(
                    strategy_key=key,
                    current_status="live",
                    new_status="removed",
                    action="demote",
                    settled=window_count,
                    avg_pnl=window_avg,
                    interval_low=window_low,
                    interval_high=window_high,
                    reason=(
                        f"Demoted: {window_count} settled positions in last {demote_window_days}d, "
                        f"avg pnl ${window_avg:+.4f}, 95% bootstrap CI [${window_low:+.4f}, ${window_high:+.4f}]"
                    ),
                    is_change=True,
                )

        return StrategyDecision(
            strategy_key=key,
            current_status="live",
            new_status="live",
            action=None,
            settled=lifetime_count,
            avg_pnl=lifetime_avg if lifetime_count > 0 else None,
            interval_low=lifetime_low if lifetime_count > 0 else None,
            interval_high=lifetime_high if lifetime_count > 0 else None,
            reason=existing["reason"],
            is_change=False,
        )

    # Removed strategy
    if current_status == "removed":
        # Blocked when stored time of last change is later than now minus 24 hours
        if stored_since_dt is not None and stored_since_dt > one_day_ago:
            return StrategyDecision(
                strategy_key=key,
                current_status="removed",
                new_status="removed",
                action=None,
                settled=lifetime_count,
                avg_pnl=lifetime_avg if lifetime_count > 0 else None,
                interval_low=lifetime_low if lifetime_count > 0 else None,
                interval_high=lifetime_high if lifetime_count > 0 else None,
                reason=existing["reason"],
                is_change=False,
            )

        # Fresh evidence: positions settled strictly after removal time `since`
        if stored_since_dt is not None:
            fresh_pnls = [pnl for dt, pnl in positions if dt > stored_since_dt]
            fresh_count = len(fresh_pnls)
            if fresh_count >= promote_min_settled:
                fresh_avg = sum(fresh_pnls) / fresh_count
                fresh_low, fresh_high = compute_bootstrap_interval(fresh_pnls)
                if fresh_low > 0:
                    return StrategyDecision(
                        strategy_key=key,
                        current_status="removed",
                        new_status="live",
                        action="promote",
                        settled=fresh_count,
                        avg_pnl=fresh_avg,
                        interval_low=fresh_low,
                        interval_high=fresh_high,
                        reason=(
                            f"Promoted: {fresh_count} settled positions since removal, "
                            f"avg pnl ${fresh_avg:+.4f}, 95% bootstrap CI [${fresh_low:+.4f}, ${fresh_high:+.4f}]"
                        ),
                        is_change=True,
                    )

        return StrategyDecision(
            strategy_key=key,
            current_status="removed",
            new_status="removed",
            action=None,
            settled=lifetime_count,
            avg_pnl=lifetime_avg if lifetime_count > 0 else None,
            interval_low=lifetime_low if lifetime_count > 0 else None,
            interval_high=lifetime_high if lifetime_count > 0 else None,
            reason=existing["reason"],
            is_change=False,
        )

    # Fallback
    return StrategyDecision(
        strategy_key=key,
        current_status=current_status,
        new_status=current_status,
        action=None,
        settled=lifetime_count,
        avg_pnl=lifetime_avg if lifetime_count > 0 else None,
        interval_low=lifetime_low if lifetime_count > 0 else None,
        interval_high=lifetime_high if lifetime_count > 0 else None,
        reason=existing.get("reason", ""),
        is_change=False,
    )


def apply_decisions(
    connection: sqlite3.Connection,
    decisions: Sequence[StrategyDecision],
    now: datetime,
) -> None:
    """Apply decisions to live_strategy_list and strategy_journal inside the active transaction."""
    now_iso = now.isoformat()
    for d in decisions:
        if not d.is_change:
            continue
        connection.execute(
            """
            INSERT INTO live_strategy_list (strategy_key, status, since, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(strategy_key) DO UPDATE SET
                status = excluded.status,
                since = excluded.since,
                reason = excluded.reason
            """,
            (d.strategy_key, d.new_status, now_iso, d.reason),
        )
        connection.execute(
            """
            INSERT INTO strategy_journal (
                recorded_at, strategy_key, action, settled, avg_pnl, interval_low, interval_high, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso,
                d.strategy_key,
                d.action,
                d.settled,
                d.avg_pnl,
                d.interval_low,
                d.interval_high,
                d.reason,
            ),
        )


def format_report(
    now: datetime,
    config: dict[str, int],
    config_notes: list[str],
    ignored_count: int,
    live_strategies: dict[str, dict],
    positions_by_strategy: dict[str, list[tuple[datetime, float]]],
    journal_20: list[dict],
    changes_24h: list[dict],
) -> str:
    """Format research/strategy_manager.md report in English markdown."""
    now_iso = now.isoformat()
    lines = [
        f"# Strategy Manager Report - {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "## Notice & Configuration",
        "",
        "> [!NOTE]",
        "> - Live results are not scored in this version, only the paper results.",
        "> - This module only decides and records; it is not yet connected to any book (a separate runner will do that later).",
        f"> - Rules config: promote_min_settled={config['promote_min_settled']}, "
        f"demote_min_settled={config['demote_min_settled']}, "
        f"demote_window_days={config['demote_window_days']}.",
        "> - These defaults are proposals that the operator edits in strategy_manager_config.json.",
        "",
    ]

    if config_notes:
        lines.append("### Configuration Notes")
        for note in config_notes:
            lines.append(f"- {note}")
        lines.append("")

    if ignored_count > 0:
        lines.append(
            f"**Ignored Positions**: {ignored_count} positions were ignored due to empty/unparseable "
            "`settled_at` or NULL/non-numeric `pnl`."
        )
        lines.append("")

    # Flag live strategies with no positions settled in the 30-day window
    cutoff_30d = now - timedelta(days=config["demote_window_days"])
    inactive_live: list[str] = []
    for key, info in sorted(live_strategies.items()):
        if info["status"] == "live":
            pos = positions_by_strategy.get(key, [])
            count_30d = sum(1 for dt, _ in pos if dt > cutoff_30d)
            if count_30d == 0:
                inactive_live.append(key)

    if inactive_live:
        lines.append("> [!WARNING]")
        lines.append(
            f"> The following live strategies have no positions settled in the last {config['demote_window_days']} days: "
            + ", ".join(inactive_live)
        )
        lines.append("")

    # 1. Live Strategy List
    lines.extend([
        "## Live Strategy List",
        "",
        "| Strategy | Status | Since (UTC) | Reason |",
        "|---|---|---|---|",
    ])
    live_rows = [info for info in live_strategies.values() if info["status"] == "live"]
    live_rows.sort(key=lambda r: r["strategy_key"])
    if live_rows:
        for r in live_rows:
            lines.append(f"| {r['strategy_key']} | {r['status']} | {r['since']} | {r['reason']} |")
    else:
        lines.append("| *None* | - | - | No live strategies currently. |")
    lines.append("")

    # 2. Strategies Close to Promotion
    # Defined as: not live, at least half of promote_min_settled, sorted by interval low
    min_close_count = config["promote_min_settled"] // 2
    close_strategies = []
    for key, info in live_strategies.items():
        if info["status"] != "live":
            positions = positions_by_strategy.get(key, [])
            if info["status"] == "removed":
                stored_since = parse_timestamp(info.get("since"))
                relevant = [pnl for dt, pnl in positions if stored_since and dt > stored_since]
            else:
                relevant = [pnl for _, pnl in positions]
            cnt = len(relevant)
            if cnt >= min_close_count:
                avg_pnl = sum(relevant) / cnt if cnt > 0 else 0.0
                low, high = compute_bootstrap_interval(relevant) if cnt > 0 else (0.0, 0.0)
                close_strategies.append({
                    "key": key,
                    "status": info["status"],
                    "settled": cnt,
                    "avg_pnl": avg_pnl,
                    "low": low,
                    "high": high,
                })

    close_strategies.sort(key=lambda item: (-item["low"], -item["settled"], item["key"]))
    lines.extend([
        "## Strategies Close to Promotion",
        "",
        f"Strategies not currently live with at least {min_close_count} settled positions (half of promote_min_settled):",
        "",
        "| Strategy | Status | Settled | Avg PnL | 95% Bootstrap CI |",
        "|---|---|---|---|---|",
    ])
    if close_strategies:
        for s in close_strategies:
            lines.append(
                f"| {s['key']} | {s['status']} | {s['settled']} | ${s['avg_pnl']:+.4f} | "
                f"[${s['low']:+.4f}, ${s['high']:+.4f}] |"
            )
    else:
        lines.append("| *None* | - | - | - | No strategies close to promotion. |")
    lines.append("")

    # 3. Changes Made in the Last 24 Hours
    lines.extend([
        "## Changes in the Last 24 Hours",
        "",
        "| Recorded At (UTC) | Strategy | Action | Settled | Avg PnL | 95% CI | Reason |",
        "|---|---|---|---|---|---|---|",
    ])
    if changes_24h:
        for c in changes_24h:
            avg_str = f"${c['avg_pnl']:+.4f}" if c.get("avg_pnl") is not None else "n/a"
            ci_str = (
                f"[${c['interval_low']:+.4f}, ${c['interval_high']:+.4f}]"
                if c.get("interval_low") is not None and c.get("interval_high") is not None
                else "n/a"
            )
            lines.append(
                f"| {c['recorded_at']} | {c['strategy_key']} | {c['action']} | {c['settled']} | "
                f"{avg_str} | {ci_str} | {c['reason']} |"
            )
    else:
        lines.append("| *None* | - | - | - | - | - | No status changes in the last 24 hours. |")
    lines.append("")

    # 4. Last 20 Journal Entries
    lines.extend([
        "## Recent Journal Entries (Last 20)",
        "",
        "| ID | Recorded At (UTC) | Strategy | Action | Settled | Avg PnL | 95% CI | Reason |",
        "|---|---|---|---|---|---|---|---|",
    ])
    if journal_20:
        for j in journal_20:
            avg_str = f"${j['avg_pnl']:+.4f}" if j.get("avg_pnl") is not None else "n/a"
            ci_str = (
                f"[${j['interval_low']:+.4f}, ${j['interval_high']:+.4f}]"
                if j.get("interval_low") is not None and j.get("interval_high") is not None
                else "n/a"
            )
            lines.append(
                f"| {j['id']} | {j['recorded_at']} | {j['strategy_key']} | {j['action']} | {j['settled']} | "
                f"{avg_str} | {ci_str} | {j['reason']} |"
            )
    else:
        lines.append("| *None* | - | - | - | - | - | - | Journal is currently empty. |")
    lines.append("")

    return "\n".join(lines)


def write_report_file(output_path: Path, content: str) -> None:
    """Atomically write content to output_path using a temporary file and os.replace."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f"{output_path.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, output_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def run_strategy_manager_pass(
    database_path: Path | None = None,
    *,
    connection: sqlite3.Connection | None = None,
    config_path: Path | None = None,
    output_path: Path | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    on_before_commit: Callable[[sqlite3.Connection], None] | None = None,
) -> tuple[list[StrategyDecision], str]:
    """Execute one scoring pass of the strategy manager.

    In normal runs:
      - Reads config (writing default proposals if missing).
      - Opens an explicit BEGIN IMMEDIATE transaction.
      - Creates tables within that transaction (rolled back on failure).
      - Reads positions and stored strategies inside the transaction.
      - Evaluates promotion, demotion, and hold decisions.
      - Applies changes to live_strategy_list and strategy_journal.
      - Commits the transaction.
      - Generates the markdown report content.

    In dry_run:
      - Opens the database in read-only mode (mode=ro).
      - Reads positions and stored state without creating tables or transactions.
      - Evaluates decisions and generates the report without writing anything to disk.
    """
    database_path = database_path or default_database_path()
    config_path = config_path or DEFAULT_CONFIG_PATH
    output_path = output_path or DEFAULT_OUTPUT_PATH

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)

    config, config_notes = load_config(config_path, dry_run=dry_run)

    if dry_run:
        if connection is None:
            if not database_path.is_file():
                print("no database", file=sys.stderr)
                raise SystemExit(1)
            uri = database_path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=timeout)
            conn.row_factory = sqlite3.Row
            should_close = True
        else:
            conn = connection
            should_close = False

        try:
            positions_by_strategy, discovered_keys, ignored_count = read_positions(conn)
            live_strategies = read_live_strategies(conn)

            all_keys = sorted(discovered_keys | set(live_strategies.keys()))
            decisions = [
                evaluate_strategy(
                    k,
                    live_strategies.get(k),
                    positions_by_strategy.get(k, []),
                    now,
                    config["promote_min_settled"],
                    config["demote_min_settled"],
                    config["demote_window_days"],
                )
                for k in all_keys
            ]

            # In dry-run, construct virtual live_strategies dict reflecting decisions for reporting
            report_live = dict(live_strategies)
            for d in decisions:
                if d.is_change:
                    report_live[d.strategy_key] = {
                        "strategy_key": d.strategy_key,
                        "status": d.new_status,
                        "since": now.isoformat(),
                        "reason": d.reason,
                    }

            journal_20 = read_journal_entries(conn, limit=20)
            changes_24h = read_recent_changes(conn, now - timedelta(hours=24))

            report_content = format_report(
                now,
                config,
                config_notes,
                ignored_count,
                report_live,
                positions_by_strategy,
                journal_20,
                changes_24h,
            )
            return decisions, report_content
        finally:
            if should_close:
                conn.close()

    # Normal run
    if connection is None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(database_path, timeout=timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        should_close = True
    else:
        conn = connection
        should_close = False

    try:
        conn.execute("BEGIN IMMEDIATE")
        initialize_manager_tables(conn)
        positions_by_strategy, discovered_keys, ignored_count = read_positions(conn)
        live_strategies = read_live_strategies(conn)

        all_keys = sorted(discovered_keys | set(live_strategies.keys()))
        decisions = [
            evaluate_strategy(
                k,
                live_strategies.get(k),
                positions_by_strategy.get(k, []),
                now,
                config["promote_min_settled"],
                config["demote_min_settled"],
                config["demote_window_days"],
            )
            for k in all_keys
        ]

        apply_decisions(conn, decisions, now)

        if on_before_commit is not None:
            on_before_commit(conn)

        conn.execute("COMMIT")

        # Read updated state from database for report
        updated_live = read_live_strategies(conn)
        journal_20 = read_journal_entries(conn, limit=20)
        changes_24h = read_recent_changes(conn, now - timedelta(hours=24))

        report_content = format_report(
            now,
            config,
            config_notes,
            ignored_count,
            updated_live,
            positions_by_strategy,
            journal_20,
            changes_24h,
        )
        return decisions, report_content
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        if should_close:
            conn.close()
