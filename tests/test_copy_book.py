from datetime import UTC, datetime
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from andy_trader import copy_book as copy
from andy_trader.complete_set import taker_fee
from andy_trader.store import connect
import run_copy_book as runner

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def make_trade(
    tx="0xtx1",
    asset="tok1",
    cid="0xc1",
    price=0.90,
    ts=1000,
    side="BUY",
    trade_type="TRADE",
    title="Will Bitcoin hit 100k?",
    outcome="Yes",
    size=10,
):
    return {
        "type": trade_type,
        "side": side,
        "transactionHash": tx,
        "asset": asset,
        "conditionId": cid,
        "price": price,
        "timestamp": ts,
        "title": title,
        "outcome": outcome,
        "size": size,
    }


def make_book(price=0.90, size=100):
    return {"asks": [{"price": str(price), "size": str(size)}]}


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "copy_test.db")
    copy.initialize_copy_book(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def network(monkeypatch):
    state = {
        "activity": {
            copy.WALLETS["sharky6999"]: [],
            copy.WALLETS["dromedaryy"]: [],
        },
        "books": {},
        "markets": {},
    }

    class MockPublicReader:
        def get(self, path, **params):
            user = params.get("user")
            return state["activity"].get(user, [])

    def mock_http(url, timeout):
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if url.startswith(copy.CLOB_BOOK_URL):
            tok = params["token_id"][0]
            return state["books"].get(tok, make_book(0.90, 100))
        if url.startswith(copy.GAMMA_MARKETS_URL):
            cids = params.get("condition_ids", [""])[0]
            return state["markets"].get(cids, [])
        raise AssertionError(f"Unexpected url: {url}")

    monkeypatch.setattr(copy, "PublicReader", MockPublicReader)
    monkeypatch.setattr(copy, "_http_json", mock_http)
    return state


def positions(db):
    return db.execute("SELECT * FROM copy_book_positions").fetchall()


def test_first_pass_copies_nothing_and_stores_starting_point(db, network):
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx1", ts=1000),
        make_trade(tx="tx2", ts=900),
    ]
    network["activity"][copy.WALLETS["dromedaryy"]] = [
        make_trade(tx="tx3", ts=800),
    ]

    errors = copy.run_once(db, now=NOW)
    assert errors == []
    assert positions(db) == []

    cursor_sharky = db.execute(
        "SELECT newest_timestamp FROM copy_book_cursor WHERE wallet_name = 'sharky6999'"
    ).fetchone()[0]
    cursor_drom = db.execute(
        "SELECT newest_timestamp FROM copy_book_cursor WHERE wallet_name = 'dromedaryy'"
    ).fetchone()[0]
    assert cursor_sharky == 1000
    assert cursor_drom == 800


def test_parsing_activity_and_no_duplicates(db, network):
    # Pass 1: establishes starting point at 1000
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)
    assert positions(db) == []

    # Pass 2: activity has:
    # - a new BUY (ts 1050)
    # - a SELL (ts 1050)
    # - a non-TRADE (ts 1050)
    # - an old trade (ts 1000)
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_buy", asset="tok1", ts=1050, side="BUY", trade_type="TRADE"),
        make_trade(tx="tx_sell", asset="tok2", ts=1050, side="SELL", trade_type="TRADE"),
        make_trade(tx="tx_transfer", asset="tok3", ts=1050, trade_type="TRANSFER"),
        make_trade(tx="tx_old", asset="tok4", ts=1000, side="BUY", trade_type="TRADE"),
    ]
    copy.run_once(db, now=NOW)

    rows = positions(db)
    assert len(rows) == 1
    assert rows[0]["tx_hash"] == "tx_buy"

    # Pass 3: same activity list again; repeated trade must not be copied twice
    copy.run_once(db, now=NOW)
    rows2 = positions(db)
    assert len(rows2) == 1


def test_fill_rules_fee_and_delay(db, network):
    # First pass
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    # Pass 2: fill at 0.90
    trade_ts = int(NOW.timestamp() - 15)  # 15s delay
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_fill", asset="tok_90", price=0.90, ts=trade_ts)
    ]
    network["books"]["tok_90"] = make_book(price=0.90, size=100)

    copy.run_once(db, now=NOW)
    rows = positions(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["fill_price"] == pytest.approx(0.90)
    assert row["fee"] == pytest.approx(float(taker_fee(100, 0.90)))
    assert row["delay_seconds"] == pytest.approx(15.0)


def test_fill_rules_skips(db, network):
    # Pass 1
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    # 0.99 fill -> fill_too_high
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_high", asset="tok_high", ts=1100)
    ]
    network["books"]["tok_high"] = make_book(price=0.99, size=100)
    copy.run_once(db, now=NOW)
    assert positions(db) == []
    skips = dict(db.execute("SELECT reason, count FROM copy_book_skips WHERE wallet_name='sharky6999'").fetchall())
    assert skips.get("fill_too_high") == 1

    # 0.03 fill -> fill_too_low
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_low", asset="tok_low", ts=1200)
    ]
    network["books"]["tok_low"] = make_book(price=0.03, size=100)
    copy.run_once(db, now=NOW)
    skips = dict(db.execute("SELECT reason, count FROM copy_book_skips WHERE wallet_name='sharky6999'").fetchall())
    assert skips.get("fill_too_low") == 1

    # thin book (< 100 shares) -> thin_book
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_thin", asset="tok_thin", ts=1300)
    ]
    network["books"]["tok_thin"] = make_book(price=0.90, size=50)
    copy.run_once(db, now=NOW)
    skips = dict(db.execute("SELECT reason, count FROM copy_book_skips WHERE wallet_name='sharky6999'").fetchall())
    assert skips.get("thin_book") == 1


