from __future__ import annotations

import urllib.error
import pytest

from andy_trader import funding_gap
from andy_trader.store import connect
import run_funding_gap


@pytest.fixture
def db(tmp_path):
    with connect(tmp_path / "test_funding_gap.db") as connection:
        yield connection


def test_three_venues_annualized_8h_and_4h(db, monkeypatch):
    """(a) Three venues give three rows with the right annualized value for an 8h and 4h interval."""
    monkeypatch.setattr(funding_gap, "INSTRUMENTS", ("BTC-USD",))

    # 8-hour interval payload
    def fetch_8h(url: str):
        if "binance" in url:
            return {"lastFundingRate": "0.0001"}
        if "bybit" in url:
            return {"retCode": 0, "result": {"list": [{"fundingRate": "0.0002", "fundingIntervalHour": 8}]}}
        if "okx" in url:
            return {
                "code": "0",
                "data": [
                    {
                        "fundingRate": "0.0003",
                        "fundingTime": "1672531200000",
                        "nextFundingTime": "1672560000000",  # 28,800,000 ms = 8h
                    }
                ],
            }
        raise ValueError(f"Unexpected url {url}")

    written, failed = funding_gap.run_once(db, fetch=fetch_8h, now="2026-10-25T00:00:00+00:00")
    assert written == 3
    assert failed == 0

    rows_8h = db.execute(
        "SELECT venue, funding_rate, interval_hours, annualized FROM funding_gap_observations WHERE observed_at = '2026-10-25T00:00:00+00:00'"
    ).fetchall()
    by_venue_8h = {r["venue"]: (r["funding_rate"], r["interval_hours"], r["annualized"]) for r in rows_8h}

    # Binance 8h
    assert by_venue_8h["binance"][0] == pytest.approx(0.0001)
    assert by_venue_8h["binance"][1] == 8.0
    assert by_venue_8h["binance"][2] == pytest.approx(0.0001 * 8760 / 8)

    # Bybit 8h
    assert by_venue_8h["bybit"][0] == pytest.approx(0.0002)
    assert by_venue_8h["bybit"][1] == 8.0
    assert by_venue_8h["bybit"][2] == pytest.approx(0.0002 * 8760 / 8)

    # OKX 8h
    assert by_venue_8h["okx"][0] == pytest.approx(0.0003)
    assert by_venue_8h["okx"][1] == 8.0
    assert by_venue_8h["okx"][2] == pytest.approx(0.0003 * 8760 / 8)

    # 4-hour interval payload
    def fetch_4h(url: str):
        if "binance" in url:
            return {"lastFundingRate": "0.0001", "fundingIntervalHours": 4}
        if "bybit" in url:
            return {"retCode": 0, "result": {"list": [{"fundingRate": "0.0002", "fundingIntervalHour": 4}]}}
        if "okx" in url:
            return {
                "code": "0",
                "data": [
                    {
                        "fundingRate": "0.0003",
                        "fundingTime": "1672531200000",
                        "nextFundingTime": "1672545600000",  # 14,400,000 ms = 4h
                    }
                ],
            }
        raise ValueError(f"Unexpected url {url}")

    written_4h, failed_4h = funding_gap.run_once(db, fetch=fetch_4h, now="2026-10-25T01:00:00+00:00")
    assert written_4h == 3
    assert failed_4h == 0

    rows_4h = db.execute(
        "SELECT venue, funding_rate, interval_hours, annualized FROM funding_gap_observations WHERE observed_at = '2026-10-25T01:00:00+00:00'"
    ).fetchall()
    by_venue_4h = {r["venue"]: (r["funding_rate"], r["interval_hours"], r["annualized"]) for r in rows_4h}

    assert by_venue_4h["binance"][1] == 4.0
    assert by_venue_4h["binance"][2] == pytest.approx(0.0001 * 8760 / 4)

    assert by_venue_4h["bybit"][1] == 4.0
    assert by_venue_4h["bybit"][2] == pytest.approx(0.0002 * 8760 / 4)

    assert by_venue_4h["okx"][1] == 4.0
    assert by_venue_4h["okx"][2] == pytest.approx(0.0003 * 8760 / 4)


