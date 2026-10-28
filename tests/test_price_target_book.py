from datetime import UTC, datetime, timedelta
import json
from urllib.parse import parse_qs, urlparse

import pytest

from andy_trader import bond_book as bond
from andy_trader import price_target_book as pt
from andy_trader.complete_set import taker_fee
from andy_trader.store import connect
import run_price_target_book as runner

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def market(mid="m1", **changes):
    return {
        "id": mid,
        "question": "Will Bitcoin reach 150,000?",
        "active": True,
        "closed": False,
        "endDate": (NOW + timedelta(days=10)).isoformat(),
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps([mid + "y", mid + "n"]),
        **changes,
    }


def event(eid="e1", markets=None, **changes):
    return {
        "id": eid,
        "active": True,
        "closed": False,
        "tags": [{"slug": "crypto"}],
        "markets": markets if markets is not None else [market()],
        **changes,
    }


def book(price=0.9, size=100):
    return {"asks": [{"price": str(price), "size": str(size)}]}


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "book.db")
    pt.initialize_price_target_book(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def network(monkeypatch):
    state = {"events": [event()], "books": {}, "closed": {}, "book_calls": []}

    def http(url, timeout):
        params = parse_qs(urlparse(url).query)
        if url.startswith(bond.CLOB_BOOK_URL):
            token = params["token_id"][0]
            state["book_calls"].append(token)
            result = state["books"].get(token, book(0.9 if token.endswith("n") else 0.1))
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
    monkeypatch.setattr(pt, "_http_json", http)
    return state


def positions(db):
    return db.execute(f"SELECT * FROM {pt.POSITIONS_TABLE}").fetchall()


def test_market_filter_accepts_reach_ending_in_10_days():
    m = market(question="Will Bitcoin reach 150,000?", endDate=(NOW + timedelta(days=10)).isoformat())
    assert pt.eligible_market(m, NOW) is True


@pytest.mark.parametrize("question,days", [
    ("Will Bitcoin reach 150,000?", 1),
    ("Will Bitcoin reach 150,000?", 30),
    ("Bitcoin Up or Down on October 5?", 10),
])
def test_market_filter_rejects_non_qualifying(question, days):
    m = market(question=question, endDate=(NOW + timedelta(days=days)).isoformat())
    assert pt.eligible_market(m, NOW) is False


@pytest.mark.parametrize("title", [
    "Will Ethereum dip to 2,000?",
    "Will Solana hit 300 by December?",
])
def test_market_filter_accepts_dip_and_hit(title):
    m = market(question=title, endDate=(NOW + timedelta(days=10)).isoformat())
    assert pt.eligible_market(m, NOW) is True


def test_open_position_at_090_with_fee_and_side(db):
    assert pt.run_once(db, now=NOW) == []
    pt.run_once(db, now=NOW)
    row, = positions(db)
    assert row["side"] == "No"
    assert row["fill_price"] == 0.90
    assert row["fee"] == pytest.approx(float(taker_fee(100, 0.90)))
    assert row["shares"] == 100
    assert row["entry_band"] == "0.90 to 0.96"
    assert row["win"] is None
    assert row["settled_at"] is None
    assert row["pnl"] is None


def test_entry_band_85_to_90(db, network):
    network["books"]["m1n"] = book(0.88, 100)
    pt.run_once(db, now=NOW)
    row, = positions(db)
    assert row["fill_price"] == 0.88
    assert row["entry_band"] == "0.85 to 0.90"


def test_skip_reasons(db, network):
    # Ask 0.97 is skipped (ask_too_high)
    network["books"] = {"m1n": book(0.97, 100)}
    pt.run_once(db, now=NOW)
    assert positions(db) == []
    skips = dict(db.execute(f"SELECT reason, count FROM {pt.SKIPS_TABLE}"))
    assert skips.get("ask_too_high") == 1

    # Thin book is skipped
    network["books"] = {"m1n": book(0.90, 50)}
    pt.run_once(db, now=NOW)
    assert positions(db) == []
    skips = dict(db.execute(f"SELECT reason, count FROM {pt.SKIPS_TABLE}"))
    assert skips.get("thin_book") == 1

    # Ask 0.80 is skipped (ask_too_low)
    network["books"] = {"m1n": book(0.80, 100)}
    pt.run_once(db, now=NOW)
    assert positions(db) == []
    skips = dict(db.execute(f"SELECT reason, count FROM {pt.SKIPS_TABLE}"))
    assert skips.get("ask_too_low") == 1


def test_limits(db, network, monkeypatch):
    monkeypatch.setattr(bond, "PAGE_SIZE", 10)
    # Total limit: at most 30 open positions
    # 35 events with 1 market each
    network["events"] = [event(str(i), [market(f"m-{i}")]) for i in range(35)]
    pt.run_once(db, now=NOW)
    assert len(positions(db)) == 30

    # Event limit: at most 2 open per event
    db.execute(f"DELETE FROM {pt.POSITIONS_TABLE}")
    network["events"] = [event("single_event", [market(f"sub-{j}") for j in range(5)])]
    pt.run_once(db, now=NOW)
    assert len(positions(db)) == 2


def test_settlement_no_win_and_no_loss(db, network):
    # First, open position
    pt.run_once(db, now=NOW)
    assert len(positions(db)) == 1

    # Settle with NO win (outcomePrices [0.01, 0.99] -> No won)
    network["closed"]["e1"] = [event(markets=[market(closed=True, outcomePrices='["0.01", "0.99"]')])]
    pt.run_once(db, now=NOW + timedelta(hours=1))
    row, = positions(db)
    assert row["win"] == 1
    assert row["settled_at"] is not None
    expected_pnl = 100 * (1 - 0.90) - float(taker_fee(100, 0.90))
    assert row["pnl"] == pytest.approx(expected_pnl)

    # Now test NO loss on another market/event
    db.execute(f"DELETE FROM {pt.POSITIONS_TABLE}")
    network["events"] = [event("e2", [market("m2")])]
    pt.run_once(db, now=NOW)
    assert len(positions(db)) == 1

    # Settle with NO loss (outcomePrices [0.99, 0.01] -> Yes won, No lost)
    network["closed"]["e2"] = [event("e2", markets=[market("m2", closed=True, outcomePrices='["0.99", "0.01"]')])]
    pt.run_once(db, now=NOW + timedelta(hours=1))
    row, = positions(db)
    assert row["win"] == 0
    assert row["settled_at"] is not None
    loss_pnl = 100 * (0 - 0.90) - float(taker_fee(100, 0.90))
    assert row["pnl"] == pytest.approx(loss_pnl)


def test_report(db, network):
    pt.run_once(db, now=NOW)
    report = pt.format_report(db)
    assert "Price Target NO Book (paper only)" in report
    assert "Open positions: 1" in report
    assert "Settled positions: 0" in report
    assert "Skipped counts (side checks, including repeat checks):" in report
    assert "0.85 to 0.90" in report
    assert "0.90 to 0.96" in report
    assert "Overall" in report
    assert "not enough settled positions yet (0 of 100)" in report
    assert "emergency stop below 0.70 is not simulated" in report


def test_report_verdict_when_100_settled(db):
    for i in range(100):
        pt.open_position(db, str(i), market(str(i)), "No", 0.90, 0.63, NOW)
        win = 1
        with db:
            db.execute(
                f"UPDATE {pt.POSITIONS_TABLE} SET win=?, pnl=?, settled_at=? WHERE market_id=?",
                (win, 100 * (win - 0.90) - 0.63, NOW.isoformat(), str(i)),
            )
    report = pt.format_report(db)
    assert "Verdict: positive edge shown (100 settled, including 0 losses)" in report


def test_runner_journal_and_lock(db, network, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "JOURNAL_PATH", tmp_path / ".price-target-book.jsonl")
    monkeypatch.setattr(runner, "LOCK_PATH", tmp_path / ".price-target-book.lock")
    monkeypatch.setattr(runner, "run_once", lambda conn: pt.run_once(conn, now=NOW))
    path = db.execute("PRAGMA database_list").fetchone()[2]
    assert runner.main(["--database", path, "--report"]) == 0
    captured = capsys.readouterr().out
    assert "Price Target NO Book (paper only)" in captured
    assert "not enough settled positions yet" in captured

    entries = [json.loads(line) for line in runner.JOURNAL_PATH.read_text().splitlines()]
    pass_line = next(entry for entry in entries if entry["event"] == "pass")
    assert "seconds" in pass_line
    assert pass_line["book_reads"] == 1
    assert not runner.LOCK_PATH.exists()