def test_61st_open_position_skipped(db, network):
    # Insert 60 open positions for sharky6999
    with db:
        for i in range(60):
            db.execute("""
                INSERT INTO copy_book_positions (
                    wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                    their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "sharky6999", copy.WALLETS["sharky6999"], f"tx_{i}", f"tok_{i}", f"cid_{i}",
                "Question", "Yes", 0.90, 1000 + i, 0.90, 0.63, 100, NOW.isoformat(), 5.0,
            ))
        db.execute(
            "INSERT INTO copy_book_cursor (wallet_name, newest_timestamp) VALUES ('sharky6999', 1100)"
        )

    # Next trade: 61st position
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_61", asset="tok_61", ts=1200)
    ]
    network["books"]["tok_61"] = make_book(price=0.90, size=100)

    copy.run_once(db, now=NOW)
    count = db.execute("SELECT COUNT(*) FROM copy_book_positions WHERE wallet_name='sharky6999'").fetchone()[0]
    assert count == 60

    skips = dict(db.execute("SELECT reason, count FROM copy_book_skips WHERE wallet_name='sharky6999'").fetchall())
    assert skips.get("max_open") == 1


def test_settlement_win_and_loss(db, network):
    # Insert two open positions: one buying Yes, one buying No on cid1
    cid = "0xcid_settle"
    with db:
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "sharky6999", copy.WALLETS["sharky6999"], "tx_win", "tok_y", cid,
            "Will BTC reach 150k?", "Yes", 0.90, 1000, 0.90, 0.63, 100, NOW.isoformat(), 5.0,
        ))
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "dromedaryy", copy.WALLETS["dromedaryy"], "tx_loss", "tok_n", cid,
            "Will BTC reach 150k?", "No", 0.10, 1000, 0.10, 0.63, 100, NOW.isoformat(), 5.0,
        ))

    # Mock Gamma markets endpoint for cid
    network["markets"][cid] = [{
        "id": "m1",
        "conditionId": cid,
        "closed": True,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
    }]

    copy.run_once(db, now=NOW)

    rows = {r["tx_hash"]: r for r in positions(db)}
    win_row = rows["tx_win"]
    loss_row = rows["tx_loss"]

    assert win_row["win"] == 1
    # pnl = 100 * (1 - 0.90) - 0.63 = 9.37
    assert win_row["pnl"] == pytest.approx(9.37)
    assert win_row["settled_at"] is not None

    assert loss_row["win"] == 0
    # pnl = 100 * (0 - 0.10) - 0.63 = -10.63
    assert loss_row["pnl"] == pytest.approx(-10.63)
    assert loss_row["settled_at"] is not None


def test_format_report(db):
    # Add positions and skips
    with db:
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds,
                settled_at, win, pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "sharky6999", copy.WALLETS["sharky6999"], "tx1", "tok1", "c1",
            "Will BTC reach 100k?", "Yes", 0.90, 1000, 0.90, 0.63, 100, NOW.isoformat(), 5.0,
            NOW.isoformat(), 1, 9.37,
        ))
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds,
                settled_at, win, pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "sharky6999", copy.WALLETS["sharky6999"], "tx2", "tok2", "c2",
            "Will ETH reach 5k?", "No", 0.90, 1010, 0.90, 0.63, 100, NOW.isoformat(), 7.0,
            NOW.isoformat(), 0, -90.63,
        ))
        copy.record_skip(db, NOW, "sharky6999", "thin_book")

    rep = copy.format_report(db)
    assert "Copy Book (paper only)" in rep
    assert "Wallet: sharky6999" in rep
    assert "Open positions: 0" in rep
    assert "Settled positions: 2" in rep
    assert "thin_book: 1" in rep
    assert "Worst loss: $-90.63" in rep
    assert "Median delay_seconds: 6.0s" in rep
    assert "Average price difference (fill - their): +0.0000" in rep
    assert "Bootstrap 95 percent interval for average pnl per position:" in rep
    assert "Verdict: not enough settled positions yet (2 of 100)" in rep
    assert "By strategy slice" in rep


def test_runner_journal_and_lock(db, network, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "JOURNAL_PATH", tmp_path / ".copy-book.jsonl")
    monkeypatch.setattr(runner, "LOCK_PATH", tmp_path / ".copy-book.lock")
    monkeypatch.setattr(runner, "run_once", lambda conn: copy.run_once(conn, now=NOW))
    path = db.execute("PRAGMA database_list").fetchone()[2]
    assert runner.main(["--database", path, "--report"]) == 0
    captured = capsys.readouterr().out
    assert "Copy Book (paper only)" in captured
    assert "not enough settled positions yet" in captured

    entries = [json.loads(line) for line in runner.JOURNAL_PATH.read_text().splitlines()]
    pass_line = next(entry for entry in entries if entry["event"] == "pass")
    assert "seconds" in pass_line
    assert "book_reads" in pass_line
    assert "requests" in pass_line
    assert not runner.LOCK_PATH.exists()


def test_cursor_advancement_on_transient_order_book_error(db, network, monkeypatch):
    import urllib.error

    # Pass 1: establishes starting point at 1000
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    # Pass 2: activity has trade 1 (ts 1010, tok1) and trade 2 (ts 1020, tok2)
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx1", asset="tok1", ts=1010),
        make_trade(tx="tx2", asset="tok2", ts=1020),
    ]
    network["books"]["tok1"] = make_book(price=0.90, size=100)

    # Monkeypatch _http_json to raise for tok2
    original_http = copy._http_json

    def flaky_http(url, timeout):
        if "tok2" in url:
            raise urllib.error.URLError("temporary network error")
        return original_http(url, timeout)

    monkeypatch.setattr(copy, "_http_json", flaky_http)

    errors = copy.run_once(db, now=NOW)
    assert len(errors) == 1
    assert "temporary network error" in errors[0]["error"]

    # Position for tx1 opened
    rows = positions(db)
    assert len(rows) == 1
    assert rows[0]["tx_hash"] == "tx1"

    # Cursor must ONLY advance to 1010, NOT 1020!
    cursor_val = db.execute(
        "SELECT newest_timestamp FROM copy_book_cursor WHERE wallet_name = 'sharky6999'"
    ).fetchone()[0]
    assert cursor_val == 1010

    # Pass 3: network restored, provide book for tok2
    monkeypatch.setattr(copy, "_http_json", original_http)
    network["books"]["tok2"] = make_book(price=0.90, size=100)

    errors2 = copy.run_once(db, now=NOW)
    assert errors2 == []

    # tx2 successfully copied, tx1 not duplicated
    rows2 = positions(db)
    assert len(rows2) == 2
    hashes = {r["tx_hash"] for r in rows2}
    assert hashes == {"tx1", "tx2"}

    cursor_val_after = db.execute(
        "SELECT newest_timestamp FROM copy_book_cursor WHERE wallet_name = 'sharky6999'"
    ).fetchone()[0]
    assert cursor_val_after == 1020


def test_settlement_requires_exact_condition_id(db, network):
    cid = "0xcid_exact"
    with db:
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "sharky6999", copy.WALLETS["sharky6999"], "tx_exact", "tok_y", cid,
            "Will BTC reach 150k?", "Yes", 0.90, 1000, 0.90, 0.63, 100, NOW.isoformat(), 5.0,
        ))

    # Single market returned by Gamma, but with a different conditionId
    network["markets"][cid] = [{
        "id": "m_diff",
        "conditionId": "0xunrelated_condition_id",
        "closed": True,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
    }]

    copy.run_once(db, now=NOW)

    row = db.execute("SELECT settled_at, win FROM copy_book_positions WHERE tx_hash='tx_exact'").fetchone()
    assert row[0] is None
    assert row[1] is None

    # Now with the matching conditionId
    network["markets"][cid] = [{
        "id": "m_match",
        "conditionId": cid,
        "closed": True,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
    }]

    copy.run_once(db, now=NOW)

    row_settled = db.execute("SELECT settled_at, win FROM copy_book_positions WHERE tx_hash='tx_exact'").fetchone()
    assert row_settled[0] is not None
    assert row_settled[1] == 1


def test_format_report_price_difference(db):
    with db:
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds,
                settled_at, win, pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "dromedaryy", copy.WALLETS["dromedaryy"], "tx_diff", "tok1", "c1",
            "Will BTC reach 100k?", "Yes", 0.90, 1000, 0.92, 0.63, 100, NOW.isoformat(), 5.0,
            NOW.isoformat(), 1, 7.37,
        ))

    rep = copy.format_report(db)
    assert "Wallet: dromedaryy" in rep
    assert "Average price difference (fill - their): +0.0200" in rep
