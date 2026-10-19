"""Read-only paper trade and health watch. Run once every two minutes."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from andy_trader.store import default_database_path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".trade-watch-state.json"
LOG = ROOT / ".trade-watch.jsonl"
ALERT = ROOT / "research" / "watch_alert.md"


def problem(name, row_id=None, message=None):
    identity = name if row_id is None else [name, row_id]
    return {"identity": identity, "message": message or name}


def crashed(check_name, error, trade_id=None):
    identity = ["check_crashed", check_name]
    if trade_id is not None:
        identity.append(trade_id)
    return {"identity": identity, "message": f"{check_name} check crashed: {error}"}


def save_state(path, state):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def latest_id(db, table):
    return db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]


def check_price(db, trade):
    row = db.execute(
        "SELECT close FROM crypto_observations WHERE instrument = ? AND close > 0 "
        "AND degraded = 0 ORDER BY open_time DESC, first_seen_at DESC LIMIT 1",
        (trade["instrument"],),
    ).fetchone()
    if row is None:
        return problem("price", trade["id"], "No stored close for trade")
    if abs(trade["price"] / row["close"] - 1) > 0.015:
        return problem("price", trade["id"], "Trade price differs from stored close by over 1.5%")


def check_fee(db, trade):
    if trade["fee_cost"] <= 0:
        return problem("fee", trade["id"], "Trade fee is missing or zero")


def check_value(db, trade):
    state = db.execute(
        "SELECT * FROM paper_portfolio_state WHERE predictor = ? AND instrument = ?",
        (trade["predictor"], trade["instrument"]),
    ).fetchone()
    if state is None:
        return problem("value", trade["id"], "Book stake is missing")
    leverage = (state["leverage"] if "leverage" in state.keys() else
                10 if trade["predictor"] == "probe_x10" else 1)
    if abs(trade["qty"] * trade["price"]) > state["starting_cash"] * leverage * 1.05:
        return problem("value", trade["id"], "Traded value exceeds stake times leverage")


def check_bracket(db, trade, notes):
    if trade["side"] not in ("long", "short"):
        return None
    state = db.execute(
        "SELECT position_qty, entry_at, stop_price, target_price "
        "FROM paper_portfolio_state WHERE predictor = ? AND instrument = ?",
        (trade["predictor"], trade["instrument"]),
    ).fetchone()
    if state is None or not state["position_qty"] or state["entry_at"] != trade["executed_at"]:
        notes.append({"trade_id": trade["id"], "note": "bracket not checkable"})
        return None
    stop, target, entry = state["stop_price"], state["target_price"], trade["price"]
    valid = (stop is not None and target is not None and
             (stop < entry < target if trade["side"] == "long" else target < entry < stop))
    if not valid:
        return problem("bracket", trade["id"], "Stop or target is missing or on the wrong side")


def check_btc_bar(db, now):
    row = db.execute(
        "SELECT open_time FROM crypto_observations WHERE instrument = 'BTC-USD' "
        "AND interval = '1m' AND degraded = 0 ORDER BY open_time DESC LIMIT 1"
    ).fetchone()
    if row is None or (now - datetime.fromisoformat(row[0])).total_seconds() >= 300:
        return problem("stale_btc_bar", message="Newest 1m BTC bar is at least five minutes old")


def check_ai(db, after_id):
    return [problem("ai_status", row["id"], f"AI decision {row['id']} status: {row['status']}")
            for row in db.execute(
                "SELECT id, status FROM llm_decisions WHERE id > ? "
                "AND (status IS NULL OR status != 'ok') ORDER BY id", (after_id,))]


def check_risk(db, after_id):
    return [problem("risk_event", row["id"], f"Risk event {row['id']}: {row['event']}")
            for row in db.execute(
                "SELECT id, event FROM risk_events WHERE id > ? ORDER BY id", (after_id,))]


def review(db, state, now):
    problems, notes, reviewed = [], [], []
    checks = (("price", check_price), ("fee", check_fee), ("value", check_value))
    try:
        trades = list(db.execute("SELECT * FROM paper_trades WHERE id > ? ORDER BY id",
                                 (state["last_trade_id"],)))
        for trade in trades:
            reviewed.append(trade["id"])
            state["last_trade_id"] = trade["id"]
            for name, check in checks:
                try:
                    result = check(db, trade)
                    if result:
                        problems.append(result)
                except Exception as exc:
                    problems.append(crashed(name, exc, trade["id"]))
            try:
                result = check_bracket(db, trade, notes)
                if result:
                    problems.append(result)
            except Exception as exc:
                problems.append(crashed("bracket", exc, trade["id"]))
    except Exception as exc:
        problems.append(crashed("trade_scan", exc))

    for name, check in (("btc_bar", lambda: check_btc_bar(db, now)),
                        ("ai_status", lambda: check_ai(db, state["last_ai_id"])),
                        ("risk_events", lambda: check_risk(db, state["last_risk_id"]))):
        try:
            result = check()
            if result:
                problems.extend(result if isinstance(result, list) else [result])
        except Exception as exc:
            problems.append(crashed(name, exc))
    for key, table in (("last_ai_id", "llm_decisions"), ("last_risk_id", "risk_events")):
        try:
            state[key] = latest_id(db, table)
        except Exception as exc:
            problems.append(crashed(key, exc))
    return reviewed, problems, notes


def last_line_time(path):
    try:
        with path.open(encoding="utf-8") as handle:
            line = list(handle)[-1]
        return datetime.fromisoformat(json.loads(line)["at"])
    except (FileNotFoundError, IndexError, ValueError, KeyError):
        return None


def launch_fixer(command):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(command, shell=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags)


def run_pass(db_path=None, state_path=STATE, log_path=LOG, alert_path=ALERT,
             fixer=None, now=None):
    now = now or datetime.now(UTC)
    db_path = Path(db_path or default_database_path())
    state_path, log_path, alert_path = map(Path, (state_path, log_path, alert_path))
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        needs_baseline = state["last_trade_id"] is None
    except FileNotFoundError:
        state = {"last_trade_id": 0, "last_ai_id": 0, "last_risk_id": 0,
                 "open_problems": [], "last_fixer_launch": None}
        needs_baseline = True
    reviewed, problems, notes = [], [], []
    try:
        db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            if needs_baseline:
                for key, table in (("last_trade_id", "paper_trades"),
                                   ("last_ai_id", "llm_decisions"),
                                   ("last_risk_id", "risk_events")):
                    state[key] = latest_id(db, table)
            reviewed, problems, notes = review(db, state, now)
        finally:
            db.close()
    except Exception as exc:
        problems.append(problem("db_unreadable", message=f"Database unreadable: {exc}"))
        if needs_baseline:
            state["last_trade_id"] = None
            state["last_ai_id"] = None
            state["last_risk_id"] = None

    old = {json.dumps(item, sort_keys=True) for item in state["open_problems"]}
    new = [item for item in problems if json.dumps(item["identity"], sort_keys=True) not in old]
    state["open_problems"] = [item["identity"] for item in problems]
    line = {"at": now.isoformat(), "trades_reviewed": reviewed,
            "problems": problems, "notes": notes}
    try:
        previous = last_line_time(log_path)
        if reviewed or problems or previous is None or (now - previous).total_seconds() >= 600:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
    except Exception as exc:
        print(f"trade watch log failed: {exc}", file=sys.stderr)
    if new:
        try:
            alert_path.parent.mkdir(parents=True, exist_ok=True)
            alert_path.write_text("Trade watch alert at " + now.isoformat() + "\n" +
                                  "\n".join(item["message"] for item in new) + "\n",
                                  encoding="utf-8")
        except Exception as exc:
            print(f"trade watch alert failed: {exc}", file=sys.stderr)
    if fixer and new:
        try:
            previous = state["last_fixer_launch"]
            if previous is None or (now - datetime.fromisoformat(previous)).total_seconds() >= 600:
                launch_fixer(fixer)
                state["last_fixer_launch"] = now.isoformat()
        except Exception as exc:
            print(f"trade watch fixer failed: {exc}", file=sys.stderr)
    try:
        save_state(state_path, state)
    except Exception as exc:
        print(f"trade watch state save failed: {exc}", file=sys.stderr)
    return line


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixer", help="Command to launch for a new problem")
    args = parser.parse_args()
    try:
        run_pass(fixer=args.fixer)
    except Exception as exc:
        print(f"trade watch failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
