from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import sqlite3
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
    state = {"events": [event()], "books": {}, "closed": {}, "book_calls": []}

    def http(url, timeout):
        params = parse_qs(urlparse(url).query)
        if url.startswith(bond.CLOB_BOOK_URL):
            token = params["token_id"][0]
            state["book_calls"].append(token)
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
    (0.96, 100, 0.015, "ask_too_high"),
    (0.9, 99, 0.015, "thin_book"),
    (0.95, 100, 0.06, "margin_too_small"),
])
def test_skip_reasons(db, network, monkeypatch, price, size, margin, reason):
    monkeypatch.setattr(bond, "MIN_MARGIN", margin)
    network["books"] = {"m1y": book(price, size), "m1n": book(price, size)}
    bond.run_once(db, now=NOW)
    assert positions(db) == []
    assert dict(db.execute("SELECT reason, count FROM bond_book_skips")) == {reason: 2}


@pytest.mark.parametrize("price,band", [(0.9, "0.90 to 0.95"), (0.95, "0.95 to 0.98")])
def test_entry_boundaries(db, network, price, band):
    network["books"]["m1y"] = book(price)
    bond.run_once(db, now=NOW)
    assert positions(db)[0]["entry_band"] == band


def test_ask_below_ninety_is_skipped(db, network):
    network["books"] = {"m1y": book(0.85), "m1n": book(0.85)}
    bond.run_once(db, now=NOW)
    assert positions(db) == []
    assert dict(db.execute("SELECT reason, count FROM bond_book_skips")) == {"ask_too_low": 2}


def test_gamma_price_below_prefilter_skips_book(db, network):
    network["events"] = [event(markets=[market(outcomePrices='["0.40", "0.85"]')])]
    bond.run_once(db, now=NOW)
    assert "m1y" not in network["book_calls"]
    assert "m1n" in network["book_calls"]
    assert dict(db.execute("SELECT reason, count FROM bond_book_skips")) == {"ask_too_low": 2}


def test_gamma_price_above_prefilter_still_reads_book(db, network):
    network["events"] = [event(markets=[market(outcomePrices='["0.85", "0.15"]')])]
    bond.run_once(db, now=NOW)
    assert "m1y" in network["book_calls"]
    row, = positions(db)
    assert row["side"] == "Yes"
    assert row["fill_price"] == 0.9


@pytest.mark.parametrize("changes", [
    {},
    {"outcomePrices": "invalid"},
    {"outcomePrices": '["bad", "bad"]'},
])
def test_missing_or_invalid_gamma_price_reads_book(db, network, changes):
    network["events"] = [event(markets=[market(**changes)])]
    bond.run_once(db, now=NOW)
    assert "m1y" in network["book_calls"]
    row, = positions(db)
    assert row["side"] == "Yes"


def test_pass_journal_has_seconds_and_book_reads(db, network, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "JOURNAL_PATH", tmp_path / ".bond-book.jsonl")
    network["events"] = [event(markets=[market(endDate=(datetime.now(UTC) + timedelta(days=1)).isoformat())])]
    path = db.execute("PRAGMA database_list").fetchone()[2]
    assert runner.main(["--database", path]) == 0
    entries = [json.loads(line) for line in runner.JOURNAL_PATH.read_text().splitlines()]
    pass_line = next(entry for entry in entries if entry["event"] == "pass")
    assert "seconds" in pass_line
    assert pass_line["book_reads"] == 2


def test_walks_depth_and_fees_per_level(db, network):
    network["books"]["m1y"] = {"asks": [{"price": "0.96", "size": "60"},
                                           {"price": "0.9", "size": "40"}]}
    bond.run_once(db, now=NOW)
    row, = positions(db)
    assert row["fill_price"] == pytest.approx(0.936)
    assert row["fee"] == float(taker_fee(40, 0.9) + taker_fee(60, 0.96))


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


