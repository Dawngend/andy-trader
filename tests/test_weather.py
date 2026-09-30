"""Tests for weather-bucket paper trading. No network: every fetch is faked."""

from datetime import UTC, date, datetime
import inspect
import json
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

from andy_trader import weather
from andy_trader.weather import (
    Bucket,
    Report,
    decide,
    evaluate,
    event_slug,
    first_decision,
    parse_bucket,
    run_once,
    whole_degrees,
)


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Will the highest temperature in London be 19°C on March 1?", Bucket("eq", 19, 19, "C")),
        ("Will the highest temperature in Seoul be -2°C on January 5?", Bucket("eq", -2, -2, "C")),
        ("Will the highest temperature in Paris be 19 °C on May 2?", Bucket("eq", 19, 19, "C")),
        ("Will the highest temperature in NYC be between 50-51°F on May 6?",
         Bucket("range", 50, 51, "F")),
        ("Will the highest temperature in NYC be between 50–51°F on May 6?",
         Bucket("range", 50, 51, "F")),
        ("Will the highest temperature in NYC be between 50°F and 51°F on May 6?",
         Bucket("range", 50, 51, "F")),
        ("Will the highest temperature in Seoul be between -3--2°C on Jan 5?",
         Bucket("range", -3, -2, "C")),
        ("Will the highest temperature in London be 53°F or higher on March 17?",
         Bucket("ge", 53, None, "F")),
        ("Will the highest temperature in London be 16°C or below on September 30?",
         Bucket("le", None, 16, "C")),
        ("Will the highest temperature in NYC be 60°F or lower on May 6?",
         Bucket("le", None, 60, "F")),
    ],
)
def test_parse_bucket_covers_every_question_shape(question, expected) -> None:
    assert parse_bucket(question) == expected


def test_whole_degrees_rounds_half_away_from_zero_in_each_unit() -> None:
    assert whole_degrees(18.5, "C") == 19
    assert whole_degrees(-2.5, "C") == -3
    assert whole_degrees(18.3, "F") == 65   # 64.94 F
    assert whole_degrees(7.22, "F") == 45   # 44.996 F, the IEM example


def test_decide_requires_the_margin_and_never_decides_early() -> None:
    eq19 = Bucket("eq", 19, 19, "C")
    assert decide(eq19, 20) is None       # 20 is only one above the edge: not yet
    assert decide(eq19, 21) is False      # clears 19 by the one-degree margin
    ge20 = Bucket("ge", 20, None, "C")
    assert decide(ge20, 20) is None
    assert decide(ge20, 21) is True
    assert decide(Bucket("le", None, 16, "C"), 18) is False


def _report(hour_utc: int, temp_c: float, minute: int = 50, station: str = "EGLC") -> Report:
    obs = int(datetime(2026, 9, 30, hour_utc, minute, tzinfo=UTC).timestamp())
    return Report(station, obs, obs + 240, temp_c)


def test_first_decision_times_each_bucket_from_the_report_that_decided_it() -> None:
    reports = [_report(9, 15), _report(10, 18), _report(11, 21), _report(13, 24)]
    decision = first_decision(reports, "EGLC", date(2026, 9, 30), "Europe/London",
                              Bucket("eq", 19, 19, "C"))
    assert decision is not None and decision.decided_yes is False
    assert decision.obs_time == reports[2].obs_time    # 21 C at 11:50 UTC, not the later 24 C
    assert decision.running_high == 21


def test_first_decision_uses_the_station_local_day() -> None:
    # 23:50 UTC on Sep 29 is 00:50 BST on Sep 30 in London: it belongs to Sep 30.
    late = Report("EGLC", int(datetime(2026, 9, 29, 23, 50, tzinfo=UTC).timestamp()),
                  int(datetime(2026, 9, 29, 23, 54, tzinfo=UTC).timestamp()), 25)
    decision = first_decision([late], "EGLC", date(2026, 9, 30), "Europe/London",
                              Bucket("eq", 19, 19, "C"))
    assert decision is not None and decision.local_hour == 0


def test_event_slug_matches_polymarket_naming() -> None:
    assert event_slug("london", date(2026, 9, 30)) == \
        "highest-temperature-in-london-on-september-30-2026"


class FakeNetwork:
    """Serves METAR, Gamma events/markets and CLOB books from in-memory fixtures."""

    def __init__(self, reports, markets, books, resolutions=None):
        self.reports, self.markets, self.books = reports, markets, books
        self.resolutions = resolutions or {}
        self.calls: list[str] = []

    def __call__(self, url: str) -> object:
        self.calls.append(url)
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if "aviationweather.gov" in parsed.netloc:
            return self.reports
        if parsed.path.endswith("/events"):
            return [{"slug": query["slug"], "markets": self.markets}]
        if parsed.path.endswith("/markets"):
            prices = self.resolutions.get(query["slug"])
            if prices is None:
                return [{"closed": False}]
            return [{"closed": True, "outcomePrices": json.dumps(prices)}]
        if parsed.path.endswith("/book"):
            return self.books.get(query["token_id"], {"asks": [], "bids": []})
        raise AssertionError(f"unexpected URL {url}")


def _metar(hour_utc: int, temp: float, minute: int = 50) -> dict:
    obs = datetime(2026, 9, 30, hour_utc, minute, tzinfo=UTC)
    return {"icaoId": "EGLC", "obsTime": int(obs.timestamp()),
            "receiptTime": obs.replace(minute=minute + 4).isoformat().replace("+00:00", "Z"),
            "temp": temp}


