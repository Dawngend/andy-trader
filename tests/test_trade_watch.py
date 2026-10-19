"""The watch reads a temporary copy of the paper-trading schema."""

from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

import trade_watch


NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


@pytest.fixture
def watch(tmp_path):
    db_path = tmp_path / "trades.db"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, predictor TEXT, instrument TEXT,
            side TEXT, qty REAL, price REAL, fee_cost REAL, executed_at TEXT);
        CREATE TABLE paper_portfolio_state (predictor TEXT, instrument TEXT, starting_cash REAL,
            position_qty REAL, entry_at TEXT, stop_price REAL, target_price REAL, leverage REAL);
        CREATE TABLE crypto_observations (instrument TEXT, interval TEXT, open_time TEXT,
            close REAL, degraded INTEGER, first_seen_at TEXT);
        CREATE TABLE llm_decisions (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE risk_events (id INTEGER PRIMARY KEY, event TEXT);
    """)
    db.execute("INSERT INTO crypto_observations VALUES (?, ?, ?, ?, ?, ?)",
               ("BTC-USD", "1m", (NOW - timedelta(minutes=1)).isoformat(), 100, 0, NOW.isoformat()))
    db.execute("INSERT INTO paper_portfolio_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               ("probe", "BTC-USD", 1000, 1, NOW.isoformat(), 90, 110, 1))
    db.commit()
    paths = {"db_path": db_path, "state_path": tmp_path / "state.json",
             "log_path": tmp_path / "watch.jsonl", "alert_path": tmp_path / "alert.md"}
    trade_watch.run_pass(**paths, now=NOW)
    yield db, paths
    db.close()


def add_trade(db, trade_id=123, *, price=100, fee=1, qty=1, side="long"):
    db.execute("INSERT INTO paper_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (trade_id, "probe", "BTC-USD", side, qty, price, fee, NOW.isoformat()))
    db.commit()


def run(watch, at=NOW, **kwargs):
    db, paths = watch
    return trade_watch.run_pass(**paths, now=at, **kwargs)


def identities(line):
    return [item["identity"] for item in line["problems"]]


def test_clean_trade(watch):
    add_trade(watch[0])
    line = run(watch)
    assert line["trades_reviewed"] == [123]
    assert line["problems"] == []


@pytest.mark.parametrize("changes, expected", [
    ({"price": 105}, "price"),
    ({"fee": 0}, "fee"),
    ({"qty": 11}, "value"),
])
def test_bad_trade_values(watch, changes, expected):
    add_trade(watch[0], **changes)
    assert [expected, 123] in identities(run(watch))


@pytest.mark.parametrize("stop,target", [(101, 110), (None, 110)])
def test_bad_open_bracket(watch, stop, target):
    db = watch[0]
    db.execute("UPDATE paper_portfolio_state SET stop_price = ?, target_price = ?", (stop, target))
    db.commit()
    add_trade(db)
    assert ["bracket", 123] in identities(run(watch))


def test_closed_position_bracket_not_checkable(watch):
    db = watch[0]
    db.execute("UPDATE paper_portfolio_state SET position_qty = 0, stop_price = NULL")
    db.commit()
    add_trade(db)
    line = run(watch)
    assert ["bracket", 123] not in identities(line)
    assert line["notes"] == [{"trade_id": 123, "note": "bracket not checkable"}]


def test_stale_btc_identity_stays_same(watch):
    first = run(watch, NOW + timedelta(minutes=6))
    second = run(watch, NOW + timedelta(minutes=9))
    assert identities(first) == identities(second) == ["stale_btc_bar"]
    assert watch[1]["alert_path"].read_text().count("Trade watch alert") == 1


def test_new_ai_and_risk_rows(watch):
    db = watch[0]
    db.execute("INSERT INTO llm_decisions VALUES (7, 'error')")
    db.execute("INSERT INTO risk_events VALUES (8, 'halt')")
    db.commit()
    assert identities(run(watch)) == [["ai_status", 7], ["risk_event", 8]]


def test_trade_ids_are_distinct(watch):
    db = watch[0]
    add_trade(db, 123, fee=0)
    add_trade(db, 124, fee=0)
    found = identities(run(watch))
    assert ["fee", 123] in found and ["fee", 124] in found


def test_crashing_check_does_not_stop_trades(watch, monkeypatch):
    db = watch[0]
    add_trade(db, 123)
    add_trade(db, 124)
    def crash(db, trade):
        raise RuntimeError("broken check")
    monkeypatch.setattr(trade_watch, "check_fee", crash)
    line = run(watch)
    assert line["trades_reviewed"] == [123, 124]
    assert ["check_crashed", "fee", 123] in identities(line)
    assert ["check_crashed", "fee", 124] in identities(line)
    assert json.loads(watch[1]["state_path"].read_text())["last_trade_id"] == 124


def test_first_run_skips_old_rows(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""
        CREATE TABLE paper_trades (id INTEGER PRIMARY KEY);
        CREATE TABLE llm_decisions (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE risk_events (id INTEGER PRIMARY KEY, event TEXT);
        CREATE TABLE crypto_observations (instrument TEXT, interval TEXT, open_time TEXT,
            degraded INTEGER);
        INSERT INTO paper_trades VALUES (12);
        INSERT INTO llm_decisions VALUES (13, 'error');
        INSERT INTO risk_events VALUES (14, 'halt');
    """)
    db.execute("INSERT INTO crypto_observations VALUES ('BTC-USD', '1m', ?, 0)",
               ((NOW - timedelta(minutes=1)).isoformat(),))
    db.commit()
    db.close()
    line = trade_watch.run_pass(db_path=tmp_path / "old.db", state_path=tmp_path / "state.json",
                                log_path=tmp_path / "log.jsonl", alert_path=tmp_path / "alert.md", now=NOW)
    assert line["trades_reviewed"] == [] and line["problems"] == []
    assert not (tmp_path / "alert.md").exists()