def test_active_events_walks_consecutive_windows(monkeypatch):
    calls = []

    def fake_fetch(**params):
        calls.append(params)
        return []

    monkeypatch.setattr(bond, "fetch_events", fake_fetch)
    assert list(bond.active_events(NOW)) == []
    starts = [datetime.fromisoformat(call["end_date_min"]) for call in calls]
    ends = [datetime.fromisoformat(call["end_date_max"]) for call in calls]
    assert starts[0] == NOW
    assert ends[-1] == NOW + timedelta(days=bond.MAX_DAYS)
    assert all(end == next_start for end, next_start in zip(ends, starts[1:]))
    assert all(end - start == timedelta(hours=bond.WINDOW_HOURS)
               for start, end in zip(starts, ends))


def test_event_in_two_adjacent_windows_yielded_once(monkeypatch):
    same = event()
    first_two = {NOW.isoformat(), (NOW + timedelta(hours=bond.WINDOW_HOURS)).isoformat()}

    def fake_fetch(**params):
        return [same] if params["end_date_min"] in first_two else []

    monkeypatch.setattr(bond, "fetch_events", fake_fetch)
    assert [e["id"] for e in bond.active_events(NOW)] == ["e1"]


def test_paging_inside_a_window_follows_offset(monkeypatch):
    monkeypatch.setattr(bond, "PAGE_SIZE", 2)
    calls = []

    def fake_fetch(**params):
        calls.append(params)
        if params["end_date_min"] != NOW.isoformat():
            return []
        return [event("e1"), event("e2")] if params["offset"] == 0 else [event("e3")]

    monkeypatch.setattr(bond, "fetch_events", fake_fetch)
    assert [e["id"] for e in bond.active_events(NOW)] == ["e1", "e2", "e3"]
    offsets = [c["offset"] for c in calls if c["end_date_min"] == NOW.isoformat()]
    assert offsets == [0, 2]


def test_failing_window_is_recorded_and_later_windows_read(db, monkeypatch):
    second = (NOW + timedelta(hours=bond.WINDOW_HOURS)).isoformat()

    def fake_fetch(**params):
        if params["end_date_min"] == NOW.isoformat():
            raise CompleteSetError("HTTP 422")
        return [event()] if params["end_date_min"] == second else []

    monkeypatch.setattr(bond, "fetch_events", fake_fetch)
    errors = bond.run_once(db, now=NOW)
    assert errors == [{"error": "HTTP 422"}]
    assert len(positions(db)) == 1


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


@pytest.mark.parametrize("title,side,expected", [
    ("Will Bitcoin reach 150,000?", "No", "price_target_no"),
    ("Will Bitcoin reach 150,000?", "Yes", "price_target_yes"),
    ("Bitcoin Up or Down on October 5?", "No", "up_down"),
    ("Bitcoin above 100,000?", "Yes", "above_below"),
    ("Will a crypto company IPO?", "Yes", "other"),
])
def test_strategy_slice(title, side, expected):
    assert bond.strategy_slice(title, side) == expected


