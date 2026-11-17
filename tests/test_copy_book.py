from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from andy_trader import copy_book as copy
from andy_trader.complete_set import taker_fee
from andy_trader.store import connect
import run_copy_book as runner

NOW = datetime.fromtimestamp(1300, UTC)


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
        "transaction_hash": tx,
        "token_id": asset,
        "condition_id": cid,
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
        "activity": {address: [] for address in copy.WALLETS.values()},
        "books": {},
        "markets": {},
    }

    def mock_http(url, timeout):
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if parsed.netloc == "data-api.polymarket.com" and parsed.path == "/v2/activity":
            return {
                "data": state["activity"].get(params["user"][0], []),
                "pagination": {"limit": 500, "offset": 0, "has_more": False, "next_cursor": None},
            }
        if url.startswith(copy.CLOB_BOOK_URL):
            tok = params["token_id"][0]
            return state["books"].get(tok, make_book(0.90, 100))
        if url.startswith(copy.GAMMA_MARKETS_URL):
            cids = params.get("condition_ids", [""])[0]
            return state["markets"].get(cids, [])
        raise AssertionError(f"Unexpected url: {url}")

    monkeypatch.setattr("andy_trader.wallet_scout._http_json", mock_http)
    monkeypatch.setattr("andy_trader.wallet_scout.REQUEST_DELAY", 0)
    monkeypatch.setattr(copy, "_http_json", mock_http)
    return state


def positions(db):
    return db.execute("SELECT * FROM copy_book_positions").fetchall()


def test_first_pass_copies_nothing_and_stores_starting_point(db, network):
    assert copy.WALLETS["lpgb"] == "0x1f5f9fa318fbda8350ac143b4df657287df850fa"
    assert copy.WALLETS["bigggggggg"] == "0x511f9c771449162349795d17becf8b37031acbe1"
    assert copy.WALLETS["0x448861155279dbf833d041b963e3ac854599e319"] == "0x448861155279dbf833d041b963e3ac854599e319"
    assert copy.WALLETS["paddaa"] == "0x60a92c8620846d81f5ea17b0564e0d4b7c545a71"
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx1", ts=1000),
        make_trade(tx="tx2", ts=900),
    ]
    network["activity"][copy.WALLETS["dromedaryy"]] = [
        make_trade(tx="tx3", ts=800),
    ]
    network["activity"][copy.WALLETS["lpgb"]] = [
        make_trade(tx="tx4", ts=1100),
        make_trade(tx="tx5", ts=1200),
    ]
    network["activity"][copy.WALLETS["bigggggggg"]] = [
        make_trade(tx="tx6", ts=1300),
    ]
    network["activity"][copy.WALLETS["0x448861155279dbf833d041b963e3ac854599e319"]] = [
        make_trade(tx="tx7", ts=1400),
    ]
    network["activity"][copy.WALLETS["paddaa"]] = [
        make_trade(tx="tx8", ts=1500),
    ]

    errors = copy.run_once(db, now=NOW)
    assert errors == []
    assert positions(db) == []

    cursors = dict(db.execute("SELECT wallet_name, newest_timestamp FROM copy_book_cursor"))
    assert cursors == {
        "sharky6999": 1000, "dromedaryy": 800, "lpgb": 1200, "bigggggggg": 1300,
        "0x448861155279dbf833d041b963e3ac854599e319": 1400, "paddaa": 1500,
    }


