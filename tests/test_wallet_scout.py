import json
import sqlite3
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

import pytest

from andy_trader import wallet_scout as scout


ACCOUNT = {"address": "0x" + "a" * 40, "user_name": "Alice", "rank": 1,
           "leaderboard_pnl": 1000, "leaderboard_volume": 5000}
LEADER = {"user_id": ACCOUNT["address"], "user_name": "Alice", "rank": 1,
          "pnl": 1000, "volume": 5000}


def position(price=.6, pnl=1, timestamp=0, shares=10, title="Other"):
    return dict(avgPrice=price, realizedPnl=pnl, timestamp=timestamp,
                totalBought=shares, title=title)


def buy(timestamp, price=.96):
    return dict(type="TRADE", side="BUY", timestamp=timestamp, price=price, usdcSize=10)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    http = Mock(side_effect=AssertionError("Unexpected network call"))
    monkeypatch.setattr(scout, "_http_json", http)
    monkeypatch.setattr(scout.time, "sleep", Mock())
    return http


def test_leaderboard_parses_v2_in_one_request(no_network):
    no_network.side_effect = [{"data": [LEADER]}]
    assert scout.fetch_leaderboard(2, "WEEK") == [ACCOUNT]
    assert no_network.call_count == 1
    query = parse_qs(urlparse(no_network.call_args.args[0]).query)
    assert "offset" not in query
    assert query["limit"] == ["2"]
    assert query["timePeriod"] == ["WEEK"]
    assert "/v2/leaderboard" in no_network.call_args.args[0]


def test_profile_numbers():
    closed = [position(.2, 8, 0, title="Will X hit 10?"),
              position(.5, -5, 10, title="X vs. Y"),
              position(.8, 2, 80, title="Above 5?"),
              position(.95, -9.5, 100, title="Up or Down?"),
              position(.99, 0, 100)]
    profile = scout.profile_account(ACCOUNT, closed, [buy(0), buy(3600, .5)])
    assert profile["sample_size"] == 5
    assert (profile["first_timestamp"], profile["last_timestamp"]) == (0, 100)
    assert profile["win_rate"] == .4
    assert profile["cost"] == pytest.approx(34.4)
    assert profile["pnl"] == -4.5
    assert profile["return_on_cost"] == pytest.approx(-4.5 / 34.4)
    assert profile["average_win"] == 5
    assert profile["average_loss"] == -7.25
    assert profile["loss_to_win_ratio"] == 1.45
    assert profile["worst_loss"] == -9.5
    assert profile["first_half_pnl"] == 3
    assert profile["second_half_pnl"] == -7.5
    assert profile["median_position_size"] == 8
    assert profile["p90_position_size"] == pytest.approx(9.74)
    assert profile["high_price_buy_share"] == .5
    assert profile["buys_per_hour"] == 2
    for (name, _), row in zip(scout.PRICE_BANDS, closed):
        assert profile["price_bands"][name] == {
            "count": 1, "pnl": row["realizedPnl"], "cost": row["avgPrice"] * 10,
            "win_rate": float(row["realizedPnl"] > 0)}
    assert profile["market_types"]["price_target"]["pnl"] == 8
    assert profile["market_types"]["sports"]["win_rate"] == 0


@pytest.mark.parametrize("title,expected", [
    ("BTC Up/Down", "up_down"), ("Up or Down?", "up_down"),
    ("Will X dip to 5?", "price_target"), ("Will X reach 5?", "price_target"),
    ("Will X hit 5?", "price_target"), ("Below 10", "above_below"),
    ("Above 10", "above_below"), ("A versus B", "sports"),
    ("A vs. B", "sports"), ("A vs B", "sports"), ("White House", "other")])
def test_market_rules(title, expected):
    assert scout.market_type(title) == expected


@pytest.mark.parametrize("field,value,flag,expected", [
    ("half_price_share", .2, "inventory_heavy", True),
    ("half_price_share", .199, "inventory_heavy", False),
    ("split_merge_share", .1, "inventory_heavy", True),
    ("split_merge_share", .099, "inventory_heavy", False),
    ("buys_per_hour", 20.01, "bot_like", True),
    ("buys_per_hour", 20, "bot_like", False),
    ("sample_size", 199, "thin_sample", True),
    ("sample_size", 200, "thin_sample", False),
    ("second_half_pnl", 0, "one_sided_period", True),
    ("second_half_pnl", -1, "one_sided_period", True),
    ("second_half_pnl", 1, "one_sided_period", False),
])
def test_flag_boundaries(field, value, flag, expected):
    profile = dict(half_price_share=0, split_merge_share=0, buys_per_hour=0,
                   sample_size=200, first_half_pnl=1, second_half_pnl=1)
    profile[field] = value
    assert (flag in scout.profile_flags(profile)) == expected


def test_flags_from_rows():
    profile = scout.profile_account(ACCOUNT, [position(.5, 1)], [buy(0), buy(1)])
    assert set(profile["flags"]) == {"inventory_heavy", "bot_like", "thin_sample", "one_sided_period"}
    profile = scout.profile_account(ACCOUNT, [position(.6, -1)] * 200,
                                    [dict(type="SPLIT", timestamp=0)] + [buy(3600)] * 9)
    assert profile["flags"] == ["inventory_heavy"]
    profile = scout.profile_account(ACCOUNT, [position(.6, -1)] * 200, [buy(0), buy(3600)])
    assert profile["flags"] == []