def _market(question: str, slug: str, yes: str, no: str) -> dict:
    return {"slug": slug, "question": question, "outcomes": '["Yes", "No"]',
            "clobTokenIds": json.dumps([yes, no]), "acceptingOrders": True, "closed": False,
            "feesEnabled": True, "takerBaseFee": 1000}


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC).timestamp()   # 13:00 in London


def test_decided_bucket_offered_below_one_dollar_opens_exactly_one_paper_trade() -> None:
    net = FakeNetwork(
        reports=[_metar(9, 15), _metar(11, 21)],
        markets=[_market("Will the highest temperature in London be 19°C on September 30?",
                         "london-19c", "Y19", "N19")],
        books={"N19": {"asks": [{"price": "0.90", "size": "50"}]}},
    )
    conn = _conn()
    first = run_once(conn, ["london"], now=NOW, fetch=net)
    assert (first.newly_decided, first.offers_checked, first.trades_opened) == (1, 1, 1)
    side, delay, cost, fee = conn.execute(
        "SELECT side, seconds_after_receipt, cost, fee FROM weather_paper_trades").fetchone()
    assert side == "no"
    assert cost == pytest.approx(9.0)
    assert fee == pytest.approx(10 * 0.10 * 0.90 * 0.10)   # market's own 1000 bps rate
    assert delay == int(NOW) - int(datetime(2026, 9, 30, 11, 54, tzinfo=UTC).timestamp())
    second = run_once(conn, ["london"], now=NOW + 60, fetch=net)
    assert second.trades_opened == 0 and second.offers_checked == 0   # one trade per bucket


def test_decided_bucket_already_priced_at_one_dollar_is_logged_but_not_traded() -> None:
    net = FakeNetwork(
        reports=[_metar(11, 21)],
        markets=[_market("Will the highest temperature in London be 19°C on September 30?",
                         "london-19c", "Y19", "N19")],
        books={"N19": {"asks": [{"price": "0.999", "size": "500"}]}},
    )
    conn = _conn()
    summary = run_once(conn, ["london"], now=NOW, fetch=net)
    assert (summary.offers_checked, summary.trades_opened) == (1, 0)
    assert conn.execute("SELECT count(*) FROM weather_offers").fetchone()[0] == 1


def test_undecided_and_late_night_buckets_are_never_traded() -> None:
    late_night = datetime(2026, 9, 30, 22, 30, tzinfo=UTC)   # 23:30 in London
    net = FakeNetwork(
        reports=[{"icaoId": "EGLC", "obsTime": int(late_night.timestamp()),
                  "receiptTime": "2026-09-30T22:34:00Z", "temp": 25}],
        markets=[_market("Will the highest temperature in London be 19°C on September 30?",
                         "london-19c", "Y19", "N19"),
                 _market("Will the highest temperature in London be 26°C on September 30?",
                         "london-26c", "Y26", "N26")],
        books={"N19": {"asks": [{"price": "0.50", "size": "50"}]}},
    )
    conn = _conn()
    summary = run_once(conn, ["london"], now=late_night.timestamp() + 600, fetch=net)
    assert (summary.newly_decided, summary.trades_opened) == (0, 0)


def test_settlement_reads_the_recorded_outcome_and_never_infers_it() -> None:
    net = FakeNetwork(
        reports=[_metar(11, 21)],
        markets=[_market("Will the highest temperature in London be 19°C on September 30?",
                         "london-19c", "Y19", "N19")],
        books={"N19": {"asks": [{"price": "0.90", "size": "50"}]}},
    )
    conn = _conn()
    run_once(conn, ["london"], now=NOW, fetch=net)
    assert conn.execute("SELECT settled_at FROM weather_paper_trades").fetchone()[0] is None
    net.resolutions["london-19c"] = ["0", "1"]   # NO won
    run_once(conn, ["london"], now=NOW + 86_400, fetch=net)
    payout, pnl = conn.execute("SELECT payout, pnl FROM weather_paper_trades").fetchone()
    assert payout == 10 and pnl == pytest.approx(10 - 9.0 - 0.09)


def test_gate_blocks_until_200_settled_profitable_trades() -> None:
    conn = _conn()
    weather.initialize(conn)
    for i in range(150):
        conn.execute(
            "INSERT INTO weather_paper_trades (market_slug, city, side, token_id, opened_at, "
            "seconds_after_receipt, shares, cost, fee, settled_at, yes_won, payout, pnl) "
            "VALUES (?, 'london', 'no', 't', ?, 60, 10, 9, 0.1, 'x', 0, 10, 0.9)",
            (f"m{i}", f"2026-09-30T00:{i // 60:02d}:{i % 60:02d}"),
        )
    verdict = evaluate(conn)
    assert not verdict.eligible and "150 of 200" in verdict.reason
    for i in range(150, 250):
        conn.execute(
            "INSERT INTO weather_paper_trades (market_slug, city, side, token_id, opened_at, "
            "seconds_after_receipt, shares, cost, fee, settled_at, yes_won, payout, pnl) "
            "VALUES (?, 'london', 'no', 't', ?, 60, 10, 9, 0.1, 'x', 0, 10, 0.9)",
            (f"m{i}", f"2026-09-30T01:{(i - 150) // 60:02d}:{(i - 150) % 60:02d}"),
        )
    assert evaluate(conn).eligible


def test_module_has_no_order_path() -> None:
    source = inspect.getsource(weather)
    for forbidden in ("POST", "method=", "private_key", "signature", "/order"):
        assert forbidden not in source, forbidden
