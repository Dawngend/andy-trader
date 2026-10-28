"""Forward approved simulated book entries to the Polymarket sender."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
from pathlib import Path
import sqlite3
from typing import Callable, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from andy_trader.bond_book import strategy_slice
from andy_trader.env import REPO_ROOT
from andy_trader.polymarket_sender import Refused, send_buy
from andy_trader.price_target_book import POSITIONS_TABLE as PRICE_TARGET_POSITIONS_TABLE
from andy_trader.store import default_database_path

LOG_TABLE = "pm_live_runner_log"
STRATEGY_TABLE = "live_strategy_list"
GATE_TABLE = "pm_ai_gate_decisions"
BOND_BOOK_NAME = "bond_book"
BOND_POSITIONS_TABLE = "bond_book_positions"
PRICE_TARGET_BOOK_NAME = "price_target_book"

BOOKS = (
    (BOND_BOOK_NAME, BOND_POSITIONS_TABLE),
    (PRICE_TARGET_BOOK_NAME, PRICE_TARGET_POSITIONS_TABLE),
)

DEFAULT_CONFIG_PATH = REPO_ROOT / "pm_live_runner_config.json"
DEFAULT_CONFIG = {
    "max_orders_per_end_time": 2,
}
MAX_RESERVED_PER_PASS = 10
BAND = Decimal("0.03")


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_iso(val: object) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    try:
        dt = datetime.fromisoformat(val.strip())
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def initialize_runner(connection: sqlite3.Connection) -> None:
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book TEXT NOT NULL,
            market_id TEXT NOT NULL,
            side TEXT NOT NULL,
            strategy_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            action TEXT NOT NULL,
            sender_row_id INTEGER,
            reason TEXT,
            ends_at_utc TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            UNIQUE (book, market_id, side, mode)
        )
    """)


def get_strategy_key(book: str, row: sqlite3.Row | dict) -> str:
    if book == BOND_BOOK_NAME:
        return f"bond:{strategy_slice(row['title'], row['side'])}"
    if book == PRICE_TARGET_BOOK_NAME:
        return "price_target"
    raise ValueError(f"Unknown book: {book}")


def is_position_fresh(row: sqlite3.Row | dict, now: datetime) -> bool:
    if row.get("settled_at") is not None:
        return False
    opened_at = parse_iso(row.get("opened_at"))
    ends_at = parse_iso(row.get("ends_at"))
    if opened_at is None or ends_at is None:
        return False
    if opened_at > now:
        return False
    if now - opened_at > timedelta(minutes=45):
        return False
    if ends_at <= now:
        return False
    return True


def is_strategy_live(connection: sqlite3.Connection, strategy_key: str) -> bool:
    if not table_exists(connection, STRATEGY_TABLE):
        return False
    row = connection.execute(
        f"SELECT status FROM {STRATEGY_TABLE} WHERE strategy_key = ?",
        (strategy_key,),
    ).fetchone()
    if row is None:
        return False
    return row["status"] == "live"


def get_gate_approval(
    connection: sqlite3.Connection,
    book: str,
    market_id: str,
    side: str,
) -> tuple[bool, str]:
    if not table_exists(connection, GATE_TABLE):
        return False, ""
    row = connection.execute(
        f"SELECT verdict, reason FROM {GATE_TABLE} WHERE book = ? AND market_id = ? AND side = ?",
        (book, market_id, side),
    ).fetchone()
    if row is None:
        return False, ""
    return row["verdict"] == "approve", row["reason"] or ""


def load_runner_config(config_path: Path | str | None = None) -> dict[str, int]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        return dict(DEFAULT_CONFIG)

    try:
        content = path.read_text(encoding="utf-8")
        raw = json.loads(content)
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)

    if not isinstance(raw, dict):
        return dict(DEFAULT_CONFIG)

    val = raw.get("max_orders_per_end_time")
    if type(val) is int and not isinstance(val, bool) and val >= 1:
        return {"max_orders_per_end_time": val}
    return dict(DEFAULT_CONFIG)


