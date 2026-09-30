"""Paper-trade Polymarket daily-high weather buckets that METAR has already decided.

Research basis: research/weather_scoping/RESULTS.md. The station's own METAR
reports reproduce 99.07% of these markets' resolutions, and once the day's
running high passes a bucket (by a one-degree margin) that bucket's outcome is
known hours before the market closes. Historically a trader who bought the
already-decided side within about 5 minutes of the deciding report could still
buy it below $1; within 15 minutes most of that was gone. So this is a speed
check, run every minute:

    1. fetch the latest METAR for each station (one GET, all stations)
    2. rebuild today's running high in the station's local day and the market's unit
    3. for every open bucket in today's event, decide it if the high already has
    4. if a decided side is still offered below $1 after fees, open ONE paper
       trade for that bucket at the price the order book actually shows
    5. settle paper trades from Polymarket's own recorded outcome

Nothing here can place an order. The only network calls are HTTP GETs to
aviationweather.gov, gamma-api.polymarket.com and clob.polymarket.com's public
/book endpoint. There is no wallet, key or signing code in this module.

Fees: Gamma reports each market's `takerBaseFee` in basis points (1000 on the
weather markets checked 2026-09-30). The rate is applied through Polymarket's
fee formula shares * rate * p * (1 - p), the same formula CT-11 uses, which is
conservative if the real weather fee is lower.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
import re
import sqlite3
import sys
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from andy_trader.complete_set import walk_ask_book

USER_AGENT = "andy-trader-weather/1.0 (personal research, paper only)"
METAR_URL = "https://aviationweather.gov/api/data/metar"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

# Settlement stations and units come from each market's official rules text
# (research/weather_scoping/SCOPING.md). Only cities whose METAR high was
# validated against real resolutions are tradeable here: London (0 of 108 days
# disagreed), Shanghai (0 of 30) and NYC (100% of buckets reproduced). Seoul
# (RKSI, 13% of days disagreed) and Paris (LFPB, 47%, three by 2 C or more)
# settle on Wunderground figures that do not track METAR closely enough, and
# Hong Kong settles on the Observatory rather than a METAR station, so all
# three are deliberately absent.
CITIES: dict[str, tuple[str, str, str]] = {
    # key: (ICAO station, time zone, slug fragment)
    "london": ("EGLC", "Europe/London", "london"),
    "nyc": ("KLGA", "America/New_York", "nyc"),
    "shanghai": ("ZSPD", "Asia/Shanghai", "shanghai"),
}
DEFAULT_CITIES = ("london", "nyc")  # where the historical edge concentrated
MARGIN = 1              # degrees the running high must clear a bucket edge by
LAST_DECISION_HOUR = 23  # readings at or after 23:00 local can be disputed as next-day
TARGET_SHARES = 10.0
MIN_EDGE = 0.02         # net profit per share required before a paper trade opens

FetchJson = Callable[[str], object]


class WeatherError(RuntimeError):
    """A data problem this module refuses to guess its way around."""


def http_get_json(url: str, timeout_seconds: float = 15.0) -> object:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404 and url.startswith(CLOB_BOOK_URL):
            return {"asks": [], "bids": []}
        raise WeatherError(f"GET {url} failed: HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise WeatherError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc


# --- Buckets -----------------------------------------------------------------

_NUM = r"(-?\d+)"
_DEG = r"\s*[°º]\s*"
_RANGE = re.compile(
    rf"{_NUM}(?:{_DEG}[CF])?\s*(?:-|–|—|\band\b|\bto\b)\s*{_NUM}{_DEG}([CF])", re.I
)
_SINGLE = re.compile(rf"(?<![\d-]){_NUM}{_DEG}([CF])", re.I)


@dataclass(frozen=True)
class Bucket:
    kind: str          # "eq", "range", "ge", "le"
    lo: int | None
    hi: int | None
    unit: str          # "C" or "F"


def parse_bucket(question: str) -> Bucket:
    """Parse a daily-high question; every shape seen in 2025-2026 is covered."""

    match = _RANGE.search(question)
    if match:
        lo, hi = int(match.group(1)), int(match.group(2))
        if lo > hi:
            raise WeatherError(f"range reversed in {question!r}")
        return Bucket("range", lo, hi, match.group(3).upper())
    match = _SINGLE.search(question)
    if not match:
        raise WeatherError(f"no temperature in {question!r}")
    value, unit, text = int(match.group(1)), match.group(2).upper(), question.lower()
    if "or higher" in text or "or above" in text:
        return Bucket("ge", value, None, unit)
    if "or below" in text or "or lower" in text:
        return Bucket("le", None, value, unit)
    return Bucket("eq", value, value, unit)


def decide(bucket: Bucket, running_high: int, margin: int = MARGIN) -> bool | None:
    """True/False once the running high settles the bucket, else None.

    The daily high can only rise, so a bucket becomes impossible (False) once the
    high passes its upper edge, and "X or higher" becomes certain (True) once the
    high reaches X. The margin absorbs METAR vs Wunderground rounding.
    """

    if bucket.kind == "ge" and bucket.lo is not None and running_high >= bucket.lo + margin:
        return True
    if bucket.kind in ("eq", "range", "le") and bucket.hi is not None \
            and running_high > bucket.hi + margin:
        return False
    return None


# --- METAR -------------------------------------------------------------------

@dataclass(frozen=True)
class Report:
    station: str
    obs_time: int       # epoch seconds, observation time
    receipt_time: int   # epoch seconds, when the report was published
    temp_c: float


def whole_degrees(temp_c: float, unit: str) -> int:
    """Round to whole degrees in the market's unit, half away from zero."""

    value = temp_c if unit == "C" else temp_c * 9 / 5 + 32
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def fetch_reports(stations: Sequence[str], fetch: FetchJson = http_get_json,
                  hours: int = 30) -> list[Report]:
    payload = fetch(f"{METAR_URL}?{urlencode({'ids': ','.join(stations), 'format': 'json', 'hours': hours})}")
    if not isinstance(payload, list):
        raise WeatherError("METAR API did not return a list")
    reports = []
    for item in payload:
        if not isinstance(item, Mapping) or item.get("temp") is None:
            continue
        obs = int(item["obsTime"])
        receipt_raw = item.get("receiptTime")
        receipt = (int(datetime.fromisoformat(str(receipt_raw).replace("Z", "+00:00")).timestamp())
                   if receipt_raw else obs)
        reports.append(Report(str(item["icaoId"]), obs, receipt, float(item["temp"])))
    return sorted(reports, key=lambda r: r.obs_time)