def test_report_strategy_slices(db):
    reach = "Will Bitcoin reach 150,000?"
    up_down = "Bitcoin Up or Down on October 5?"
    bond.open_position(db, "e1", market("m1", question=reach), "No", 0.9, 0.63, NOW)
    bond.open_position(db, "e2", market("m2", question=reach), "No", 0.9, 0.63, NOW)
    bond.open_position(db, "e3", market("m3", question=up_down), "No", 0.9, 0.63, NOW)
    bond.open_position(db, "e4", market("m4"), "Yes", 0.9, 0.63, NOW)
    for market_id, win in (("m1", 1), ("m2", 0), ("m3", 1)):
        with db:
            db.execute(
                "UPDATE bond_book_positions SET win=?, pnl=?, settled_at=? WHERE market_id=?",
                (win, 100 * (win - 0.9) - 0.63, NOW.isoformat(), market_id),
            )
    report = bond.format_report(db)
    assert "By strategy slice" in report
    assert ("A slice needs 100 settled positions before it counts; "
            "the slice with the most settled positions so far is shown first.") in report
    assert ("price_target_no | 2 | 1 | 0.9000 | 50.00% | -40.00% | "
            "$-0.4063 | $+9.37 | $-90.63") in report
    assert ("up_down | 1 | 1 | 0.9000 | 100.00% | +10.00% | "
            "$+0.0937 | $+9.37 | n/a") in report
    assert "  worst loss: $-90.63" in report
    assert "  worst loss: n/a" in report
    assert report.index("price_target_no |") < report.index("up_down |")
    assert "above_below |" not in report
    assert "Open positions by slice: price_target_no 0, up_down 0, above_below 1" in report
    assert ("Entry band | Settled | Wins | Average entry price | Win rate | Gap before fees | "
            "Average pnl/share after fees | Average win | Average loss") in report
    assert "0.90 to 0.95 | 3 | 2 | 0.9000 | 66.67% | -23.33%" in report
    assert "Overall | 3 | 2 | 0.9000 | 66.67% | -23.33%" in report


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
    else:
        assert "Worst loss: n/a" in report
        assert "Worst loss: $0.00" not in report
        assert "Overall | 100 | 100 | 0.9000 | 100.00% | +10.00% | $+0.0937 | $+9.37 | n/a" in report


def test_report_row_with_no_wins_shows_average_win_as_not_available():
    rows = [{"win": 0, "fill_price": 0.9, "pnl": -90.63, "shares": 100}]
    assert bond.report_row("Overall", rows) == (
        "Overall | 1 | 0 | 0.9000 | 0.00% | -90.00% | $-0.9063 | n/a | $-90.63"
    )


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


def test_paused_slice_skips_up_down_market_side(db, network):
    network["events"] = [event(markets=[market("m1", question="Bitcoin Up or Down on October 28?")])]
    assert bond.run_once(db, now=NOW) == []
    assert network["book_calls"] == []
    assert positions(db) == []
    assert dict(db.execute("SELECT reason, count FROM bond_book_skips")) == {"slice_paused": 2}
    assert "slice_paused: 2" in bond.format_report(db)


def test_above_below_market_side_still_opens_position(db, network):
    question = "Bitcoin above 100,000?"
    assert bond.strategy_slice(question, "Yes") == bond.ABOVE_BELOW
    network["events"] = [event(markets=[market("m1", question=question)])]
    assert bond.run_once(db, now=NOW) == []
    assert "m1y" in network["book_calls"]
    row, = positions(db)
    assert row["side"] == "Yes"
    assert row["market_id"] == "m1"


def test_already_open_up_down_position_still_settles(db, network):
    up_down_market = market("m1", question="Bitcoin Up or Down on October 28?")
    bond.open_position(db, "e1", up_down_market, "Yes", 0.9, 0.63, NOW)
    network["closed"]["e1"] = [event("e1", markets=[market("m1", question="Bitcoin Up or Down on October 28?",
                                                           closed=True, outcomePrices='[1, 0]')])]
    bond.run_once(db, now=NOW + timedelta(hours=1))
    row, = positions(db)
    assert row["win"] == 1
    assert row["settled_at"] is not None


def test_empty_paused_slices_allows_up_down_entry(db, network, monkeypatch):
    monkeypatch.setattr(bond, "PAUSED_SLICES", ())
    network["events"] = [event(markets=[market("m1", question="Bitcoin Up or Down on October 28?")])]
    assert bond.run_once(db, now=NOW) == []
    assert "m1y" in network["book_calls"]
    row, = positions(db)
    assert row["side"] == "Yes"
    assert row["market_id"] == "m1"