def extract_token(market: dict | None, side: str) -> str | None:
    if not isinstance(market, dict):
        return None
    outcomes_val = market.get("outcomes")
    tokens_val = market.get("clobTokenIds")
    if outcomes_val is None or tokens_val is None:
        return None
    try:
        outcomes = json.loads(outcomes_val) if isinstance(outcomes_val, str) else outcomes_val
        tokens = json.loads(tokens_val) if isinstance(tokens_val, str) else tokens_val
    except (ValueError, TypeError):
        return None
    if not isinstance(outcomes, list) or not isinstance(tokens, list):
        return None
    for outcome, token in zip(outcomes, tokens):
        if str(outcome) == str(side):
            return str(token) if token is not None else None
    return None


def default_read_market(market_id: str) -> dict:
    url = f"https://gamma-api.polymarket.com/markets/{market_id}"
    req = Request(url, headers={"User-Agent": "andy-trader-pm-live-runner/1.0", "Accept": "application/json"})
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}


def default_read_book(token_id: str) -> dict:
    url = f"https://clob.polymarket.com/book?{urlencode({'token_id': token_id})}"
    req = Request(url, headers={"User-Agent": "andy-trader-pm-live-runner/1.0", "Accept": "application/json"})
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_candidate_positions(
    connection: sqlite3.Connection,
    mode: str,
    now: datetime,
) -> list[dict]:
    candidates = []
    has_log = table_exists(connection, LOG_TABLE)

    for book_name, table_name in BOOKS:
        if not table_exists(connection, table_name):
            continue
        cursor = connection.execute(f"SELECT * FROM {table_name}")
        rows = cursor.fetchall()
        for r in rows:
            row_dict = dict(r)
            if not is_position_fresh(row_dict, now):
                continue

            market_id = str(row_dict["market_id"])
            side = str(row_dict["side"])

            if has_log:
                already = connection.execute(
                    f"SELECT 1 FROM {LOG_TABLE} WHERE book = ? AND market_id = ? AND side = ? AND mode = ?",
                    (book_name, market_id, side, mode),
                ).fetchone()
                if already is not None:
                    continue

            strategy_key = get_strategy_key(book_name, row_dict)
            if not is_strategy_live(connection, strategy_key):
                continue

            approved, gate_reason = get_gate_approval(connection, book_name, market_id, side)
            if not approved:
                continue

            opened_at_dt = parse_iso(row_dict["opened_at"])
            candidates.append({
                "book": book_name,
                "table": table_name,
                "market_id": market_id,
                "side": side,
                "title": row_dict.get("title", ""),
                "fill_price": row_dict["fill_price"],
                "shares": row_dict["shares"],
                "opened_at": row_dict["opened_at"],
                "opened_at_dt": opened_at_dt,
                "ends_at": row_dict["ends_at"],
                "strategy_key": strategy_key,
                "gate_reason": gate_reason,
            })

    candidates.sort(key=lambda c: (c["opened_at_dt"], c["book"], c["market_id"], c["side"]))
    return candidates