@dataclass(frozen=True)
class Decision:
    decided_yes: bool
    running_high: int    # the running high at the deciding report
    obs_time: int        # the report that FIRST decided this bucket
    receipt_time: int    # when that report was published
    local_hour: int


def first_decision(reports: Sequence[Report], station: str, local_day: date, tz: str,
                   bucket: Bucket, margin: int = MARGIN) -> Decision | None:
    """The earliest report of the local day whose running high decides `bucket`.

    Measured per bucket on purpose: the day's high keeps rising after a bucket
    is decided, and timing a bucket from whichever report set the latest high
    would make an hours-old decision look fresh, hiding exactly the staleness
    this strategy trades on.
    """

    zone = ZoneInfo(tz)
    high: int | None = None
    for report in reports:
        if report.station != station:
            continue
        local = datetime.fromtimestamp(report.obs_time, UTC).astimezone(zone)
        if local.date() != local_day:
            continue
        value = whole_degrees(report.temp_c, bucket.unit)
        high = value if high is None else max(high, value)
        verdict = decide(bucket, high, margin)
        if verdict is not None:
            return Decision(verdict, high, report.obs_time, report.receipt_time, local.hour)
    return None


# --- Markets -----------------------------------------------------------------

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")


def event_slug(city: str, local_day: date) -> str:
    return (f"highest-temperature-in-{CITIES[city][2]}-on-"
            f"{_MONTHS[local_day.month - 1]}-{local_day.day}-{local_day.year}")


