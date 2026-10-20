"""One paper-only probe pass, scheduled every minute."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import os
import queue
import threading
import time
import uuid

from andy_trader.collector import FetchSettings, fetch_binance
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.llm_agent import LlmPredictor
from andy_trader.llm_ledger import initialize_ledger
from andy_trader.portfolio import (initialize_portfolio, get_or_create_state,
    execute_paper_trade, check_bracket, mark_to_market)
from andy_trader.predict import predict_once
from andy_trader.probe_book import probe_trade_once, probe_x10_trade_once
from andy_trader.risk import check_and_enforce
from andy_trader.store import connect, default_database_path, record_observations

INSTRUMENTS = tuple(f"{coin}-USD" for coin in ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"))
BOOKS = (("probe", 1, probe_trade_once), ("probe_x10", 10, probe_x10_trade_once))
LOCK_PATH = REPO_ROOT / ".probe-watch.lock"
JOURNAL_PATH = REPO_ROOT / ".probe-watch.jsonl"


def journal(event, **details):
    with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(UTC).isoformat(), "event": event, **details}) + "\n")


def acquire_lock():
    token = uuid.uuid4().hex
    try:
        if time.time() - LOCK_PATH.stat().st_mtime < 180:
            return None
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    try:
        with LOCK_PATH.open("x") as handle:
            handle.write(token)
    except FileExistsError:
        return None
    return token


def journal_closes(connection, after_id):
    rows = connection.execute("SELECT * FROM paper_trades WHERE id > ? AND side = 'flat' "
                              "AND predictor IN ('probe', 'probe_x10') ORDER BY id", (after_id,))
    for row in rows:
        entry = connection.execute("SELECT * FROM paper_trades WHERE predictor = ? AND instrument = ? "
                                   "AND id < ? ORDER BY id DESC LIMIT 1",
                                   (row["predictor"], row["instrument"], row["id"])).fetchone()
        if entry is None or entry["side"] == "flat":
            raise ValueError(f"Missing entry for closed trade {row['id']}")
        entry_cost = entry["fee_cost"] + entry["slippage_cost"]
        exit_cost = row["fee_cost"] + row["slippage_cost"]
        direction = 1 if entry["side"] == "long" else -1
        pnl = direction * row["qty"] * (row["price"] - entry["price"]) - entry_cost - exit_cost
        journal("closed_trade", trade_id=row["id"], book=row["predictor"], instrument=row["instrument"],
                reason=row["reason"], pnl=pnl, entry_cost=entry_cost, exit_cost=exit_cost)


def run_pass(connection, *, now=None):
    started = time.monotonic()
    initialize_portfolio(connection)
    initialize_ledger(connection)
    connection.execute("CREATE TABLE IF NOT EXISTS probe_batch_claims "
                       "(book TEXT NOT NULL, batch_id TEXT NOT NULL, claimed_at TEXT NOT NULL, UNIQUE(book, batch_id))")
    after_id = connection.execute("SELECT COALESCE(MAX(id), 0) FROM paper_trades").fetchone()[0]
    settings = FetchSettings(timeout_seconds=8, retries=1, rate_limit_seconds=0)
    results = queue.Queue()
    def fetch(instrument):
        try:
            results.put((instrument, fetch_binance(instrument, "1m", settings), None))
        except Exception as exc:
            results.put((instrument, [], exc))
    deadline = time.monotonic() + settings.timeout_seconds
    pending = set(INSTRUMENTS)
    for instrument in INSTRUMENTS:
        threading.Thread(target=fetch, args=(instrument,), daemon=True).start()
    while pending:
        try:
            instrument, candles, error = results.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            for instrument in sorted(pending):
                journal("fetch_error", instrument=instrument, error="8 second timeout")
            break
        pending.remove(instrument)
        if error is not None:
            journal("fetch_error", instrument=instrument, error=str(error))
        else:
            record_observations(connection, candles)
    moment = now or datetime.now(UTC)
    prices = {}
    for instrument in INSTRUMENTS:
        row = connection.execute("SELECT open_time, close FROM crypto_observations WHERE instrument = ? "
                                 "AND venue = 'binance' AND interval = '1m' AND degraded = 0 AND close > 0 "
                                 "AND open_time <= ? ORDER BY open_time DESC, first_seen_at DESC LIMIT 1",
                                 (instrument, (moment - timedelta(minutes=1)).isoformat())).fetchone()
        if row is None or (moment - datetime.fromisoformat(row["open_time"])).total_seconds() > 150:
            journal("stale_price", instrument=instrument)
        else:
            prices[instrument] = float(row["close"])
    try:
        for book, leverage, _ in BOOKS:
            for instrument, price in prices.items():
                # Protective exits run before AI; the minimum hold applies only to signal exits.
                state = get_or_create_state(connection, predictor=book, instrument=instrument,
                                            starting_cash=1000, leverage=leverage, now_iso=moment.isoformat())
                decision = check_and_enforce(connection, predictor=book, instrument=instrument,
                    starting_cash=state.starting_cash, now_iso=moment.isoformat(),
                    current_equity=max(0, state.cash + state.position_qty * price))
                if not decision.allowed and state.position_qty:
                    execute_paper_trade(connection, predictor=book, instrument=instrument,
                        target_side="flat", price=price, now_iso=moment.isoformat(),
                        reason=f"ungated RISK INTERLOCK forced exit: {decision.reason}")
                check_bracket(connection, book, instrument, moment.isoformat(), reason_prefix="ungated", fresh_minutes=2.5)
                mark_to_market(connection, predictor=book, instrument=instrument, price=price,
                               now_iso=moment.isoformat(), starting_cash=1000)
        # Commit and journal exits before any network request to the model.
        journal_closes(connection, after_id)
        after_id = connection.execute("SELECT COALESCE(MAX(id), 0) FROM paper_trades").fetchone()[0]
        latest = connection.execute("SELECT created_at FROM llm_decisions WHERE status NOT IN "
                                    "('replay', 'ablation', 'capped') ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        if latest and (moment - datetime.fromisoformat(latest[0])).total_seconds() <= float(os.environ.get("LLM_MIN_MINUTES", "60")) * 60:
            return
        if time.monotonic() - started >= 20 or not prices:
            return
        if not all(os.environ.get(key) for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")):
            return
        model = LlmPredictor(INSTRUMENTS, request_timeout=25)
        predict_once(connection, instruments=tuple(prices), horizons=("1h",), interval="1m",
                     predictors=(model,), now_iso=moment.isoformat(), maximum_data_age_minutes=2.5)
        if model.batch_id is None:
            return
        for book, _, trade in BOOKS:
            claimed = connection.execute("INSERT OR IGNORE INTO probe_batch_claims VALUES (?, ?, ?)",
                                         (book, model.batch_id, moment.isoformat())).rowcount
            connection.commit()
            if claimed:
                for instrument in prices:
                    trade(connection, instrument=instrument, interval="1m", batch_id=model.batch_id,
                          now=now or datetime.now(UTC), max_data_age_minutes=2.5)
    finally:
        journal_closes(connection, after_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database")
    args = parser.parse_args(argv)
    token = acquire_lock()
    if token is None:
        return 0
    try:
        load_env_file(REPO_ROOT / ".env")
        with connect(Path(args.database) if args.database else default_database_path()) as connection:
            run_pass(connection)
        return 0
    except Exception as exc:
        journal("error", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if LOCK_PATH.exists() and LOCK_PATH.read_text() == token:
            LOCK_PATH.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