def run_pass(
    database_path: str | Path | None = None,
    config_path: str | Path | None = None,
    *,
    live: bool = False,
    send: Callable = send_buy,
    clock: Callable[[], datetime] = utc_now,
    read_market: Callable[[str], dict] | None = None,
    read_book: Callable[[str], dict] | None = None,
) -> dict:
    mode = "live" if live else "dry"
    now = ensure_utc(clock())
    market_reader = read_market or default_read_market
    book_reader = read_book or default_read_book

    cfg = load_runner_config(config_path)
    max_orders_per_end_time = cfg["max_orders_per_end_time"]

    db_path = Path(database_path) if database_path else default_database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "mode": mode,
        "candidates_seen": 0,
        "reserved": 0,
        "forwarded": 0,
        "skipped": 0,
        "error": 0,
        "reasons": {},
    }

    connection = sqlite3.connect(db_path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.isolation_level = None

    try:
        initialize_runner(connection)
        candidates = read_candidate_positions(connection, mode, now)
        summary["candidates_seen"] = len(candidates)

        reserved_count = 0
        for cand in candidates:
            if reserved_count >= MAX_RESERVED_PER_PASS:
                break

            # 1. Token lookup
            try:
                market = market_reader(cand["market_id"])
            except Exception:
                market = None
            token = extract_token(market, cand["side"])
            if not token:
                summary["reasons"]["no_token"] = summary["reasons"].get("no_token", 0) + 1
                continue

            # 2. Price lookup
            try:
                book_data = book_reader(token)
            except Exception:
                book_data = None
            if not isinstance(book_data, dict):
                summary["reasons"]["price_moved"] = summary["reasons"].get("price_moved", 0) + 1
                continue

            asks = []
            for level in book_data.get("asks", []):
                if isinstance(level, dict) and "price" in level:
                    try:
                        p = Decimal(str(level["price"]))
                        if p > 0:
                            asks.append(p)
                    except (InvalidOperation, TypeError, ValueError):
                        continue
            if not asks:
                summary["reasons"]["price_moved"] = summary["reasons"].get("price_moved", 0) + 1
                continue

            best_ask = min(asks)

            tick_val = book_data.get("tick_size")
            if tick_val is None:
                summary["reasons"]["no_tick"] = summary["reasons"].get("no_tick", 0) + 1
                continue
            try:
                tick = Decimal(str(tick_val))
                if tick <= 0:
                    summary["reasons"]["no_tick"] = summary["reasons"].get("no_tick", 0) + 1
                    continue
            except (InvalidOperation, TypeError, ValueError):
                summary["reasons"]["no_tick"] = summary["reasons"].get("no_tick", 0) + 1
                continue

            limit_price = (best_ask / tick).to_integral_value(rounding=ROUND_CEILING) * tick

            try:
                paper_fill = Decimal(str(cand["fill_price"]))
            except (InvalidOperation, TypeError, ValueError):
                summary["reasons"]["price_moved"] = summary["reasons"].get("price_moved", 0) + 1
                continue

            diff = limit_price - paper_fill
            if diff > BAND or diff < -BAND:
                summary["reasons"]["price_moved"] = summary["reasons"].get("price_moved", 0) + 1
                continue

            # 3. Reservation transaction
            fresh_now = ensure_utc(clock())
            connection.execute("BEGIN IMMEDIATE")
            try:
                already = connection.execute(
                    f"SELECT 1 FROM {LOG_TABLE} WHERE book = ? AND market_id = ? AND side = ? AND mode = ?",
                    (cand["book"], cand["market_id"], cand["side"], mode),
                ).fetchone()
                if already is not None:
                    connection.execute("ROLLBACK")
                    continue

                pos_row = connection.execute(
                    f"SELECT * FROM {cand['table']} WHERE market_id = ? AND side = ?",
                    (cand["market_id"], cand["side"]),
                ).fetchone()
                if pos_row is None:
                    connection.execute("ROLLBACK")
                    continue

                pos_dict = dict(pos_row)
                if not is_position_fresh(pos_dict, fresh_now):
                    connection.execute("ROLLBACK")
                    continue

                strategy_key = get_strategy_key(cand["book"], pos_dict)
                if not is_strategy_live(connection, strategy_key):
                    connection.execute("ROLLBACK")
                    continue

                approved, fresh_gate_reason = get_gate_approval(
                    connection, cand["book"], cand["market_id"], cand["side"]
                )
                if not approved:
                    connection.execute("ROLLBACK")
                    continue
                gate_reason = fresh_gate_reason or cand["gate_reason"]

                ends_at_dt = parse_iso(pos_dict["ends_at"])
                if ends_at_dt is None:
                    connection.execute("ROLLBACK")
                    continue
                ends_at_utc = ends_at_dt.isoformat()

                window_count = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {LOG_TABLE}
                    WHERE mode = ? AND ends_at_utc = ? AND action IN ('reserved', 'forwarded', 'error')
                    """,
                    (mode, ends_at_utc),
                ).fetchone()[0]
                if window_count >= max_orders_per_end_time:
                    summary["reasons"]["same_end_time"] = summary["reasons"].get("same_end_time", 0) + 1
                    connection.execute("ROLLBACK")
                    continue

                decided_at_str = fresh_now.isoformat()
                cursor = connection.execute(
                    f"""
                    INSERT INTO {LOG_TABLE}
                    (book, market_id, side, strategy_key, mode, action, ends_at_utc, decided_at)
                    VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)
                    """,
                    (
                        cand["book"],
                        cand["market_id"],
                        cand["side"],
                        strategy_key,
                        mode,
                        ends_at_utc,
                        decided_at_str,
                    ),
                )
                log_id = cursor.lastrowid
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

            reserved_count += 1
            summary["reserved"] += 1

            # 4. Sender call
            reason_text = f"{cand['book']} {strategy_key} {gate_reason}"
            intent = {
                "token_id": token,
                "price": limit_price,
                "shares": Decimal(str(pos_dict["shares"])),
                "reason": reason_text,
            }

            try:
                res = send(intent, live=live, database_path=db_path)
            except Refused as exc:
                if str(exc) == "busy":
                    connection.execute(f"DELETE FROM {LOG_TABLE} WHERE id = ?", (log_id,))
                else:
                    connection.execute(
                        f"UPDATE {LOG_TABLE} SET action = 'skipped', reason = ? WHERE id = ?",
                        (str(exc), log_id),
                    )
                    summary["skipped"] += 1
                continue
            except Exception as exc:
                connection.execute(
                    f"UPDATE {LOG_TABLE} SET action = 'error', reason = ? WHERE id = ?",
                    (str(exc), log_id),
                )
                summary["error"] += 1
                continue

            sender_row_id = res.get("id") if isinstance(res, dict) else None
            sender_status = res.get("status") if isinstance(res, dict) else None

            if sender_status in ("dry_run", "sent", "filled"):
                action = "forwarded"
                reason = f"sender status: {sender_status}"
                summary["forwarded"] += 1
            elif sender_status == "rejected":
                action = "skipped"
                reason = "sender rejected"
                summary["skipped"] += 1
            elif sender_status in ("pending", "unknown"):
                action = "forwarded"
                reason = "sender status: unknown, check the sender ledger"
                summary["forwarded"] += 1
            else:
                action = "forwarded"
                reason = f"sender status: {sender_status}, check the sender ledger"
                summary["forwarded"] += 1

            connection.execute(
                f"UPDATE {LOG_TABLE} SET action = ?, sender_row_id = ?, reason = ? WHERE id = ?",
                (action, sender_row_id, reason, log_id),
            )
    finally:
        connection.close()

    return summary


def print_status(database_path: str | Path, *, clock: Callable[[], datetime] = utc_now) -> None:
    path = Path(database_path) if database_path else default_database_path()
    if not path.is_file():
        print("Database file not found.")
        return

    now = ensure_utc(clock())
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        if not table_exists(connection, LOG_TABLE):
            print("No live runner log table found.")
            return

        print("Counts per mode:")
        for mode in ("dry", "live"):
            counts = {}
            for action in ("forwarded", "skipped", "error"):
                cnt = connection.execute(
                    f"SELECT COUNT(*) FROM {LOG_TABLE} WHERE mode = ? AND action = ?",
                    (mode, action),
                ).fetchone()[0]
                counts[action] = cnt
            print(f"  Mode {mode}: forwarded={counts['forwarded']}, skipped={counts['skipped']}, error={counts['error']}")

        reserved_rows = connection.execute(
            f"SELECT * FROM {LOG_TABLE} WHERE action = 'reserved' ORDER BY id ASC"
        ).fetchall()
        print("\nReserved rows:")
        if not reserved_rows:
            print("  none")
        else:
            for r in reserved_rows:
                decided = parse_iso(r["decided_at"])
                if decided:
                    age_seconds = int((now - decided).total_seconds())
                    age_str = f"{age_seconds}s"
                else:
                    age_str = "unknown"
                print(
                    f"  id={r['id']} book={r['book']} market_id={r['market_id']} "
                    f"side={r['side']} mode={r['mode']} age={age_str}"
                )

        recent_rows = connection.execute(
            f"SELECT * FROM {LOG_TABLE} ORDER BY id DESC LIMIT 5"
        ).fetchall()
        print("\nFive most recent rows:")
        if not recent_rows:
            print("  none")
        else:
            for r in recent_rows:
                print(
                    f"  id={r['id']} mode={r['mode']} action={r['action']} "
                    f"book={r['book']} market_id={r['market_id']} side={r['side']} "
                    f"sender_row_id={r['sender_row_id']} reason={r['reason']}"
                )
    finally:
        connection.close()