def _list(value: object) -> list[object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise WeatherError(f"expected a list, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class BucketMarket:
    slug: str
    question: str
    bucket: Bucket
    yes_token: str
    no_token: str
    fee_rate: float
    accepting_orders: bool


def fetch_event_markets(city: str, local_day: date,
                        fetch: FetchJson = http_get_json) -> list[BucketMarket]:
    payload = fetch(f"{GAMMA_EVENTS_URL}?{urlencode({'slug': event_slug(city, local_day)})}")
    if not isinstance(payload, list) or not payload:
        return []
    markets = []
    for m in payload[0].get("markets") or []:
        outcomes = [str(o).casefold() for o in _list(m.get("outcomes"))]
        tokens = [str(t) for t in _list(m.get("clobTokenIds"))]
        if outcomes != ["yes", "no"] or len(tokens) != 2:
            continue
        fee_rate = (float(m.get("takerBaseFee") or 0) / 10_000) if m.get("feesEnabled") else 0.0
        markets.append(BucketMarket(
            slug=str(m.get("slug")), question=str(m.get("question")),
            bucket=parse_bucket(str(m.get("question"))), yes_token=tokens[0], no_token=tokens[1],
            fee_rate=fee_rate,
            accepting_orders=bool(m.get("acceptingOrders")) and not m.get("closed"),
        ))
    return markets


def market_resolution(slug: str, fetch: FetchJson = http_get_json) -> bool | None:
    """True if YES won, False if NO won, None if not resolved (read, never inferred)."""

    payload = fetch(f"{GAMMA_MARKETS_URL}?{urlencode({'slug': slug})}")
    if not isinstance(payload, list) or not payload:
        return None
    market = payload[0]
    if not market.get("closed") or market.get("outcomePrices") is None:
        return None
    prices = [float(p) for p in _list(market["outcomePrices"])]
    if prices[0] >= 0.99 and prices[1] <= 0.01:
        return True
    if prices[1] >= 0.99 and prices[0] <= 0.01:
        return False
    return None


# --- Storage -----------------------------------------------------------------

def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS weather_decisions (
            market_slug TEXT PRIMARY KEY,
            city TEXT NOT NULL,
            local_day TEXT NOT NULL,
            bucket_kind TEXT NOT NULL,
            temp_lo INTEGER,
            temp_hi INTEGER,
            unit TEXT NOT NULL,
            decided_yes INTEGER NOT NULL,
            running_high INTEGER NOT NULL,
            deciding_obs_time INTEGER NOT NULL,
            deciding_receipt_time INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS weather_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_slug TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            seconds_after_receipt INTEGER NOT NULL,
            side TEXT NOT NULL,
            best_ask REAL,
            shares REAL NOT NULL,
            fill_cost REAL,
            fee_cost REAL,
            edge_per_share REAL
        );
        CREATE TABLE IF NOT EXISTS weather_paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_slug TEXT NOT NULL UNIQUE,
            city TEXT NOT NULL,
            side TEXT NOT NULL,
            token_id TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            seconds_after_receipt INTEGER NOT NULL,
            shares REAL NOT NULL,
            cost REAL NOT NULL,
            fee REAL NOT NULL,
            settled_at TEXT,
            yes_won INTEGER,
            payout REAL,
            pnl REAL
        );
        """
    )
    connection.commit()


# --- One pass ----------------------------------------------------------------

@dataclass(frozen=True)
class PassSummary:
    buckets_seen: int
    newly_decided: int
    offers_checked: int
    trades_opened: int
    trades_settled: int


def _now_iso(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).isoformat()


def run_once(connection: sqlite3.Connection, cities: Sequence[str] = DEFAULT_CITIES, *,
             now: float | None = None, fetch: FetchJson = http_get_json) -> PassSummary:
    now = datetime.now(UTC).timestamp() if now is None else now
    initialize(connection)
    reports = fetch_reports([CITIES[c][0] for c in cities], fetch=fetch)
    seen = newly_decided = checked = opened = 0
    for city in cities:
        station, tz, _ = CITIES[city]
        local_day = datetime.fromtimestamp(now, UTC).astimezone(ZoneInfo(tz)).date()
        for market in fetch_event_markets(city, local_day, fetch=fetch):
            seen += 1
            decision = first_decision(reports, station, local_day, tz, market.bucket)
            if decision is None or decision.local_hour >= LAST_DECISION_HOUR:
                continue
            verdict = decision.decided_yes
            inserted = connection.execute(
                """INSERT OR IGNORE INTO weather_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (market.slug, city, local_day.isoformat(), market.bucket.kind, market.bucket.lo,
                 market.bucket.hi, market.bucket.unit, int(verdict), decision.running_high,
                 decision.obs_time, decision.receipt_time, _now_iso(now)),
            ).rowcount
            newly_decided += inserted
            if not market.accepting_orders:
                continue
            already = connection.execute(
                "SELECT 1 FROM weather_paper_trades WHERE market_slug = ?", (market.slug,)
            ).fetchone()
            if already:
                continue
            token = market.yes_token if verdict else market.no_token
            book = fetch(f"{CLOB_BOOK_URL}?{urlencode({'token_id': token})}")
            if not isinstance(book, Mapping):
                raise WeatherError(f"order book for {market.slug} is not an object")
            fill = walk_ask_book(book, TARGET_SHARES, fee_rate=market.fee_rate)
            checked += 1
            delay = int(now) - decision.receipt_time
            edge = None
            if fill.complete and fill.fill_cost is not None and fill.fee_cost is not None:
                edge = 1 - (fill.fill_cost + fill.fee_cost) / TARGET_SHARES
            connection.execute(
                """INSERT INTO weather_offers (market_slug, observed_at, seconds_after_receipt,
                   side, best_ask, shares, fill_cost, fee_cost, edge_per_share)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (market.slug, _now_iso(now), delay, "yes" if verdict else "no", fill.best_ask,
                 TARGET_SHARES, fill.fill_cost, fill.fee_cost, edge),
            )
            if edge is not None and edge >= MIN_EDGE:
                connection.execute(
                    """INSERT INTO weather_paper_trades (market_slug, city, side, token_id,
                       opened_at, seconds_after_receipt, shares, cost, fee)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (market.slug, city, "yes" if verdict else "no", token, _now_iso(now), delay,
                     TARGET_SHARES, fill.fill_cost, fill.fee_cost),
                )
                opened += 1
    connection.commit()
    settled = settle_due(connection, now=now, fetch=fetch)
    return PassSummary(seen, newly_decided, checked, opened, settled)


