from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import pytest

from andy_trader import bond_book as bond
from andy_trader.complete_set import CompleteSetError, taker_fee
from andy_trader.store import connect
import run_bond_book as runner

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def market(mid="m1", **changes):
    return {"id": mid, "question": "Bitcoin above 100,000?", "active": True,
            "closed": False, "endDate": (NOW + timedelta(days=1)).isoformat(),
            "outcomes": '["Yes", "No"]', "clobTokenIds": json.dumps([mid + "y", mid + "n"]),
            **changes}


def event(eid="e1", markets=None, **changes):
    return {"id": eid, "active": True, "closed": False, "tags": [{"slug": "crypto"}],
            "markets": markets if markets is not None else [market()], **changes}


def book(price=0.9, size=100):
    return {"asks": [{"price": str(price), "size": str(size)}]}


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "book.db")
    bond.initialize_bond_book(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def network(monkeypatch):
    state = {"events": [event()], "books": {}, "closed": {}}

    def http(url, timeout):
        params = parse_qs(urlparse(url).query)
        if url.startswith(bond.CLOB_BOOK_URL):
            token = params["token_id"][0]
            result = state["books"].get(token, book(0.9 if token.endswith("y") else 0.1))
            if isinstance(result, Exception):
                raise result
            return result
        assert url.startswith(bond.GAMMA_EVENTS_URL)
        if "id" in params:
            return state["closed"].get(params["id"][0], [])
        assert params["tag_id"] == ["21"]
        offset = int(params["offset"][0])
        return state["events"][offset:offset + bond.PAGE_SIZE]

    monkeypatch.setattr(bond, "_http_json", http)
    return state


def positions(db):
    return db.execute("SELECT * FROM bond_book_positions").fetchall()


def test_open_fee_and_no_duplicate(db):
    assert bond.run_once(db, now=NOW) == []
    bond.run_once(db, now=NOW)
    row, = positions(db)
    assert row["side"] == "Yes"
    assert row["fill_price"] == 0.9
    assert row["fee"] == float(taker_fee(100, 0.9))
    assert row["shares"] == 100
    assert row["win"] is None


@pytest.mark.parametrize("price,size,margin,reason", [
    (0.79, 100, 0.015, "ask_too_low"),
    (0.99, 100, 0.015, "ask_too_high"),
    (0.9, 99, 0.015, "thin_book"),
    (0.98, 100, 0.02, "margin_too_small"),
])
def test_skip_reasons(db, network, monkeypatch, price, size, margin, reason):
    monkeypatch.setattr(bond, "MIN_MARGIN", margin)
    network["books"] = {"m1y": book(price, size), "m1n": book(price, size)}
    bond.run_once(db, now=NOW)
    assert positions(db) == []
    assert dict(db.execute("SELECT reason, count FROM bond_book_skips")) == {reason: 2}


@pytest.mark.parametrize("price,band", [(0.8, "0.80 to 0.90"), (0.9, "0.90 to 0.95"),
                                        (0.95, "0.95 to 0.98"), (0.98, "0.95 to 0.98")])
def test_entry_boundaries(db, network, price, band):
    network["books"]["m1y"] = book(price)
    bond.run_once(db, now=NOW)
    assert positions(db)[0]["entry_band"] == band


def test_walks_depth_and_fees_per_level(db, network):
    network["books"]["m1y"] = {"asks": [{"price": "0.96", "size": "60"},
                                           {"price": "0.8", "size": "40"}]}
    bond.run_once(db, now=NOW)
    row, = positions(db)
    assert row["fill_price"] == pytest.approx(0.896)
    assert row["fee"] == float(taker_fee(40, 0.8) + taker_fee(60, 0.96))


def test_limits_and_pagination(db, network, monkeypatch):
    monkeypatch.setattr(bond, "PAGE_SIZE", 7)
    network["events"] = [event(str(i), [market(f"{i}-{j}") for j in range(4)]) for i in range(20)]
    bond.run_once(db, now=NOW)
    assert len(positions(db)) == 30
    assert all(row[0] <= 2 for row in db.execute(
        "SELECT COUNT(*) FROM bond_book_positions GROUP BY event_id"))


@pytest.mark.parametrize("changes", [
    {"active": False}, {"closed": True}, {"question": "Will a crypto company IPO?"},
    {"endDate": NOW.isoformat()}, {"endDate": (NOW + timedelta(days=4)).isoformat()},
    {"outcomes": '["A", "B", "C"]'}, {"clobTokenIds": '["one"]'},
])
def test_universe_market_filters(db, network, changes):
    network["events"] = [event(markets=[market(**changes)])]
    bond.run_once(db, now=NOW)
    assert positions(db) == []


def test_crypto_tag_required(db, network):
    network["events"] = [event(tags=[{"slug": "politics"}])]
    bond.run_once(db, now=NOW)
    assert positions(db) == []


@pytest.mark.parametrize("prices,closed,win", [
    ([1, 0], True, 1), ([0, 1], True, 0), ([0.99, 0.01], True, 1),
    ([0.01, 0.99], True, 0), ([0.98, 0.02], True, None),
    ([0.5, 0.5], True, None), ([1, 0], False, None), ([1, 1], True, None),
    ([0, 0], True, None), ([1], True, None), ([float("nan"), 0], True, None),
])
def test_settlement(db, network, prices, closed, win):
    bond.run_once(db, now=NOW)
    network["closed"]["e1"] = [event(markets=[market(closed=closed, outcomePrices=json.dumps(prices))])]
    bond.run_once(db, now=NOW + timedelta(hours=1))
    row, = positions(db)
    assert row["win"] == win
    if win is None:
        assert row["settled_at"] is None and row["pnl"] is None
    else:
        assert row["pnl"] == pytest.approx(100 * (win - 0.9) - float(taker_fee(100, 0.9)))
        settled_at = row["settled_at"]
        bond.run_once(db, now=NOW + timedelta(hours=2))
        assert positions(db)[0]["settled_at"] == settled_at


def test_http_error_continues_and_is_journaled(db, network, tmp_path, monkeypatch, capsys):
    network["events"] = [event(markets=[market(), market("m2")])]
    network["books"]["m1y"] = CompleteSetError("HTTP 503")
    monkeypatch.setattr(runner, "JOURNAL_PATH", tmp_path / ".bond-book.jsonl")
    monkeypatch.setattr(runner, "run_once", lambda conn: bond.run_once(conn, now=NOW))
    path = db.execute("PRAGMA database_list").fetchone()[2]
    assert runner.main(["--database", path, "--report"]) == 1
    row, = positions(db)
    assert row["market_id"] == "m2"
    entries = [json.loads(line) for line in runner.JOURNAL_PATH.read_text().splitlines()]
    assert entries[0]["market_id"] == "m1"
    assert "HTTP 503" in entries[0]["error"]
    assert "not enough settled positions yet" in capsys.readouterr().out


def test_settlement_error_does_not_block_other_events(db, network):
    network["events"] = [event(), event("e2", [market("m2")])]
    bond.run_once(db, now=NOW)
    network["closed"] = {
        "e1": [event(markets=[market(closed=True, outcomePrices="invalid")])],
        "e2": [event("e2", [market("m2", closed=True, outcomePrices='[1,0]')])],
    }
    errors = bond.run_once(db, now=NOW)
    assert errors[0]["market_id"] == "m1"
    assert [r["win"] for r in positions(db)] == [None, 1]


def test_report(db):
    bond.run_once(db, now=NOW)
    report = bond.format_report(db)
    for _, label in bond.ENTRY_BANDS:
        assert label in report
    assert "Overall" in report
    assert "Open positions: 1" in report
    assert "not enough settled positions yet (0 of 100)" in report
    assert "Verdict:" not in report


@pytest.mark.parametrize("losses,verdict", [(0, "positive edge shown"), (10, "no edge shown")])
def test_verdict_and_loss_statistics(db, losses, verdict):
    for i in range(100):
        bond.open_position(db, str(i), market(str(i)), "Yes", 0.9, 0.63, NOW)
        win = int(i >= losses)
        with db:
            db.execute("UPDATE bond_book_positions SET win=?, pnl=?, settled_at=? WHERE market_id=?",
                       (win, 100 * (win - 0.9) - 0.63, NOW.isoformat(), str(i)))
    report = bond.format_report(db)
    assert f"Verdict: {verdict} (100 settled, including {losses} losses)" in report
    if losses:
        assert "Worst loss: $-90.63" in report
        assert "one average loss: 9.67" in report


@pytest.mark.parametrize("same_side,limit", [(True, 30), (False, 1)])
def test_concurrent_inserts(tmp_path, monkeypatch, same_side, limit):
    path = tmp_path / "concurrent.db"
    with connect(path) as conn:
        bond.initialize_bond_book(conn)
    monkeypatch.setattr(bond, "MAX_OPEN", limit)
    barrier = Barrier(2)

    def insert(index):
        conn = connect(path)
        try:
            barrier.wait(timeout=10)
            bond.open_position(conn, str(index), market("same" if same_side else str(index)),
                               "Yes", 0.9, 0.63, NOW)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(insert, range(2)))
    with connect(path) as conn:
        assert len(positions(conn)) == 1
