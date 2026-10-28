"""Total Paper P&L Report Across All Books."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3

POLYMARKET_BOOKS = [
    ("bond_book", "bond_book_positions"),
    ("price_target_book", "price_target_book_positions"),
    ("copy_book", "copy_book_positions"),
]

# Dawn chose 10,000 dollars as reference budget on 2026-10-29.
REPORT_BUDGET = 10000.0


@dataclass
class GroupResult:
    name: str
    starting_cash: float | None
    value: float | None
    pnl: float
    open_cost: float
    settled_count: int | None = None
    open_count: int | None = None
    newest_time: str | None = None
    note: str = ""


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def create_snapshot_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS total_pnl_snapshots (
            recorded_at TEXT NOT NULL,
            group_name TEXT NOT NULL,
            starting_cash REAL,
            value REAL,
            pnl REAL NOT NULL,
            open_cost REAL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS total_pnl_snapshots_lookup "
        "ON total_pnl_snapshots(recorded_at, group_name)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS total_pnl_baseline (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            recorded_at TEXT,
            total_pnl REAL
        )
        """
    )
    connection.commit()


def get_baseline(connection: sqlite3.Connection) -> tuple[str, float] | None:
    try:
        row = connection.execute(
            "SELECT recorded_at, total_pnl FROM total_pnl_baseline WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None or row[1] is None:
        return None
    return (str(row[0]), float(row[1]))


def set_baseline(
    connection: sqlite3.Connection,
    total_pnl: float,
    recorded_at: str,
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO total_pnl_baseline (id, recorded_at, total_pnl)
        VALUES (1, ?, ?)
        """,
        (recorded_at, total_pnl),
    )
    connection.commit()


def ensure_baseline(
    connection: sqlite3.Connection,
    total_pnl: float,
    recorded_at: str,
) -> tuple[str, float]:
    baseline = get_baseline(connection)
    if baseline is None:
        set_baseline(connection, total_pnl, recorded_at)
        return (recorded_at, total_pnl)
    return baseline


def collect_crypto_portfolios(connection: sqlite3.Connection) -> list[GroupResult]:
    if not _table_exists(connection, "paper_portfolio_state"):
        return [
            GroupResult(
                name="crypto_portfolios",
                starting_cash=0.0,
                value=0.0,
                pnl=0.0,
                open_cost=0.0,
                settled_count=None,
                open_count=None,
                newest_time=None,
                note="no table yet",
            )
        ]

    has_equity = _table_exists(connection, "paper_equity_curve")
    cursor = connection.execute(
        "SELECT DISTINCT predictor FROM paper_portfolio_state ORDER BY predictor"
    )
    predictors = [row[0] for row in cursor.fetchall()]
    if not predictors:
        return []

    results: list[GroupResult] = []
    for predictor in predictors:
        rows = connection.execute(
            "SELECT instrument, starting_cash FROM paper_portfolio_state WHERE predictor = ?",
            (predictor,),
        ).fetchall()

        starting_cash = sum(float(r[1]) for r in rows)
        value = 0.0
        missing: list[str] = []
        newest_equity_time: str | None = None

        for r in rows:
            instr = r[0]
            instr_start = float(r[1])
            eq_row = None
            if has_equity:
                eq_row = connection.execute(
                    """
                    SELECT equity, recorded_at
                    FROM paper_equity_curve
                    WHERE predictor = ? AND instrument = ?
                    ORDER BY recorded_at DESC, id DESC
                    LIMIT 1
                    """,
                    (predictor, instr),
                ).fetchone()

            if eq_row is not None:
                value += float(eq_row[0])
                rec_at = eq_row[1]
                if newest_equity_time is None or rec_at > newest_equity_time:
                    newest_equity_time = rec_at
            else:
                value += instr_start
                missing.append(instr)

        pnl = value - starting_cash
        note = ""
        if not has_equity:
            note = "no table yet (paper_equity_curve)"
        elif missing:
            if len(missing) == len(rows):
                note = "no equity row yet"
            else:
                note = f"no equity row yet: {', '.join(missing)}"

        results.append(
            GroupResult(
                name=predictor,
                starting_cash=starting_cash,
                value=value,
                pnl=pnl,
                open_cost=0.0,
                settled_count=None,
                open_count=None,
                newest_time=newest_equity_time,
                note=note,
            )
        )
    return results


def collect_complete_set(connection: sqlite3.Connection) -> GroupResult:
    if not _table_exists(connection, "complete_set_paper_account") or not _table_exists(
        connection, "complete_set_paper_trades"
    ):
        return GroupResult(
            name="complete_set",
            starting_cash=0.0,
            value=0.0,
            pnl=0.0,
            open_cost=0.0,
            settled_count=0,
            open_count=0,
            newest_time=None,
            note="no table yet",
        )

    row = connection.execute(
        "SELECT cash, starting_cash, updated_at FROM complete_set_paper_account WHERE id = 1"
    ).fetchone()
    if row is None:
        return GroupResult(
            name="complete_set",
            starting_cash=0.0,
            value=0.0,
            pnl=0.0,
            open_cost=0.0,
            settled_count=0,
            open_count=0,
            newest_time=None,
            note="empty account",
        )

    cash = float(row[0])
    starting_cash = float(row[1])
    updated_at = row[2]

    open_cost = 0.0
    open_count = 0
    settled_count = 0
    newest_time = updated_at

    if _table_exists(connection, "complete_set_paper_trades"):
        open_res = connection.execute(
            "SELECT COUNT(*), SUM(total_debit) FROM complete_set_paper_trades WHERE settled_at IS NULL"
        ).fetchone()
        if open_res:
            open_count = int(open_res[0])
            open_cost = float(open_res[1] or 0.0)

        settled_res = connection.execute(
            "SELECT COUNT(*), MAX(settled_at) FROM complete_set_paper_trades WHERE settled_at IS NOT NULL"
        ).fetchone()
        if settled_res:
            settled_count = int(settled_res[0])
            if settled_res[1] and (newest_time is None or settled_res[1] > newest_time):
                newest_time = settled_res[1]

        trade_open_res = connection.execute(
            "SELECT MAX(opened_at) FROM complete_set_paper_trades"
        ).fetchone()
        if trade_open_res and trade_open_res[0] and (newest_time is None or trade_open_res[0] > newest_time):
            newest_time = trade_open_res[0]

    value = cash + open_cost
    pnl = value - starting_cash

    note = ""
    if open_count > 0:
        note = f"{open_count} open trades holding cash"

    return GroupResult(
        name="complete_set",
        starting_cash=starting_cash,
        value=value,
        pnl=pnl,
        open_cost=open_cost,
        settled_count=settled_count,
        open_count=open_count,
        newest_time=newest_time,
        note=note,
    )


def collect_polymarket_books(connection: sqlite3.Connection) -> list[GroupResult]:
    results: list[GroupResult] = []
    for book_name, table_name in POLYMARKET_BOOKS:
        if not _table_exists(connection, table_name):
            results.append(
                GroupResult(
                    name=book_name,
                    starting_cash=None,
                    value=None,
                    pnl=0.0,
                    open_cost=0.0,
                    settled_count=0,
                    open_count=0,
                    newest_time=None,
                    note="no table yet",
                )
            )
            continue

        settled_res = connection.execute(
            f"SELECT COUNT(*), SUM(pnl), MAX(settled_at) FROM {table_name} WHERE settled_at IS NOT NULL"
        ).fetchone()
        settled_count = int(settled_res[0]) if settled_res else 0
        pnl = float(settled_res[1] or 0.0) if settled_res else 0.0
        max_settled_at = settled_res[2] if settled_res else None

        open_res = connection.execute(
            f"SELECT COUNT(*), SUM(fill_price * shares + fee), MAX(opened_at) FROM {table_name} WHERE settled_at IS NULL"
        ).fetchone()
        open_count = int(open_res[0]) if open_res else 0
        open_cost = float(open_res[1] or 0.0) if open_res else 0.0
        max_opened_at = open_res[2] if open_res else None

        newest_time = max_settled_at
        if max_opened_at and (newest_time is None or max_opened_at > newest_time):
            newest_time = max_opened_at

        results.append(
            GroupResult(
                name=book_name,
                starting_cash=None,
                value=None,
                pnl=pnl,
                open_cost=open_cost,
                settled_count=settled_count,
                open_count=open_count,
                newest_time=newest_time,
                note="",
            )
        )
    return results


def compute_return_pct(groups: list[GroupResult]) -> float | None:
    covered = [
        g
        for g in groups
        if g.name != "TOTAL" and g.starting_cash is not None and g.starting_cash > 0
    ]
    total_start = sum(g.starting_cash for g in covered)
    if not total_start:
        return None
    pnl_sum = sum(g.pnl for g in covered)
    return (pnl_sum / total_start) * 100.0


def collect_total(groups: list[GroupResult]) -> GroupResult:
    individual_groups = [g for g in groups if g.name != "TOTAL"]
    covered = [
        g
        for g in individual_groups
        if g.starting_cash is not None and g.starting_cash > 0
    ]
    total_start = sum(g.starting_cash for g in covered) if covered else None

    val_groups = [g for g in individual_groups if g.value is not None]
    total_val = sum(g.value for g in val_groups) if val_groups else None

    total_pnl = sum(g.pnl for g in individual_groups)
    total_open_cost = sum(g.open_cost for g in individual_groups)

    settled_counts = [
        g.settled_count for g in individual_groups if g.settled_count is not None
    ]
    total_settled = sum(settled_counts) if settled_counts else None

    open_counts = [
        g.open_count for g in individual_groups if g.open_count is not None
    ]
    total_open = sum(open_counts) if open_counts else None

    if covered and total_start:
        covered_names = ", ".join(g.name for g in covered)
        ret_pct = compute_return_pct(individual_groups)
        note = f"Return: {ret_pct:+.2f}% on starting cash (covers {covered_names})"
    else:
        note = "No groups with starting cash"

    return GroupResult(
        name="TOTAL",
        starting_cash=total_start,
        value=total_val,
        pnl=total_pnl,
        open_cost=total_open_cost,
        settled_count=total_settled,
        open_count=total_open,
        newest_time=None,
        note=note,
    )


def collect_all_groups(connection: sqlite3.Connection) -> list[GroupResult]:
    groups: list[GroupResult] = []
    groups.extend(collect_crypto_portfolios(connection))
    groups.append(collect_complete_set(connection))
    groups.extend(collect_polymarket_books(connection))
    groups.append(collect_total(groups))
    return groups


def record_snapshot(
    connection: sqlite3.Connection,
    groups: list[GroupResult],
    recorded_at: str | None = None,
) -> str:
    create_snapshot_table(connection)
    if recorded_at is None:
        recorded_at = datetime.now(UTC).isoformat()

    for g in groups:
        connection.execute(
            """
            INSERT INTO total_pnl_snapshots
                (recorded_at, group_name, starting_cash, value, pnl, open_cost)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (recorded_at, g.name, g.starting_cash, g.value, g.pnl, g.open_cost),
        )
    connection.commit()
    return recorded_at


def compute_pnl_changes(
    connection: sqlite3.Connection,
    current_pnl: float,
    current_time: datetime | None = None,
) -> dict[str, float | None]:
    if not _table_exists(connection, "total_pnl_snapshots"):
        return {"30m": None, "3h": None, "24h": None}

    now = current_time or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    windows = {
        "30m": timedelta(minutes=30),
        "3h": timedelta(hours=3),
        "24h": timedelta(hours=24),
    }

    rows = connection.execute(
        """
        SELECT recorded_at, pnl
        FROM total_pnl_snapshots
        WHERE group_name = 'TOTAL'
        ORDER BY recorded_at DESC
        """
    ).fetchall()

    snapshots: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r[0])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            snapshots.append((dt, float(r[1])))
        except Exception:
            continue

    results: dict[str, float | None] = {}
    for key, delta in windows.items():
        cutoff = now - delta
        candidates = [s for s in snapshots if s[0] <= cutoff]
        if candidates:
            nearest = max(candidates, key=lambda s: s[0])
            results[key] = current_pnl - nearest[1]
        else:
            results[key] = None

    return results