def test_parsing_activity_and_no_duplicates(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
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
    assert rows[0]["token_id"] == "tok1"
    assert rows[0]["condition_id"] == "0xc1"
    seen = {tuple(row) for row in db.execute("SELECT tx_hash, token_id FROM copy_book_seen")}
    assert seen == {("tx_buy", "tok1")}
    # Pass 3: same activity list again; repeated trade must not be copied twice
    copy.run_once(db, now=NOW)
    rows2 = positions(db)
    assert len(rows2) == 1


@pytest.mark.parametrize("delay", [25 * 60, 40 * 60])
def test_trade_freshness_limit(db, network, delay, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    with db:
        db.execute(
            "INSERT INTO copy_book_cursor (wallet_name, newest_timestamp) VALUES (?, ?)",
            ("sharky6999", int(NOW.timestamp()) - 10_000),
        )
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_delay", asset="tok_delay", ts=int(NOW.timestamp()) - delay),
    ]

    errors = copy.run_once(db, now=NOW)

    assert errors == []
    skips = dict(db.execute("SELECT reason, count FROM copy_book_skips WHERE wallet_name='sharky6999'"))
    seen = {tuple(row) for row in db.execute("SELECT tx_hash, token_id FROM copy_book_seen")}
    if delay > copy.MAX_DELAY_SECONDS:
        assert positions(db) == []
        assert skips == {"stale": 1}
        assert seen == {("tx_delay", "tok_delay")}
        assert copy.BOOK_READS == 0
    else:
        row, = positions(db)
        assert row["tx_hash"] == "tx_delay"
        assert row["delay_seconds"] == delay
        assert skips == {}
        assert seen == {("tx_delay", "tok_delay")}


def test_stale_trade_counted_once_across_passes(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    with db:
        db.execute(
            "INSERT INTO copy_book_cursor (wallet_name, newest_timestamp) VALUES (?, ?)",
            ("sharky6999", int(NOW.timestamp()) - 10_000),
        )
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_stale", asset="tok_stale", ts=int(NOW.timestamp()) - 40 * 60),
    ]

    copy.run_once(db, now=NOW)
    copy.run_once(db, now=NOW)

    skips = dict(db.execute("SELECT reason, count FROM copy_book_skips WHERE wallet_name='sharky6999'"))
    assert skips == {"stale": 1}
    assert positions(db) == []


def test_fill_rules_fee_and_delay(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
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


def test_fill_rules_skips(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
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


def test_61st_open_position_skipped(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
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


def test_second_buy_on_same_token_skipped_as_token_cap(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx_first", asset="tok1", ts=1050)]
    copy.run_once(db, now=NOW)
    row, = positions(db)
    assert row["tx_hash"] == "tx_first"

    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx_second", asset="tok1", ts=1100)]
    copy.run_once(db, now=NOW)
    assert copy.BOOK_READS == 0
    copy.run_once(db, now=NOW)

    row, = positions(db)
    assert row["tx_hash"] == "tx_first"
    skips = dict(db.execute("SELECT reason, count FROM copy_book_skips WHERE wallet_name='sharky6999'").fetchall())
    assert skips == {"token_cap": 1}


def test_same_token_for_different_wallet_is_still_copied(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    for wallet_name in ("sharky6999", "dromedaryy"):
        network["activity"][copy.WALLETS[wallet_name]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx_sharky", asset="tok1", ts=1050)]
    network["activity"][copy.WALLETS["dromedaryy"]] = [make_trade(tx="tx_dromedaryy", asset="tok1", ts=1060)]
    copy.run_once(db, now=NOW)

    copied = {(row["wallet_name"], row["token_id"]) for row in positions(db)}
    assert copied == {("sharky6999", "tok1"), ("dromedaryy", "tok1")}
    assert db.execute("SELECT COUNT(*) FROM copy_book_skips").fetchone()[0] == 0


def test_fetch_markets_requests_closed_markets(monkeypatch):
    cid = "0x51b660c5af128e515194be6da1d857f9a1ec758d0094956462c5d32d868e1096"
    http = Mock(return_value=[])
    monkeypatch.setattr(copy, "_http_json", http)

    copy.fetch_markets(cid)

    params = parse_qs(urlparse(http.call_args.args[0]).query)
    assert params["condition_ids"] == [cid]
    assert params["closed"] == ["true"]


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


@pytest.mark.parametrize("outcome, win, pnl", [
    ("No", 1, 9.37),
    ("Yes", 0, -90.63),
])
def test_settle_positions_with_resolved_gamma_market(db, network, outcome, win, pnl):
    cid = "0xcid_resolved"
    with db:
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "sharky6999", copy.WALLETS["sharky6999"], "tx_resolved", "tok1", cid,
            "Will BTC dip to 78k?", outcome, 0.90, 1000, 0.90, 0.63, 100, NOW.isoformat(), 5.0,
        ))
    network["markets"][cid] = [{
        "conditionId": cid,
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0", "1"]',
    }]
    errors = []

    copy.settle_positions(db, NOW, errors)

    row = positions(db)[0]
    assert errors == []
    assert row["settled_at"] == NOW.isoformat()
    assert row["win"] == win
    assert row["pnl"] == pytest.approx(pnl)


@pytest.mark.parametrize("markets", [
    [],
    [{
        "conditionId": "0xcid_unresolved",
        "closed": False,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.5", "0.5"]',
    }],
    [{
        "conditionId": "0xcid_unresolved",
        "closed": True,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.5", "0.5"]',
    }],
], ids=["empty", "open", "closed-tied"])
def test_settle_positions_leaves_unresolved_positions_open(db, network, markets):
    cid = "0xcid_unresolved"
    with db:
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "sharky6999", copy.WALLETS["sharky6999"], "tx_unresolved", "tok1", cid,
            "Will BTC dip to 78k?", "Yes", 0.90, 1000, 0.90, 0.63, 100, NOW.isoformat(), 5.0,
        ))
    network["markets"][cid] = markets
    errors = []

    copy.settle_positions(db, NOW, errors)

    row = positions(db)[0]
    assert errors == []
    assert row["settled_at"] is None
    assert row["win"] is None
    assert row["pnl"] is None


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
        copy.record_skip(db, NOW, "sharky6999", "stale")
        copy.record_skip(db, NOW, "sharky6999", "token_cap")

    rep = copy.format_report(db)
    assert "Copy Book (paper only)" in rep
    assert {line for line in rep.splitlines() if line.startswith("Wallet: ")} == {
        "Wallet: sharky6999", "Wallet: dromedaryy", "Wallet: lpgb", "Wallet: bigggggggg",
        "Wallet: 0x448861155279dbf833d041b963e3ac854599e319", "Wallet: paddaa",
    }
    assert "Open positions: 0" in rep
    assert "Settled positions: 2" in rep
    assert "thin_book: 1" in rep
    assert "stale: 1" in rep
    assert "token_cap: 1" in rep
    assert "Worst loss: $-90.63" in rep
    assert "Worst loss: $0.00" not in rep
    assert "Worst loss: n/a" in rep
    assert "Median delay_seconds: 6.0s" in rep
    assert "Average price difference (fill - their): +0.0000" in rep
    assert "Bootstrap 95 percent interval for average pnl per position:" in rep
    assert "Verdict: not enough settled positions yet (2 of 100)" in rep
    assert "By strategy slice" in rep


def test_format_report_wallet_row_has_wallet_heading(db):
    rep = copy.format_report(db)
    lines = rep.splitlines()
    wallet_row = lines.index("sharky6999 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | 0 | n/a")
    assert lines[wallet_row - 1].startswith("Wallet | Settled | Wins |")
    assert lines[lines.index("By strategy slice") + 1].startswith("Slice | Settled | Wins |")


def insert_settled(db, wallet_name, tx, cid, pnl):
    with db:
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds,
                settled_at, win, pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            wallet_name, copy.WALLETS[wallet_name], tx, "tok1", cid,
            "Will BTC reach 100k?", "Yes", 0.50, 1000, 0.50, 0.0, 100, NOW.isoformat(), 5.0,
            NOW.isoformat(), int(pnl > 0), pnl,
        ))


def wallet_row(db, wallet_name):
    lines = copy.format_report(db).splitlines()
    return next(line for line in lines if line.startswith(f"{wallet_name} | ")).split(" | ")


def test_positions_on_same_condition_id_are_one_event(db):
    insert_settled(db, "dromedaryy", "tx1", "c1", 50.0)
    insert_settled(db, "dromedaryy", "tx2", "c1", 50.0)
    insert_settled(db, "dromedaryy", "tx3", "c2", -50.0)
    insert_settled(db, "sharky6999", "tx4", "c3", 7.0)

    assert copy.event_totals(db, "dromedaryy") == [100.0, -50.0]

    low, high = copy.compute_bootstrap_interval([100.0, -50.0])
    row = wallet_row(db, "dromedaryy")
    assert row[1] == "3"
    assert row[9:] == ["2", f"[${low:+.2f}, ${high:+.2f}]"]


def test_wallet_with_one_event_shows_no_event_interval(db):
    insert_settled(db, "dromedaryy", "tx1", "c1", 50.0)
    insert_settled(db, "dromedaryy", "tx2", "c1", -50.0)

    row = wallet_row(db, "dromedaryy")
    assert row[1] == "2"
    assert row[9:] == ["1", "n/a"]


def test_report_has_events_column_after_position_columns(db):
    lines = copy.format_report(db).splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("Wallet | "))
    assert lines[header].split(" | ")[1] == "Settled"
    assert lines[header].split(" | ")[9] == "Events"
    assert lines[header + 2] == (
        "Note: Events group positions that settled on the same market, so Events is the honest sample size."
    )


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
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
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

    # The failed trade is not marked as seen, so it is tried again.
    seen = {tuple(r) for r in db.execute("SELECT tx_hash, token_id FROM copy_book_seen").fetchall()}
    assert seen == {("tx1", "tok1")}

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

    seen_after = {tuple(r) for r in db.execute("SELECT tx_hash, token_id FROM copy_book_seen").fetchall()}
    assert seen_after == {("tx1", "tok1"), ("tx2", "tok2")}


