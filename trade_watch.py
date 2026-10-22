"""Read-only paper trade and health watch. Run once every two minutes."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request

try:
    import winreg
except ImportError:
    winreg = None

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


# Routine interlock bookkeeping, not problems: the hourly trade-rate throttle and re-arms
# (the daily-loss trip now re-arms itself each UTC day). Trips and refusals still alert.
QUIET_RISK_EVENTS = ("throttled", "auto_rearmed", "rearmed")


def check_risk(db, after_id):
    quiet = ", ".join("?" for _ in QUIET_RISK_EVENTS)
    return [problem("risk_event", row["id"], f"Risk event {row['id']}: {row['event']}")
            for row in db.execute(
                f"SELECT id, event FROM risk_events WHERE id > ? AND event NOT IN ({quiet}) ORDER BY id",
                (after_id, *QUIET_RISK_EVENTS))]


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


def send_phone_alert(token, chat_id, messages):
    text = "\n".join(messages)[:500]
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        urllib.request.urlopen(request, timeout=5).close()
        return True
    except Exception as exc:
        print(f"telegram alert failed: {type(exc).__name__}", file=sys.stderr)
        return False


def run_pass(db_path=None, state_path=STATE, log_path=LOG, alert_path=ALERT,
             fixer=None, notifier=None, now=None):
    now = now or datetime.now(UTC)
    db_path = Path(db_path or default_database_path())
    state_path, log_path, alert_path = map(Path, (state_path, log_path, alert_path))
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("state file is not a JSON object")
        state = loaded
        for key in ("last_trade_id", "last_ai_id", "last_risk_id", "open_problems", "last_fixer_launch"):
            if key not in state:
                raise KeyError(key)
        needs_baseline = state["last_trade_id"] is None
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        state = {"last_trade_id": 0, "last_ai_id": 0, "last_risk_id": 0,
                 "open_problems": [], "last_fixer_launch": None, "last_notified": None,
                 "pending_alerts": []}
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
    new_keys = {json.dumps(item["identity"], sort_keys=True) for item in new}

    pending = list(state.get("pending_alerts", []))
    pending_keys = {json.dumps(item["identity"], sort_keys=True) for item in pending}
    for item in new:
        key = json.dumps(item["identity"], sort_keys=True)
        if key not in pending_keys:
            pending.append(item)
            pending_keys.add(key)

    def open_problems(notifier_handled):
        undelivered = new_keys if not (fixer_handled and notifier_handled) else set()
        return [item["identity"] for item in problems
                if json.dumps(item["identity"], sort_keys=True) not in undelivered]

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
    fixer_handled = True
    if fixer and new:
        fixer_handled = False
        try:
            previous = state["last_fixer_launch"]
            if previous is None or (now - datetime.fromisoformat(previous)).total_seconds() >= 600:
                launch_fixer(fixer)
                state["last_fixer_launch"] = now.isoformat()
                fixer_handled = True
        except Exception as exc:
            print(f"trade watch fixer failed: {exc}", file=sys.stderr)

    notifier_pending = bool(notifier and pending)
    state["open_problems"] = open_problems(notifier_handled=not notifier_pending)
    state["pending_alerts"] = pending
    try:
        save_state(state_path, state)
        saved = True
    except Exception as exc:
        print(f"trade watch state save failed: {exc}", file=sys.stderr)
        saved = False

    if notifier_pending and saved:
        try:
            previous = state.get("last_notified")
            if previous is None or (now - datetime.fromisoformat(previous)).total_seconds() >= 600:
                result = notifier([item["message"] for item in pending])
                if result is not False:
                    state["last_notified"] = now.isoformat()
                    state["open_problems"] = open_problems(notifier_handled=True)
                    state["pending_alerts"] = []
                    try:
                        save_state(state_path, state)
                    except Exception as exc:
                        print(f"trade watch state save failed: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"trade watch notifier failed: {exc}", file=sys.stderr)
    return line


def default_registry_reader(name):
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def telegram_settings(environ=os.environ, registry=None):
    registry = registry or default_registry_reader

    def read_registry(name):
        try:
            return registry(name)
        except Exception:
            return None

    token = environ.get("ANDY_TELEGRAM_TOKEN")
    chat_id = environ.get("ANDY_TELEGRAM_CHAT_ID")
    if token is None and sys.platform == "win32":
        token = read_registry("ANDY_TELEGRAM_TOKEN")
    if chat_id is None and sys.platform == "win32":
        chat_id = read_registry("ANDY_TELEGRAM_CHAT_ID")
    return token, chat_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixer", help="Command to launch for a new problem")
    args = parser.parse_args()
    token, chat_id = telegram_settings()
    notifier = (lambda messages: send_phone_alert(token, chat_id, messages)) if token and chat_id else None
    try:
        run_pass(fixer=args.fixer, notifier=notifier)
    except Exception as exc:
        print(f"trade watch failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