def add_cohort_position(db, mid, *, title="Will Bitcoin reach 150,000?", side="No",
                        opened_at="2026-10-30T00:00:00+00:00", price=0.9, fee=0.5, win=None):
    band = next(label for upper, label in bond.ENTRY_BANDS if price < upper)
    settled_at = "2026-11-01T00:00:00+00:00" if win is not None else None
    pnl = 100 * (win - price) - fee if win is not None else None
    with db:
        db.execute("""
            INSERT INTO bond_book_positions
            (market_id, event_id, title, side, entry_band, fill_price, fee, shares,
             opened_at, ends_at, settled_at, win, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (mid, mid, title, side, band, price, fee, 100, opened_at,
              "2026-10-31T00:00:00+00:00", settled_at, win, pnl))


def block_cohort_side_effects(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Cohort reporting must not collect, initialize, settle, or journal")

    for name in ("connect", "default_database_path", "run_once", "journal"):
        monkeypatch.setattr(runner, name, unexpected)
    for name in ("initialize_bond_book", "settle_positions", "_http_json"):
        monkeypatch.setattr(bond, name, unexpected)


def test_cohort_report_counts_net_pnl_and_intervals(db):
    add_cohort_position(db, "win", win=1)
    add_cohort_position(db, "loss", win=0)
    add_cohort_position(db, "open")
    add_cohort_position(db, "boundary", title="Bitcoin above 100,000?", side="Yes", price=0.95, win=1)
    with db:
        db.execute("UPDATE bond_book_positions SET pnl=999 WHERE market_id='open'")
    before = bond.format_report(db)
    report = bond.format_cohort_report(db, "2026-10-30")
    assert "Overall | 3 | 1 | $-76.50 | $-25.50 | [$-90.50, $+9.50]" in report
    assert "price_target_no | 2 | 1 | $-81.00 | $-40.50 | [$-90.50, $+9.50]" in report
    assert "above_below | 1 | 0 | $+4.50 | $+4.50 | [$+4.50, $+4.50]" in report
    assert "0.90 to 0.95 | 2 | 1 | $-81.00 | $-40.50 | [$-90.50, $+9.50]" in report
    assert "0.95 to 0.98 | 1 | 0 | $+4.50 | $+4.50 | [$+4.50, $+4.50]" in report
    assert "price_target_no | 0.90 to 0.95 | 2 | 1 | $-81.00" in report
    assert "95 percent bootstrap interval of average pnl per settled position" in report
    assert "not proof of an edge" in report
    assert report == bond.format_cohort_report(db, "2026-10-30")
    assert bond.format_report(db) == before


def test_cohort_report_excludes_old_paused_and_outside_current_rules(db):
    add_cohort_position(db, "old", opened_at="2026-10-29T23:59:59.999999Z", win=0)
    add_cohort_position(db, "old-open", opened_at="2026-10-29T23:59:59Z")
    add_cohort_position(db, "paused", title="Bitcoin Up or Down?", side="Up", win=0)
    add_cohort_position(db, "paused-open", title="Bitcoin Up or Down?", side="Down")
    add_cohort_position(db, "low", price=0.89, win=0)
    add_cohort_position(db, "high", price=0.96, win=0)
    add_cohort_position(db, "margin", price=0.95, fee=4, win=0)
    add_cohort_position(db, "current", win=1)
    report = bond.format_cohort_report(db, "2026-10-30")
    assert "Overall | 1 | 0 | $+9.50 | $+9.50 | [$+9.50, $+9.50]" in report
    assert "up_down |" not in report


@pytest.mark.parametrize("opened_at,included", [
    ("2026-10-29T23:59:59.999999+00:00", False),
    ("2026-10-30T00:00:00+00:00", True),
    ("2026-10-30T00:00:00Z", True),
    ("2026-10-30T00:00:00", True),
    ("2026-10-30T07:59:59+08:00", False),
    ("2026-10-30T08:00:00+08:00", True),
    ("2026-10-29T20:00:00-04:00", True),
])
def test_cohort_report_uses_inclusive_utc_entry_date(db, opened_at, included):
    add_cohort_position(db, "sample", opened_at=opened_at)
    report = bond.format_cohort_report(db, "2026-10-30")
    assert f"Overall | 0 | {int(included)} | n/a | n/a | n/a" in report


@pytest.mark.parametrize("start_date", ["", "bad", "2026-02-30", "2026-13-01", "20261030",
                                        "2026-1-1", "2026-10-30T00:00:00Z"])
def test_cohort_report_rejects_invalid_start_date(db, start_date):
    with pytest.raises(ValueError):
        bond.format_cohort_report(db, start_date)


@pytest.mark.parametrize("open_count", [0, 1])
def test_cohort_report_without_settled_positions(db, open_count):
    if open_count:
        add_cohort_position(db, "open", side="Yes")
    report = bond.format_cohort_report(db, "2026-10-30")
    assert f"Overall | 0 | {open_count} | n/a | n/a | n/a" in report
    assert f"price_target_yes | 0 | {open_count} | n/a | n/a | n/a" in report
    assert ("No positions match this cohort." in report) == (open_count == 0)
    assert "positive edge shown" not in report
    assert "[$+0.00, $+0.00]" not in report


def test_cohort_report_only_reads_positions(db):
    add_cohort_position(db, "sample", win=1)
    with db:
        db.execute("DROP TABLE bond_book_skips")
    db.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK
                      if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ) else sqlite3.SQLITE_DENY)
    try:
        report = bond.format_cohort_report(db, "2026-10-30")
    finally:
        db.set_authorizer(None)
    assert "Overall | 1 | 0" in report


def test_cohort_cli_opens_existing_database_read_only(db, tmp_path, monkeypatch, capsys):
    add_cohort_position(db, "sample", win=1)
    path = tmp_path / "paper #1.db"
    destination = sqlite3.connect(path)
    db.backup(destination)
    destination.close()
    before = path.read_bytes()
    block_cohort_side_effects(monkeypatch)
    original_connect = sqlite3.connect
    connections = []

    def read_only_connect(database, **kwargs):
        assert database == path.resolve().as_uri() + "?mode=ro"
        assert kwargs == {"uri": True}
        connection = original_connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM bond_book_positions")
        connections.append(connection)
        return connection

    monkeypatch.setattr(runner.sqlite3, "connect", read_only_connect)
    assert runner.main(["--database", str(path), "--cohort-since", "2026-10-30"]) == 0
    output = capsys.readouterr()
    assert "Overall | 1 | 0 | $+9.50" in output.out
    assert output.err == ""
    assert path.read_bytes() == before
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_cohort_cli_requires_explicit_database(monkeypatch, capsys):
    block_cohort_side_effects(monkeypatch)
    with pytest.raises(SystemExit) as error:
        runner.main(["--cohort-since", "2026-10-30"])
    assert error.value.code == 2
    assert "requires --database" in capsys.readouterr().err


def test_cohort_cli_cannot_be_combined_with_normal_report(monkeypatch):
    block_cohort_side_effects(monkeypatch)
    with pytest.raises(SystemExit) as error:
        runner.main(["--cohort-since", "2026-10-30", "--report"])
    assert error.value.code == 2


def test_cohort_cli_does_not_create_missing_database(tmp_path, monkeypatch, capsys):
    block_cohort_side_effects(monkeypatch)
    path = tmp_path / "missing" / "paper.db"
    assert runner.main(["--database", str(path), "--cohort-since", "2026-10-30"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Cannot read bond book cohort" in output.err
    assert not path.parent.exists()


def test_cohort_cli_does_not_initialize_missing_table(tmp_path, monkeypatch, capsys):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    before = path.read_bytes()
    block_cohort_side_effects(monkeypatch)
    assert runner.main(["--database", str(path), "--cohort-since", "2026-10-30"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "no such table: bond_book_positions" in output.err
    assert path.read_bytes() == before


def test_cohort_cli_invalid_date_does_not_journal(db, tmp_path, monkeypatch, capsys):
    block_cohort_side_effects(monkeypatch)
    assert runner.main(["--database", str(tmp_path / "book.db"), "--cohort-since", "bad"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "YYYY-MM-DD" in output.err