def test_late_trade_with_older_timestamp_is_copied(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    # Trade B (ts 1100) is seen first; trade A (ts 1050) only shows up in a later pass.
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx_b", asset="tok2", ts=1100)]
    copy.run_once(db, now=NOW)
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_b", asset="tok2", ts=1100),
        make_trade(tx="tx_a", asset="tok1", ts=1050),
    ]
    copy.run_once(db, now=NOW)

    assert {r["tx_hash"] for r in positions(db)} == {"tx_a", "tx_b"}


def test_skipped_trade_is_not_counted_twice(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx_hi", asset="tok_hi", ts=1050)]
    network["books"]["tok_hi"] = make_book(price=0.99, size=100)
    copy.run_once(db, now=NOW)
    copy.run_once(db, now=NOW)

    skips = dict(db.execute("SELECT reason, count FROM copy_book_skips WHERE wallet_name='sharky6999'").fetchall())
    assert skips == {"fill_too_high": 1}
    assert positions(db) == []


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


def test_opened_at_and_delay_capture_after_order_book_read(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    # Establish starting point
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    trade_ts = int(NOW.timestamp())  # their_timestamp
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_time_check", asset="tok_timed", price=0.90, ts=trade_ts)
    ]
    network["books"]["tok_timed"] = make_book(price=0.90, size=100)

    # Time progresses: pass starts at NOW, but order book read finishes 8 seconds later
    fill_time = NOW + timedelta(seconds=8)
    copy.run_once(db, now=NOW, clock=lambda: fill_time)

    rows = positions(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["opened_at"] == fill_time.isoformat()
    assert row["delay_seconds"] == pytest.approx(8.0)


def test_unseen_trades_at_cursor_timestamp_accepted(db, network, monkeypatch):
    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    # Pass 1: establishes starting point at 1000
    network["activity"][copy.WALLETS["sharky6999"]] = [make_trade(tx="tx0", ts=1000)]
    copy.run_once(db, now=NOW)

    # Pass 2: activity has trade A at ts 1050
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_a", asset="tok1", ts=1050)
    ]
    network["books"]["tok1"] = make_book(price=0.90, size=100)
    copy.run_once(db, now=NOW)

    rows = positions(db)
    assert len(rows) == 1
    assert rows[0]["tx_hash"] == "tx_a"

    # Pass 3: activity now includes trade B also at ts 1050
    network["activity"][copy.WALLETS["sharky6999"]] = [
        make_trade(tx="tx_a", asset="tok1", ts=1050),
        make_trade(tx="tx_b", asset="tok2", ts=1050),
    ]
    network["books"]["tok2"] = make_book(price=0.90, size=100)
    copy.run_once(db, now=NOW)

    rows_after = positions(db)
    assert len(rows_after) == 2
    hashes = {r["tx_hash"] for r in rows_after}
    assert hashes == {"tx_a", "tx_b"}

    # Pass 4: repeat pass with same activity; neither trade is copied again
    copy.run_once(db, now=NOW)
    rows_final = positions(db)
    assert len(rows_final) == 2


@pytest.mark.parametrize("resume_delay", [0, copy.MAX_DELAY_SECONDS + 1])
def test_paused_wallet_skips_fresh_buy_and_resumes_without_backfill(
    db, network, monkeypatch, resume_delay,
):
    address = copy.WALLETS["sharky6999"]
    network["activity"][address] = [make_trade(tx="tx_start", ts=1)]
    copy.run_once(db, now=NOW)
    network["activity"][address] = [make_trade(tx="tx_paused", ts=int(NOW.timestamp()))]

    assert copy.run_once(db, now=NOW) == []
    assert positions(db) == []
    assert copy.BOOK_READS == 0

    monkeypatch.setattr(copy, "PAUSED_WALLETS", set())
    resumed_at = NOW + timedelta(seconds=resume_delay)
    network["activity"][address].extend([
        make_trade(tx="tx_stale", asset="tok_stale", ts=int(NOW.timestamp()) - copy.MAX_DELAY_SECONDS - 1),
        make_trade(tx="tx_resumed", asset="tok_resumed", ts=int(resumed_at.timestamp())),
    ])
    assert copy.run_once(db, now=resumed_at) == []
    row, = positions(db)
    assert row["tx_hash"] == "tx_resumed"
    assert row["wallet_name"] == "sharky6999"


def test_paused_wallet_still_settles_and_reports(db, network):
    cid = "0xcid_paused"
    with db:
        db.execute("""
            INSERT INTO copy_book_positions (
                wallet_name, wallet, tx_hash, token_id, condition_id, title, outcome,
                their_price, their_timestamp, fill_price, fee, shares, opened_at, delay_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "sharky6999", copy.WALLETS["sharky6999"], "tx_open", "tok_open", cid,
            "Will BTC reach 150k?", "No", 0.90, 1000, 0.90, 0.63, 100, NOW.isoformat(), 5.0,
        ))
    network["markets"][cid] = [{
        "conditionId": cid,
        "closed": True,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0", "1"]',
    }]

    assert copy.run_once(db, now=NOW) == []
    row, = positions(db)
    assert row["settled_at"] == NOW.isoformat()
    assert row["win"] == 1
    assert row["pnl"] == pytest.approx(9.37)
    assert "Wallet: sharky6999\nOpen positions: 0\nSettled positions: 1" in copy.format_report(db)


def test_unpaused_wallet_still_copies(db, network):
    address = copy.WALLETS["dromedaryy"]
    network["activity"][address] = [make_trade(tx="tx_start", ts=1000)]
    copy.run_once(db, now=NOW)
    network["activity"][address] = [make_trade(tx="tx_active", ts=int(NOW.timestamp()))]

    assert copy.run_once(db, now=NOW) == []
    row, = positions(db)
    assert row["wallet_name"] == "dromedaryy"
    assert row["tx_hash"] == "tx_active"
    assert row["fill_price"] == pytest.approx(0.90)
