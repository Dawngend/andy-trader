from datetime import UTC, datetime, timedelta

import pytest

from andy_trader import trend_book
from andy_trader.portfolio import execute_paper_trade, get_or_create_state
from andy_trader.risk import RiskDecision
from andy_trader.store import Candle, connect, record_observations

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
INSTRUMENT = "BTC-USD"


def daily(day_offset, close, *, instrument=INSTRUMENT):
    """A completed daily Bybit candle `day_offset` days before NOW's date."""

    open_time = datetime.combine((NOW - timedelta(days=day_offset)).date(), datetime.min.time(), tzinfo=UTC)
    return Candle(instrument, "bybit", "1d", open_time.isoformat(), close, close, close, close, 1)


def price_bar(price=100, age_seconds=65, instrument=INSTRUMENT):
    open_time = NOW - timedelta(seconds=age_seconds)
    return Candle(instrument, "binance", "1m", open_time.isoformat(), price, price, price, price, 1)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(trend_book, "INSTRUMENTS", (INSTRUMENT,))
    with connect(tmp_path / "test.db") as connection:
        yield connection


def rising_closes(count=41, start=100.0, step=1.0):
    # day_offset 1 is the latest completed day (yesterday); larger offsets are older.
    return [daily(offset, start + step * (count - offset)) for offset in range(1, count + 1)]


def falling_closes(count=41, start=100.0, step=1.0):
    return [daily(offset, start + step * offset) for offset in range(1, count + 1)]


def test_rising_series_opens_long(db):
    record_observations(db, rising_closes())
    record_observations(db, [price_bar()])
    trend_book.run_once(db, now=NOW)
    state = get_or_create_state(db, predictor="trend40", instrument=INSTRUMENT, now_iso=NOW.isoformat())
    assert state.position_qty > 0


def test_falling_series_opens_short(db):
    record_observations(db, falling_closes())
    record_observations(db, [price_bar()])
    trend_book.run_once(db, now=NOW)
    state = get_or_create_state(db, predictor="trend40", instrument=INSTRUMENT, now_iso=NOW.isoformat())
    assert state.position_qty < 0


@pytest.mark.parametrize(
    "earlier_closes, vote_side",
    [
        ((90, 90, 110), "long"),
        ((90, 110, 90), "long"),
        ((110, 90, 90), "long"),
        ((110, 110, 90), "short"),
        ((110, 90, 110), "short"),
        ((90, 110, 110), "short"),
        ((90, 100, 110), "short"),
        ((100, 100, 100), "short"),
    ],
)
def test_vote_uses_three_lookbacks_without_changing_trend40(db, earlier_closes, vote_side):
    closes = {offset: daily(offset, 100) for offset in range(1, 62)}
    for offset, close in zip((21, 41, 61), earlier_closes):
        closes[offset] = daily(offset, close)
    record_observations(db, list(closes.values()) + [price_bar()])
    trend_book.run_once(db, now=NOW)
    sides = dict(db.execute("SELECT predictor, side FROM paper_trades").fetchall())
    trend40_side = "long" if 100 > earlier_closes[1] else "short"
    assert sides == {"trend_vote": vote_side, "trend40": trend40_side, "acct500": trend40_side}


@pytest.mark.parametrize("count", [0, 40, 41, 60])
def test_vote_fewer_than_61_closes_skips_trade(db, count):
    record_observations(db, rising_closes(count=count) + [price_bar()])
    trend_book.run_once(db, now=NOW)
    state = get_or_create_state(db, predictor="trend_vote", instrument=INSTRUMENT, now_iso=NOW.isoformat())
    assert state.position_qty == 0
    assert db.execute("SELECT count(*) FROM paper_trades WHERE predictor='trend_vote'").fetchone()[0] == 0


def test_fewer_than_41_closes_skips_instrument(db):
    record_observations(db, rising_closes(count=40))
    record_observations(db, [price_bar()])
    trend_book.run_once(db, now=NOW)
    state = get_or_create_state(db, predictor="trend40", instrument=INSTRUMENT, now_iso=NOW.isoformat())
    assert state.position_qty == 0
    assert db.execute("SELECT count(*) FROM paper_trades").fetchone()[0] == 0


@pytest.mark.parametrize("book, count", [("trend40", 41), ("trend_vote", 61)])
def test_todays_incomplete_candle_is_ignored(db, book, count):
    closes = rising_closes(count=count)
    record_observations(db, closes)
    # A today-dated candle with a value that would flip the signal if counted.
    record_observations(db, [daily(0, 1.0)])
    record_observations(db, [price_bar()])
    trend_book.run_once(db, now=NOW)
    state = get_or_create_state(db, predictor=book, instrument=INSTRUMENT, now_iso=NOW.isoformat())
    assert state.position_qty > 0