def test_failing_venue_is_skipped_and_run_still_writes_others(db, monkeypatch):
    """(b) A failing venue is skipped and the run still writes the others."""
    monkeypatch.setattr(funding_gap, "INSTRUMENTS", ("BTC-USD",))

    def fetch_fail_binance(url: str):
        if "binance" in url:
            raise urllib.error.URLError("Connection refused")
        if "bybit" in url:
            return {"result": {"list": [{"fundingRate": "0.0001"}]}}
        if "okx" in url:
            return {"data": [{"fundingRate": "0.0002"}]}
        raise ValueError("Unexpected url")

    written, failed = funding_gap.run_once(db, fetch=fetch_fail_binance)
    assert written == 2
    assert failed == 1

    rows = db.execute("SELECT venue FROM funding_gap_observations").fetchall()
    venues = {r["venue"] for r in rows}
    assert venues == {"bybit", "okx"}


def test_malformed_payload_is_skipped(db, monkeypatch):
    """(c) A malformed payload is skipped."""
    monkeypatch.setattr(funding_gap, "INSTRUMENTS", ("BTC-USD",))

    def fetch_malformed(url: str):
        if "binance" in url:
            return "<html>502 Bad Gateway</html>"
        if "bybit" in url:
            return {"retCode": 10001, "retMsg": "params error"}
        if "okx" in url:
            return {"data": [{"fundingRate": "0.0005"}]}
        raise ValueError("Unexpected url")

    written, failed = funding_gap.run_once(db, fetch=fetch_malformed)
    assert written == 1
    assert failed == 2

    rows = db.execute("SELECT venue, funding_rate FROM funding_gap_observations").fetchall()
    assert len(rows) == 1
    assert rows[0]["venue"] == "okx"
    assert rows[0]["funding_rate"] == pytest.approx(0.0005)