def settle_due(connection: sqlite3.Connection, *, now: float,
               fetch: FetchJson = http_get_json) -> int:
    settled = 0
    rows = connection.execute(
        "SELECT id, market_slug, side, shares, cost, fee FROM weather_paper_trades "
        "WHERE settled_at IS NULL"
    ).fetchall()
    for trade_id, slug, side, shares, cost, fee in rows:
        yes_won = market_resolution(slug, fetch=fetch)
        if yes_won is None:
            continue
        won = yes_won == (side == "yes")
        payout = shares if won else 0.0
        connection.execute(
            "UPDATE weather_paper_trades SET settled_at = ?, yes_won = ?, payout = ?, pnl = ? "
            "WHERE id = ?",
            (_now_iso(now), int(yes_won), payout, payout - cost - fee, trade_id),
        )
        settled += 1
    connection.commit()
    return settled


# --- Evidence ----------------------------------------------------------------

MINIMUM_SETTLED_TRADES = 200
RECENT_WINDOW = 100


@dataclass(frozen=True)
class WeatherVerdict:
    settled: int
    wins: int
    net_pnl: float
    recent_net_pnl: float | None
    hit_rate: float | None
    break_even_hit_rate: float | None
    eligible: bool
    reason: str


def evaluate(connection: sqlite3.Connection) -> WeatherVerdict:
    """The paper gate's bar, applied to trades whose outcome is meant to be certain.

    Brier skill is meaningless for calls made at probability ~1, so the evidence
    is money and correctness: at least 200 settled paper trades, positive net
    PnL over all of them AND over the most recent 100, and a hit rate above the
    break-even rate implied by what the trades actually cost.
    """

    initialize(connection)
    rows = connection.execute(
        "SELECT pnl, payout, shares, cost, fee FROM weather_paper_trades "
        "WHERE settled_at IS NOT NULL ORDER BY opened_at"
    ).fetchall()
    n = len(rows)
    wins = sum(1 for r in rows if r[1] > 0)
    net = sum(r[0] for r in rows)
    recent = rows[-RECENT_WINDOW:]
    recent_net = sum(r[0] for r in recent) if len(recent) == RECENT_WINDOW else None
    hit = wins / n if n else None
    break_even = (sum(r[3] + r[4] for r in rows) / sum(r[2] for r in rows)) if n else None
    if n < MINIMUM_SETTLED_TRADES:
        return WeatherVerdict(n, wins, net, recent_net, hit, break_even, False,
                              f"{n} of {MINIMUM_SETTLED_TRADES} settled trades")
    if net <= 0:
        return WeatherVerdict(n, wins, net, recent_net, hit, break_even, False,
                              "net PnL is not positive")
    if recent_net is None or recent_net <= 0:
        return WeatherVerdict(n, wins, net, recent_net, hit, break_even, False,
                              f"most recent {RECENT_WINDOW} trades are not profitable")
    if hit is None or break_even is None or hit <= break_even:
        return WeatherVerdict(n, wins, net, recent_net, hit, break_even, False,
                              "hit rate does not clear break-even")
    return WeatherVerdict(n, wins, net, recent_net, hit, break_even, True, "clears the gate")