def test_no_trade_when_side_already_matches(db):
    record_observations(db, rising_closes())
    record_observations(db, [price_bar()])
    execute_paper_trade(db, predictor="trend40", instrument=INSTRUMENT, target_side="long",
                         price=100, now_iso=(NOW - timedelta(days=1)).isoformat(),
                         starting_cash=1000, reason="pre-existing long")
    trend_book.run_once(db, now=NOW)
    assert db.execute("SELECT count(*) FROM paper_trades WHERE predictor='trend40'").fetchone()[0] == 1


def test_flip_when_side_differs(db):
    record_observations(db, falling_closes())
    record_observations(db, [price_bar()])
    execute_paper_trade(db, predictor="trend40", instrument=INSTRUMENT, target_side="long",
                         price=100, now_iso=(NOW - timedelta(days=1)).isoformat(),
                         starting_cash=1000, reason="pre-existing long")
    trend_book.run_once(db, now=NOW)
    state = get_or_create_state(db, predictor="trend40", instrument=INSTRUMENT, now_iso=NOW.isoformat())
    assert state.position_qty < 0
    trades = db.execute("SELECT side FROM paper_trades WHERE predictor='trend40' ORDER BY id").fetchall()
    assert [row["side"] for row in trades] == ["long", "flat", "short"]


def test_second_run_same_day_makes_no_new_trades(db):
    record_observations(db, rising_closes(count=61))
    record_observations(db, [price_bar()])
    trend_book.run_once(db, now=NOW)
    count = db.execute("SELECT count(*) FROM paper_trades").fetchone()[0]
    assert count == 3
    record_observations(db, [daily(1, 1.0), price_bar(price=101, age_seconds=5)])
    trend_book.run_once(db, now=NOW + timedelta(minutes=1))
    assert db.execute("SELECT count(*) FROM paper_trades").fetchone()[0] == count
    assert db.execute("SELECT count(*) FROM trend_rebalance_claims").fetchone()[0] == 1
    marks = db.execute(
        "SELECT predictor, mark_price FROM paper_equity_curve ORDER BY id DESC LIMIT 3"
    ).fetchall()
    assert dict(marks) == {"trend40": 101.0, "trend_vote": 101.0, "acct500": 101.0}


@pytest.mark.parametrize("book", ["trend40", "trend_vote"])
def test_stale_price_skips_instrument(db, book):
    record_observations(db, rising_closes(count=61))
    record_observations(db, [price_bar(age_seconds=200)])
    trend_book.run_once(db, now=NOW)
    state = get_or_create_state(db, predictor=book, instrument=INSTRUMENT, now_iso=NOW.isoformat())
    assert state.position_qty == 0
    assert db.execute("SELECT count(*) FROM paper_trades").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM paper_equity_curve").fetchone()[0] == 0


@pytest.mark.parametrize("book", ["trend40", "trend_vote", "acct500"])
def test_risk_interlock_closes_open_position_and_opens_nothing(db, monkeypatch, book):
    record_observations(db, rising_closes(count=61))
    record_observations(db, [price_bar()])
    execute_paper_trade(db, predictor=book, instrument=INSTRUMENT, target_side="short",
                         price=100, now_iso=(NOW - timedelta(days=1)).isoformat(),
                         starting_cash=1000, reason="pre-existing short")
    monkeypatch.setattr(trend_book, "check_and_enforce",
                         lambda *a, **k: RiskDecision(allowed=False, reason="test block"))
    trend_book.run_once(db, now=NOW)
    state = get_or_create_state(db, predictor=book, instrument=INSTRUMENT, now_iso=NOW.isoformat())
    assert state.position_qty == 0
    trades = db.execute("SELECT side, reason FROM paper_trades WHERE predictor=? ORDER BY id", (book,)).fetchall()
    assert [row["side"] for row in trades] == ["short", "flat"]
    assert trades[-1]["reason"].startswith(f"{book} RISK INTERLOCK forced exit:")


def test_account_book_starts_with_its_own_cash(db):
    record_observations(db, [price_bar()])
    trend_book.run_once(db, now=NOW)
    cash = dict(db.execute("SELECT predictor, starting_cash FROM paper_portfolio_state").fetchall())
    assert cash == {"trend40": 1000.0, "trend_vote": 1000.0, "acct500": 62.5}


def test_account_book_opens_first_position_on_a_claimed_day(db):
    record_observations(db, rising_closes(count=61) + [price_bar()])
    trend_book.initialize_trend_book(db)
    db.execute("INSERT INTO trend_rebalance_claims VALUES (?, ?)", (NOW.date().isoformat(), NOW.isoformat()))
    trend_book.run_once(db, now=NOW)
    trades = db.execute("SELECT predictor, side, reason FROM paper_trades").fetchall()
    assert [(row["predictor"], row["side"]) for row in trades] == [("acct500", "long")]
    assert trades[0]["reason"].startswith("acct500: 40d return +")

    # Once it has traded, it waits for the next day like the other books.
    record_observations(db, [daily(1, 1.0), price_bar(price=101, age_seconds=5)])
    trend_book.run_once(db, now=NOW + timedelta(minutes=1))
    assert db.execute("SELECT count(*) FROM paper_trades").fetchone()[0] == 1
