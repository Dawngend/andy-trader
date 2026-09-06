"""Tests for the 1-minute fast runner."""

from pathlib import Path

import pytest

import run_fast
from andy_trader.collector import CollectorError
from andy_trader.store import Candle


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
