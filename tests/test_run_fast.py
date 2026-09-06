"""Tests for the 1-minute fast runner."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import run_fast
from andy_trader.collector import CollectorError
from andy_trader.fast_momentum import PREDICTOR_NAME
from andy_trader.store import Candle, Prediction, connect, record_prediction


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "fast.db")


def test_a_failing_collection_never_raises(tmp_path: Path, monkeypatch, capsys) -> None:
    """This runs 1,440 times a day. A transient network failure must be logged
    and shrugged off, not raised: an unhandled exception here would fill Task
    Scheduler with failures and, worse, stop the run that settles due calls."""

    def _boom(**_kwargs):
        raise CollectorError("venue unreachable")

    monkeypatch.setattr(run_fast, "collect", _boom)
    monkeypatch.setattr(run_fast, "FAST_LOG_PATH", tmp_path / "fast.jsonl")

    exit_code = run_fast.main(["--database", _db(tmp_path), "--instruments", "BTC-USD"])

    assert exit_code == 1
    assert "failed" in capsys.readouterr().err
    assert "fast_pass_failed" in (tmp_path / "fast.jsonl").read_text()


def test_a_quiet_pass_still_records_what_it_did(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_fast, "collect", lambda **_kwargs: ([], []))
    monkeypatch.setattr(run_fast, "FAST_LOG_PATH", tmp_path / "fast.jsonl")

    exit_code = run_fast.main(
        ["--database", _db(tmp_path), "--instruments", "BTC-USD", "--quiet"]
    )

    assert exit_code == 0
    logged = (tmp_path / "fast.jsonl").read_text()
    assert "fast_pass" in logged
    assert '"calls": []' in logged


def test_collected_bars_are_written_to_the_store(tmp_path: Path, monkeypatch) -> None:
    from andy_trader.fast_momentum import load_minute_closes
    from andy_trader.store import connect

    bar = Candle(
        instrument="BTC-USD",
        venue="binance",
        interval="1m",
        open_time="2026-09-06T12:00:00+00:00",
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        volume=1.0,
    )
    monkeypatch.setattr(run_fast, "collect", lambda **_kwargs: ([bar], []))
    monkeypatch.setattr(run_fast, "FAST_LOG_PATH", tmp_path / "fast.jsonl")
    database = _db(tmp_path)

    assert run_fast.main(["--database", database, "--instruments", "BTC-USD", "--quiet"]) == 0

    connection = connect(Path(database))
    assert len(load_minute_closes(connection, "BTC-USD")) == 1


def test_a_fresh_call_goes_through_the_paper_trade_path(tmp_path: Path, monkeypatch) -> None:
    """The fast strategy must actually be able to trade, not just log
    predictions -- it was originally wired to settlement and scoring only,
    which meant it could never appear on the paper book no matter what it
    scored. This is the regression test for that gap."""
    database = _db(tmp_path)
    connection = connect(Path(database))
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    record_prediction(
        connection,
        Prediction(
            predictor=PREDICTOR_NAME,
            instrument="BTC-USD",
            horizon="2m",
            probability_up=0.9,
            reference_price=100.0,
            created_at=(now - timedelta(minutes=1)).isoformat(),
            resolves_at=(now + timedelta(minutes=1)).isoformat(),
        ),
    )
    from andy_trader.store import Candle as _Candle, record_observations

    record_observations(
        connection,
        [
            _Candle(
                instrument="BTC-USD", venue="binance", interval="1m",
                open_time=now.isoformat(), open=100.0, high=100.0, low=100.0,
                close=100.0, volume=1.0,
            )
        ],
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(run_fast, "collect", lambda **_kwargs: ([], []))
    monkeypatch.setattr(run_fast, "FAST_LOG_PATH", tmp_path / "fast.jsonl")

    exit_code = run_fast.main(["--database", database, "--instruments", "BTC-USD"])

    assert exit_code == 0
    logged = (tmp_path / "fast.jsonl").read_text()
    assert '"trades"' in logged
    assert '"instrument": "BTC-USD"' in logged
    connection = connect(Path(database))
    row = connection.execute(
        "SELECT * FROM paper_portfolio_state WHERE predictor = ? AND instrument = 'BTC-USD'",
        (PREDICTOR_NAME,),
    ).fetchone()
    assert row is not None, "the skill gate should have created a book, even if it stayed flat"


def test_a_stale_call_is_refused_well_before_the_ordinary_twenty_minute_default(
    tmp_path: Path, monkeypatch
) -> None:
    """A round is 5 minutes long and the decision window is ~2 minutes. The
    project-wide 20-minute freshness default assumes a 15-minute cadence and
    would happily act on a call from several rounds ago."""
    database = _db(tmp_path)
    connection = connect(Path(database))
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    record_prediction(
        connection,
        Prediction(
            predictor=PREDICTOR_NAME,
            instrument="BTC-USD",
            horizon="2m",
            probability_up=0.9,
            reference_price=100.0,
            # Stale under this strategy's own tolerance, fresh under the
            # project-wide default -- this must be refused anyway.
            created_at=(now - timedelta(minutes=5)).isoformat(),
            resolves_at=(now - timedelta(minutes=3)).isoformat(),
        ),
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(run_fast, "collect", lambda **_kwargs: ([], []))
    monkeypatch.setattr(run_fast, "FAST_LOG_PATH", tmp_path / "fast.jsonl")

    assert run_fast.main(["--database", database, "--instruments", "BTC-USD"]) == 0

    # A staleness refusal happens before any portfolio row would be created, so
    # the table itself may not exist yet -- that absence IS the assertion.
    connection = connect(Path(database))
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_portfolio_state'"
        )
    }
    if tables:
        row = connection.execute(
            "SELECT position_qty FROM paper_portfolio_state WHERE predictor = ? "
            "AND instrument = 'BTC-USD'",
            (PREDICTOR_NAME,),
        ).fetchone()
        assert row is None or row["position_qty"] == 0


def test_instruments_default_to_btc_when_nothing_is_configured(
    tmp_path: Path, monkeypatch
) -> None:
    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return ([], [])

    monkeypatch.delenv("CRYPTO_FAST_INSTRUMENTS", raising=False)
    monkeypatch.setattr(run_fast, "collect", _capture)
    monkeypatch.setattr(run_fast, "FAST_LOG_PATH", tmp_path / "fast.jsonl")

    run_fast.main(["--database", _db(tmp_path), "--quiet"])

    assert seen["instruments"] == ("BTC-USD",)
    assert seen["intervals"] == ("1m",)
    assert seen["venues"] == ("binance",)