def test_report_gap_equals_highest_minus_lowest(db):
    """(d) The report gap equals highest minus lowest."""
    funding_gap.initialize_database(db)
    now_iso = "2026-10-25T12:00:00+00:00"

    # Rates: Binance 0.05, Bybit 0.12, OKX 0.02
    for venue, ann in (("binance", 0.05), ("bybit", 0.12), ("okx", 0.02)):
        rate = ann * 8 / 8760
        db.execute(
            """
            INSERT INTO funding_gap_observations
            (observed_at, instrument, venue, funding_rate, interval_hours, annualized)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now_iso, "BTC-USD", venue, rate, 8.0, ann),
        )
    db.commit()

    report = funding_gap.compute_instrument_report(db, "BTC-USD")
    # Highest (0.12) - lowest (0.02) = 0.10
    assert report["latest_gap"] == pytest.approx(0.10)

    text = funding_gap.format_report(db)
    assert "0.1000" in text or "10.00%" in text


def test_report_counts_snapshots_and_longest_run_above_15_percent(db):
    """(e) The report counts snapshots above 15 percent and longest run on a hand-made series."""
    funding_gap.initialize_database(db)

    # 5 snapshots with known annualized gaps:
    # 0: gap 0.20 (>15%) -> run=1
    # 1: gap 0.18 (>15%) -> run=2
    # 2: gap 0.10 (<=15%) -> run=0
    # 3: gap 0.25 (>15%) -> run=1
    # 4: gap 0.05 (<=15%) -> run=0
    gaps = [0.20, 0.18, 0.10, 0.25, 0.05]
    for idx, gap in enumerate(gaps):
        obs_time = f"2026-10-25T{idx:02d}:00:00+00:00"
        # Venue A at 0.00 annualized
        db.execute(
            """
            INSERT INTO funding_gap_observations
            (observed_at, instrument, venue, funding_rate, interval_hours, annualized)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (obs_time, "BTC-USD", "bybit", 0.0, 8.0, 0.0),
        )
        # Venue B at gap annualized
        db.execute(
            """
            INSERT INTO funding_gap_observations
            (observed_at, instrument, venue, funding_rate, interval_hours, annualized)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (obs_time, "BTC-USD", "binance", gap * 8 / 8760, 8.0, gap),
        )
    db.commit()

    report = funding_gap.compute_instrument_report(db, "BTC-USD")
    assert report["snapshots_above_15"] == 3
    assert report["longest_consecutive_above_15"] == 2

    text = funding_gap.format_report(db)
    assert "Snapshots stored: 5" in text
    assert "Snapshots above 15%: 3" in text
    assert "Longest run above 15%: 2" in text


def test_okx_interval_read_from_funding_times():
    """(f) The OKX interval is read from fundingTime and nextFundingTime."""
    # 4 hour difference in ms
    payload_4h = {
        "code": "0",
        "data": [
            {
                "fundingRate": "0.0001",
                "fundingTime": "1672531200000",
                "nextFundingTime": "1672545600000",
            }
        ],
    }
    rate, interval = funding_gap.parse_okx(payload_4h)
    assert rate == pytest.approx(0.0001)
    assert interval == pytest.approx(4.0)

    # 8 hour difference in ms
    payload_8h = {
        "code": "0",
        "data": [
            {
                "fundingRate": "-0.0002",
                "fundingTime": "1672531200000",
                "nextFundingTime": "1672560000000",
            }
        ],
    }
    rate_8h, interval_8h = funding_gap.parse_okx(payload_8h)
    assert rate_8h == pytest.approx(-0.0002)
    assert interval_8h == pytest.approx(8.0)

    # Missing timestamps fall back to 8 hours
    payload_missing = {
        "code": "0",
        "data": [{"fundingRate": "0.0001"}],
    }
    _, interval_missing = funding_gap.parse_okx(payload_missing)
    assert interval_missing == pytest.approx(8.0)


def test_run_funding_gap_cli(tmp_path, capsys, monkeypatch):
    """Test CLI runs with --database and prints output."""
    monkeypatch.setattr(funding_gap, "INSTRUMENTS", ("BTC-USD",))

    def fake_fetch(url: str):
        if "binance" in url:
            return {"lastFundingRate": "0.0001"}
        if "bybit" in url:
            return {"result": {"list": [{"fundingRate": "0.0002"}]}}
        if "okx" in url:
            return {"data": [{"fundingRate": "0.0003"}]}
        raise ValueError("Unexpected url")

    monkeypatch.setattr(funding_gap, "default_fetch", fake_fetch)

    db_file = tmp_path / "cli.db"
    ret = run_funding_gap.main(["--database", str(db_file)])
    assert ret == 0

    out = capsys.readouterr().out
    assert "Rows written: 3, venues failed: 0" in out

    # Test report CLI
    ret_report = run_funding_gap.main(["--database", str(db_file), "--report"])
    assert ret_report == 0

    report_out = capsys.readouterr().out
    assert "Snapshots stored: 1" in report_out
    assert "BTC-USD:" in report_out
    assert "Latest gap:" in report_out


def test_lock_file_acquisition_and_contention(tmp_path, monkeypatch):
    """Test lock file prevents concurrent runs and handles stale locks."""
    fake_lock = tmp_path / ".funding-gap.lock"
    monkeypatch.setattr(run_funding_gap, "LOCK_PATH", fake_lock)

    token1 = run_funding_gap.acquire_lock()
    assert token1 is not None
    assert fake_lock.exists()

    # Second acquisition while locked should fail
    token2 = run_funding_gap.acquire_lock()
    assert token2 is None

    # Simulate release
    fake_lock.unlink()
    token3 = run_funding_gap.acquire_lock()
    assert token3 is not None
    fake_lock.unlink()


def test_all_eight_instruments_mapped():
    """Verify all 8 instruments from run_probe_watch are present and map to expected symbols."""
    expected_instruments = (
        "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD",
        "DOGE-USD", "ADA-USD", "AVAX-USD", "LINK-USD"
    )
    assert funding_gap.INSTRUMENTS == expected_instruments
    for inst in expected_instruments:
        binance_sym, bybit_sym, okx_sym = funding_gap.instrument_to_symbols(inst)
        coin = inst.split("-")[0]
        assert binance_sym == f"{coin}USDT"
        assert bybit_sym == f"{coin}USDT"
        assert okx_sym == f"{coin}-USDT-SWAP"


def test_empty_database_report(db):
    """Verify report handles an empty database gracefully."""
    text = funding_gap.format_report(db)
    assert "Snapshots stored: 0" in text
    assert "BTC-USD:" in text
    assert "No observations recorded" in text


def test_prompt_btc_funding_gap_example(db):
    """Verify the prompt's real-world example calculation:
    Binance +0.0009%, Bybit -0.0009%, OKX +0.0045% gives ~5.9% annualized gap."""
    funding_gap.initialize_database(db)
    now_iso = "2026-10-25T00:00:00+00:00"

    # +0.0009% = 0.000009
    rates = {
        "binance": 0.000009,
        "bybit": -0.000009,
        "okx": 0.000045,
    }
    for venue, rate in rates.items():
        ann = rate * (8760 / 8)
        db.execute(
            """
            INSERT INTO funding_gap_observations
            (observed_at, instrument, venue, funding_rate, interval_hours, annualized)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now_iso, "BTC-USD", venue, rate, 8.0, ann),
        )
    db.commit()

    report = funding_gap.compute_instrument_report(db, "BTC-USD")
    # Gap is 0.049275 - (-0.009855) = 0.05913 (~5.91% / year)
    assert report["latest_gap"] == pytest.approx(0.05913, abs=1e-4)

