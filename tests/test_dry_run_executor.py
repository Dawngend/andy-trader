"""The dry-run executor: decides what each intent would have done. Never sends."""

import sqlite3

import pytest

import andy_trader.dry_run_executor as executor
from andy_trader.order_intent import initialize_order_intents, new_intent, record_intent
from andy_trader.paper_gate import EligibilityVerdict
from andy_trader.store import initialize_database

NOW = "2026-10-17T12:00:00+00:00"


def connection():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_database(db)
    initialize_order_intents(db)
    return db


def add_price(db, instrument, close, *, open_time="2026-10-17T11:00:00+00:00", interval="1h"):
    db.execute(
        """
        INSERT INTO crypto_observations
        (content_hash, instrument, venue, interval, open_time, open, high, low, close,
         volume, degraded, degraded_reason, first_seen_at, last_seen_at, times_seen)
        VALUES (?, ?, 'test', ?, ?, ?, ?, ?, ?, 0, 0, NULL, ?, ?, 1)
        """,
        (f"{instrument}-{open_time}", instrument, interval, open_time, close, close, close, close, NOW, NOW),
    )
    db.commit()


def add_intent(db, **overrides):
    defaults = dict(
        instrument="BTC-USD", predictor="p", side="long", size=100.0,
        stop=95.0, target=110.0, source_decision_id=1, now_iso=NOW, expiry_seconds=300,
    )
    defaults.update(overrides)
    return record_intent(db, new_intent(**defaults))


def eligible_verdict(predictor, instrument):
    return EligibilityVerdict(
        eligible=True, reason="ok", predictor=predictor, instrument=instrument, sample_size=200,
    )


def blocked_verdict(predictor, instrument):
    return EligibilityVerdict(
        eligible=False, reason="does not beat the base rate",
        predictor=predictor, instrument=instrument, sample_size=200,
    )


def test_rejects_when_no_live_price_exists(monkeypatch):
    db = connection()
    monkeypatch.setattr(executor, "evaluate_paper_eligibility", lambda *a, **k: eligible_verdict("p", "BTC-USD"))
    add_intent(db)
    report = executor.run_dry_run(db, now_iso=NOW)
    assert report == {"checked": 1, "would_send": 0, "rejected": 1}
    row = db.execute("SELECT * FROM order_decisions").fetchone()
    assert row["decision"] == "rejected"
    assert "no live price" in row["reason"]


def test_passes_every_check_and_records_would_send(monkeypatch):
    db = connection()
    monkeypatch.setattr(executor, "evaluate_paper_eligibility", lambda *a, **k: eligible_verdict("p", "BTC-USD"))
    add_price(db, "BTC-USD", 100.0)
    add_intent(db)
    report = executor.run_dry_run(db, now_iso=NOW)
    assert report == {"checked": 1, "would_send": 1, "rejected": 0}
    row = db.execute("SELECT * FROM order_decisions").fetchone()
    assert row["decision"] == "would_send"
    assert row["price_checked"] == 100.0


def test_rejects_when_gate_blocks_the_pair(monkeypatch):
    db = connection()
    monkeypatch.setattr(executor, "evaluate_paper_eligibility", lambda *a, **k: blocked_verdict("p", "BTC-USD"))
    add_price(db, "BTC-USD", 100.0)
    add_intent(db)
    report = executor.run_dry_run(db, now_iso=NOW)
    assert report["rejected"] == 1
    row = db.execute("SELECT * FROM order_decisions").fetchone()
    assert "skill gate" in row["reason"]


def test_rejects_leverage_above_1x(monkeypatch):
    db = connection()
    monkeypatch.setattr(executor, "evaluate_paper_eligibility", lambda *a, **k: eligible_verdict("p", "BTC-USD"))
    add_price(db, "BTC-USD", 100.0)
    add_intent(db, leverage=2.0)
    report = executor.run_dry_run(db, now_iso=NOW)
    assert report["rejected"] == 1
    row = db.execute("SELECT * FROM order_decisions").fetchone()
    assert "leverage" in row["reason"]


def test_rejects_size_above_max_per_coin(monkeypatch):
    db = connection()
    monkeypatch.setattr(executor, "evaluate_paper_eligibility", lambda *a, **k: eligible_verdict("p", "BTC-USD"))
    add_price(db, "BTC-USD", 100.0)
    add_intent(db, size=1_000_000.0)
    report = executor.run_dry_run(db, now_iso=NOW)
    assert report["rejected"] == 1
    row = db.execute("SELECT * FROM order_decisions").fetchone()
    assert "max per coin" in row["reason"]


def test_rejects_once_trade_rate_limit_is_hit(monkeypatch):
    db = connection()
    monkeypatch.setattr(executor, "evaluate_paper_eligibility", lambda *a, **k: eligible_verdict("p", "BTC-USD"))
    add_price(db, "BTC-USD", 100.0)
    for _ in range(executor.DEFAULT_MAX_TRADES_PER_HOUR):
        add_intent(db)
        executor.run_dry_run(db, now_iso=NOW)
    add_intent(db)
    report = executor.run_dry_run(db, now_iso=NOW)
    assert report == {"checked": 1, "would_send": 0, "rejected": 1}
    row = db.execute(
        "SELECT reason FROM order_decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "trades" in row["reason"]


def test_never_calls_send_order(monkeypatch):
    db = connection()
    monkeypatch.setattr(executor, "evaluate_paper_eligibility", lambda *a, **k: eligible_verdict("p", "BTC-USD"))
    add_price(db, "BTC-USD", 100.0)
    add_intent(db)

    def fail(*args, **kwargs):
        raise AssertionError("send_order must never be called while ALLOW_LIVE_SEND is off")

    monkeypatch.setattr(executor, "send_order", fail)
    from andy_trader import bybit_sender
    monkeypatch.setattr(bybit_sender, "send_live", fail)
    report = executor.run_dry_run(db, now_iso=NOW)
    assert report["would_send"] == 1


def test_send_order_itself_always_raises():
    with pytest.raises(NotImplementedError):
        executor.send_order(None)


def test_already_decided_intents_are_not_rechecked(monkeypatch):
    db = connection()
    monkeypatch.setattr(executor, "evaluate_paper_eligibility", lambda *a, **k: eligible_verdict("p", "BTC-USD"))
    add_price(db, "BTC-USD", 100.0)
    add_intent(db)
    first = executor.run_dry_run(db, now_iso=NOW)
    second = executor.run_dry_run(db, now_iso=NOW)
    assert first == {"checked": 1, "would_send": 1, "rejected": 0}
    assert second == {"checked": 0, "would_send": 0, "rejected": 0}
    assert db.execute("SELECT COUNT(*) FROM order_decisions").fetchone()[0] == 1


def test_cli_reports_on_an_empty_database(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.setenv("CRYPTO_DB_PATH", str(tmp_path / "trader.db"))
    monkeypatch.setattr(sys, "argv", ["dry_run_executor"])
    executor.main()
    assert "Checked 0 intent(s)" in capsys.readouterr().out
