"""Tests for the unattended cycle's fallback and diagnostic journal."""

from datetime import UTC, datetime
import json
from pathlib import Path

from andy_trader.store import Candle
import run_cycle


def test_cycle_falls_back_only_when_the_primary_reference_is_degraded(
    monkeypatch, tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_collect(*, instruments, intervals, venues, settings):
        del settings
        calls.append(tuple(venues))
        if venues == ("bybit",):
            return [
                Candle(
                    instrument=instruments[0], venue="bybit", interval=intervals[0],
                    open_time=datetime.now(UTC).isoformat(), open=None, high=None,
                    low=None, close=None, volume=None, degraded=True,
                    degraded_reason="untrusted certificate",
                )
            ], [{"venue": "bybit", "instrument": instruments[0],
                 "interval": intervals[0], "reason": "untrusted certificate"}]
        return [
            Candle(
                instrument=instruments[0], venue="coingecko", interval=intervals[0],
                open_time=datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat(),
                open=None, high=None,
                low=None, close=100.0, volume=1.0,
            )
        ], []

    journal = tmp_path / "cycle.jsonl"
    monkeypatch.setattr(run_cycle, "collect", fake_collect)
    monkeypatch.setattr(run_cycle, "CYCLE_LOG_PATH", journal)
    monkeypatch.setattr(run_cycle, "default_database_path", lambda: tmp_path / "c.db")
    monkeypatch.setattr(run_cycle, "load_env_file", lambda _path: None)
    monkeypatch.delenv("CRYPTO_MAX_DATA_AGE_MINUTES", raising=False)

    result = run_cycle.main(
        ["--instruments", "BTC-USD", "--intervals", "1h", "--horizons", "1h",
         "--skip-signals", "--quiet"]
    )

    assert result == 0
    assert calls == [("bybit",), ("coingecko",)]
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    events = [entry["event"] for entry in entries]
    assert events == [
        "cycle_started", "primary_prices_collected", "fallback_prices_collected",
        "signals_collected", "store_updated", "cycle_completed",
    ]
    stored = next(entry for entry in entries if entry["event"] == "store_updated")
    assert stored["predictions"] == 2  # only the minimum-history-one baselines can run