def format_money(val: float | None, show_sign: bool = False) -> str:
    if val is None:
        return "-"
    if show_sign:
        sign = "+" if val > 0 else ("-" if val < 0 else "")
        return f"{sign}${abs(val):,.2f}"
    return f"${val:,.2f}"


def format_count(val: int | None) -> str:
    if val is None:
        return "-"
    return str(val)


def format_pnl_change_line(changes: dict[str, float | None]) -> str:
    parts = []
    labels = [("30m", "last 30 minutes"), ("3h", "last 3 hours"), ("24h", "last 24 hours")]
    for key, label in labels:
        val = changes.get(key)
        if val is None:
            parts.append(f"{label}: not enough history")
        else:
            parts.append(f"{label}: {format_money(val, show_sign=True)}")
    return "Change in total pnl: " + " | ".join(parts)


def fetch_recent_totals(
    connection: sqlite3.Connection,
    limit: int = 24,
) -> list[tuple[str, float]]:
    if not _table_exists(connection, "total_pnl_snapshots"):
        return []
    rows = connection.execute(
        """
        SELECT recorded_at, pnl
        FROM total_pnl_snapshots
        WHERE group_name = 'TOTAL'
        ORDER BY recorded_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def format_zero_point_section(
    baseline_time: str | None,
    total_pnl: float | None,
    baseline_pnl: float | None,
) -> str:
    if baseline_time is None or total_pnl is None or baseline_pnl is None:
        return "### Since the zero point\n\nnot available"
    diff = total_pnl - baseline_pnl
    pct = (diff / REPORT_BUDGET) * 100.0
    diff_sign = "+" if diff >= 0 else "-"
    diff_str = f"{diff_sign}${abs(diff):,.2f}"
    return (
        "### Since the zero point\n\n"
        f"Zero point: {baseline_time}\n"
        f"Budget: {format_money(REPORT_BUDGET)}\n"
        f"Result since zero point: {diff_str} ({pct:+.2f}%)\n\n"
        "This is a reporting number only and does not limit what the books trade."
    )


def format_table(groups: list[GroupResult]) -> str:
    header = (
        "| Group | Starting Cash | Value | P&L | Open Cost (at cost) | "
        "Settled | Open | Newest Equity / Trade | Notes |"
    )
    separator = "|---|---|---|---|---|---|---|---|---|"
    lines = [header, separator]
    for g in groups:
        time_str = g.newest_time if g.newest_time else "-"
        lines.append(
            f"| {g.name} | {format_money(g.starting_cash)} | {format_money(g.value)} | "
            f"{format_money(g.pnl, show_sign=True)} | {format_money(g.open_cost)} | "
            f"{format_count(g.settled_count)} | {format_count(g.open_count)} | "
            f"{time_str} | {g.note} |"
        )
    return "\n".join(lines)


def format_report(
    connection: sqlite3.Connection,
    groups: list[GroupResult],
    recorded_at: str,
    current_time: datetime | None = None,
) -> str:
    table_str = format_table(groups)

    total_group = next((g for g in groups if g.name == "TOTAL"), None)
    total_pnl = total_group.pnl if total_group is not None else None

    baseline = get_baseline(connection) if total_pnl is not None else None
    if baseline is None or total_pnl is None:
        zero_point_section = format_zero_point_section(None, None, None)
    else:
        baseline_time, baseline_pnl = baseline
        zero_point_section = format_zero_point_section(
            baseline_time, total_pnl, baseline_pnl
        )

    changes = compute_pnl_changes(connection, total_pnl or 0.0, current_time)
    change_line = format_pnl_change_line(changes)

    recent_totals = fetch_recent_totals(connection, limit=24)
    history_lines = [
        "### Recent Total P&L History (last 24 snapshots)",
        "",
        "| Time | Total P&L |",
        "|---|---|",
    ]
    for rec_at, pnl_val in recent_totals:
        history_lines.append(f"| {rec_at} | {format_money(pnl_val, show_sign=True)} |")

    report_parts = [
        "# Total Paper P&L Report",
        "",
        f"Recorded at: {recorded_at}",
        "",
        table_str,
        "",
        zero_point_section,
        "",
        change_line,
        "",
        "\n".join(history_lines),
        "",
    ]
    return "\n".join(report_parts)