@pytest.mark.parametrize("closed,activity,label", [
    ([position(.5)], [], "market_maker"),
    ([position(.9)], [dict(type="MAKER_REBATE", timestamp=0)], "market_maker"),
    ([position(.9)], [], "bond"),
    ([position(.2)], [], "longshot"),
    ([position(.9, -1, title="Will X hit 10?")], [], "price_target_no"),
    ([position(.6)], [], "mixed"),
])
def test_labels_from_rows(closed, activity, label):
    assert scout.profile_account(ACCOUNT, closed, activity)["label"] == label


@pytest.mark.parametrize("changes,label", [
    ({"flags": ["inventory_heavy"]}, "market_maker"),
    ({"maker_rebate_count": 1}, "market_maker"),
    ({}, "bond"),
    ({"win_rate": .899}, "longshot"),
    ({"high_entry_cost_share": .599}, "longshot"),
    ({"high_entry_cost_share": 0, "longshot_share": .399}, "price_target_no"),
    ({"high_entry_cost_share": 0, "longshot_share": .399, "price_target_high_share": .399}, "mixed"),
])
def test_label_thresholds_and_first_match(changes, label):
    profile = dict(flags=[], maker_rebate_count=0, high_entry_cost_share=.6,
                   win_rate=.9, longshot_share=.4, price_target_high_share=.4)
    profile.update(changes)
    assert scout.strategy_label(profile) == label


def test_empty_and_zero_duration_samples():
    empty = scout.profile_account(ACCOUNT, [], [])
    for field in ("win_rate", "return_on_cost", "loss_to_win_ratio", "buys_per_hour", "first_timestamp", "median_position_size"):
        assert empty[field] is None
    assert empty["flags"] == ["thin_sample"]
    assert empty["label"] == "mixed"
    profile = scout.profile_account(ACCOUNT, [position(0, 0)], [buy(0)])
    assert profile["return_on_cost"] is None
    assert profile["buys_per_hour"] is None
    assert profile["loss_to_win_ratio"] is None


@pytest.mark.parametrize("pages,responses,expected,calls", [
    (40, [[position()], []], 1, 2), (2, [[position()], [position()]], 2, 2),
    (40, [[]], 0, 1), (300, [[position()]] * 200, 200, 200)])
def test_paging_stops(no_network, pages, responses, expected, calls):
    no_network.side_effect = responses
    assert len(scout.fetch_closed_positions(scout.PublicReader(), ACCOUNT["address"], pages)) == expected
    assert no_network.call_count == calls
    for index, call in enumerate(no_network.call_args_list):
        assert parse_qs(urlparse(call.args[0]).query)["offset"] == [str(index * 50)]
    assert scout.time.sleep.call_count == calls


def test_failure_budget_spans_requests(no_network):
    no_network.side_effect = [OSError("first"), [position()], OSError("second"), [], OSError("third")]
    reader = scout.PublicReader()
    assert len(scout.fetch_closed_positions(reader, ACCOUNT["address"], 40)) == 1
    with pytest.raises(RuntimeError, match="third"):
        reader.get("activity", user=ACCOUNT["address"])
    assert no_network.call_count == 5
    assert len(reader.failures) == 3


def test_run_continues_report_and_append_only(tmp_path, no_network):
    bad = dict(LEADER, user_id="0x" + "b" * 40, rank=2, user_name="Bob")
    def response(url, timeout):
        if "leaderboard" in url:
            return {"data": [bad, LEADER]}
        if bad["user_id"] in url:
            raise OSError("account unavailable")
        if "closed-positions" in url:
            return [position()] if "offset=0&" in url else []
        return [buy(0), buy(3600)]
    no_network.side_effect = response
    database, report = tmp_path / "scout.db", tmp_path / "research" / "report.md"
    first = scout.run_scout(database, report, top=2)
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT * FROM wallet_profiles").fetchall()
        failure = connection.execute("SELECT * FROM wallet_scout_failures").fetchone()
        assert failure[0:2] == (first, bad["user_id"])
        assert "account unavailable" in failure[2]
    second = scout.run_scout(database, report, top=2)
    assert first != second
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM wallet_profiles WHERE run_id=?", (first,)).fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM wallet_profiles").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM wallet_scout_failures").fetchone()[0] == 2
        bands, flags = connection.execute("SELECT price_bands, flags FROM wallet_profiles LIMIT 1").fetchone()
        assert json.loads(bands)["0.50_to_0.80"]["count"] == 1
        assert "thin_sample" in json.loads(flags)
    text = report.read_text(encoding="utf-8")
    assert "| User Name | Label | Flags | Sample Size |" in text
    assert "lifetime pnl can be much larger" in text
    assert "Sample windows (UTC)" in text
    assert "1970-01-01" in text
    assert ACCOUNT["address"] not in text
    assert "Alice (0xaaaaaa...aaaa)" in text


def test_report_rank_order_and_escaping(tmp_path):
    alice = scout.profile_account(dict(ACCOUNT, user_name="Alice|<x>\n" + ACCOUNT["address"]), [], [])
    bob = scout.profile_account(dict(ACCOUNT, user_name="Bob", rank=2), [], [])
    report = tmp_path / "report.md"
    scout.write_report(report, [bob, alice], "run", "now", "MONTH", 0)
    text = report.read_text(encoding="utf-8")
    assert text.index("Alice") < text.index("Bob")
    assert "&#124;&lt;x&gt;" in text
    assert ACCOUNT["address"] not in text


def test_recovered_failure_is_recorded(tmp_path, no_network):
    no_network.side_effect = [{"data": [LEADER]}, OSError("temporary"), [], []]
    scout.run_scout(tmp_path / "scout.db", tmp_path / "report.md", top=1)
    with sqlite3.connect(tmp_path / "scout.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM wallet_profiles").fetchone()[0] == 1
        assert "temporary" in connection.execute("SELECT reason FROM wallet_scout_failures").fetchone()[0]