def test_failed_state_save_keeps_old_cursor(watch, monkeypatch):
    db, paths = watch
    add_trade(db)
    before = paths["state_path"].read_text()
    def fail(path, state):
        raise OSError("disk full")
    monkeypatch.setattr(trade_watch, "save_state", fail)
    assert run(watch)["trades_reviewed"] == [123]
    assert paths["state_path"].read_text() == before
    assert run(watch)["trades_reviewed"] == [123]


def test_unreadable_database_still_outputs(tmp_path):
    paths = {"db_path": tmp_path / "missing.db", "state_path": tmp_path / "state.json",
             "log_path": tmp_path / "log.jsonl", "alert_path": tmp_path / "alert.md"}
    line = trade_watch.run_pass(**paths, now=NOW)
    assert identities(line) == ["db_unreadable"]
    assert len(paths["log_path"].read_text().splitlines()) == 1
    assert json.loads(paths["state_path"].read_text())["last_trade_id"] is None
    db = sqlite3.connect(paths["db_path"])
    db.executescript("""
        CREATE TABLE paper_trades (id INTEGER PRIMARY KEY);
        CREATE TABLE llm_decisions (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE risk_events (id INTEGER PRIMARY KEY, event TEXT);
        CREATE TABLE crypto_observations (instrument TEXT, interval TEXT, open_time TEXT,
            degraded INTEGER);
        INSERT INTO paper_trades VALUES (12);
        INSERT INTO llm_decisions VALUES (13, 'error');
        INSERT INTO risk_events VALUES (14, 'halt');
    """)
    db.execute("INSERT INTO crypto_observations VALUES ('BTC-USD', '1m', ?, 0)",
               ((NOW - timedelta(minutes=1)).isoformat(),))
    db.commit()
    db.close()
    recovered = trade_watch.run_pass(**paths, now=NOW + timedelta(minutes=1))
    assert recovered["trades_reviewed"] == [] and recovered["problems"] == []
    assert json.loads(paths["state_path"].read_text())["last_trade_id"] == 12


def test_fixer_rate_limit(watch, monkeypatch):
    launches = []
    monkeypatch.setattr(trade_watch, "launch_fixer", launches.append)
    add_trade(watch[0], 123, fee=0)
    run(watch, fixer="fix things")
    add_trade(watch[0], 124, fee=0)
    run(watch, NOW + timedelta(minutes=2), fixer="fix things")
    assert launches == ["fix things"]


def test_idle_line_waits_ten_minutes(watch):
    paths = watch[1]
    run(watch, NOW + timedelta(minutes=2))
    assert len(paths["log_path"].read_text().splitlines()) == 1
    run(watch, NOW + timedelta(minutes=10))
    assert len(paths["log_path"].read_text().splitlines()) == 2