def main(argv: Sequence[str] | None = None) -> int:
    from andy_trader.store import connect, default_database_path

    parser = argparse.ArgumentParser(description="Weather-bucket paper trading (paper only).")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="one pass: METAR, decisions, offers, paper trades")
    run.add_argument("--cities", nargs="+", default=list(DEFAULT_CITIES), choices=sorted(CITIES))
    commands.add_parser("report", help="paper results and the gate verdict")
    args = parser.parse_args(argv)
    connection = connect(default_database_path())
    if args.command == "run":
        summary = run_once(connection, args.cities)
        print(f"buckets {summary.buckets_seen}, newly decided {summary.newly_decided}, "
              f"offers checked {summary.offers_checked}, opened {summary.trades_opened}, "
              f"settled {summary.trades_settled}")
        return 0
    verdict = evaluate(connection)
    offers = connection.execute(
        "SELECT count(*), sum(edge_per_share >= ?) FROM weather_offers", (MIN_EDGE,)
    ).fetchone()
    print(f"offers checked on decided buckets: {offers[0]} (profitable: {offers[1] or 0})")
    print(f"settled paper trades: {verdict.settled}, wins {verdict.wins}, "
          f"net PnL ${verdict.net_pnl:.2f}")
    if verdict.hit_rate is not None:
        print(f"hit rate {verdict.hit_rate:.1%} vs break-even {verdict.break_even_hit_rate:.1%}")
    print(f"gate: {'MAY TRADE' if verdict.eligible else 'blocked'} ({verdict.reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
