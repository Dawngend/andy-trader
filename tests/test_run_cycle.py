"""Tests for the unattended cycle's fallback and diagnostic journal."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path

from andy_trader.store import Candle
import run_cycle


@dataclass(frozen=True)
class _FakeRegistryEntry:
    model_id: str
    promoted: bool
    holdout_brier_skill: float
    base_rate_brier_skill: float
    promotion_reason: str


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
    assert "paper_trade_completed" not in events  # opt-in only; nothing was configured


def _fake_price_collect(*, instruments, intervals, venues, settings):
    del venues, settings
    return [
        Candle(
            instrument=instruments[0], venue="bybit", interval=intervals[0],
            open_time=datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat(),
            open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0,
        )
    ], []


def test_paper_trade_is_opt_in_and_runs_when_explicitly_configured(monkeypatch, tmp_path: Path) -> None:
    journal = tmp_path / "cycle.jsonl"
    monkeypatch.setattr(run_cycle, "collect", _fake_price_collect)
    monkeypatch.setattr(run_cycle, "CYCLE_LOG_PATH", journal)
    monkeypatch.setattr(run_cycle, "default_database_path", lambda: tmp_path / "c.db")
    monkeypatch.setattr(run_cycle, "load_env_file", lambda _path: None)
    monkeypatch.delenv("CRYPTO_MAX_DATA_AGE_MINUTES", raising=False)
    monkeypatch.delenv("CRYPTO_PAPER_TRADE_PAIRS", raising=False)

    result = run_cycle.main(
        ["--instruments", "BTC-USD", "--intervals", "1h", "--horizons", "1h",
         "--skip-signals", "--quiet", "--paper-trade", "baseline:coin_flip=BTC-USD"]
    )

    assert result == 0
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    paper_event = next(entry for entry in entries if entry["event"] == "paper_trade_completed")
    assert paper_event["attempts"] == [
        {
            "predictor": "baseline:coin_flip",
            "instrument": "BTC-USD",
            "traded": False,  # coin_flip's 0.5 sits exactly at the flat threshold
            "side": None,
            "equity": 10_000.0,
            "skipped_reason": None,
            "risk_allowed": True,
            "risk_reason": "within all configured limits",
            "forced_exit": False,
        }
    ]


def test_paper_trade_malformed_entry_is_ignored_not_fatal(monkeypatch, tmp_path: Path, capsys) -> None:
    journal = tmp_path / "cycle.jsonl"
    monkeypatch.setattr(run_cycle, "collect", _fake_price_collect)
    monkeypatch.setattr(run_cycle, "CYCLE_LOG_PATH", journal)
    monkeypatch.setattr(run_cycle, "default_database_path", lambda: tmp_path / "c.db")
    monkeypatch.setattr(run_cycle, "load_env_file", lambda _path: None)
    monkeypatch.delenv("CRYPTO_MAX_DATA_AGE_MINUTES", raising=False)
    monkeypatch.delenv("CRYPTO_PAPER_TRADE_PAIRS", raising=False)

    result = run_cycle.main(
        ["--instruments", "BTC-USD", "--intervals", "1h", "--horizons", "1h",
         "--skip-signals", "--quiet", "--paper-trade", "not-a-valid-pair"]
    )

    assert result == 0  # a malformed config entry must never take down the whole cycle
    assert "Ignoring malformed" in capsys.readouterr().err
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    assert not any(entry["event"] == "paper_trade_completed" for entry in entries)


def _base_env(monkeypatch, tmp_path: Path) -> Path:
    journal = tmp_path / "cycle.jsonl"
    monkeypatch.setattr(run_cycle, "collect", _fake_price_collect)
    monkeypatch.setattr(run_cycle, "CYCLE_LOG_PATH", journal)
    monkeypatch.setattr(run_cycle, "default_database_path", lambda: tmp_path / "c.db")
    monkeypatch.setattr(run_cycle, "load_env_file", lambda _path: None)
    monkeypatch.delenv("CRYPTO_MAX_DATA_AGE_MINUTES", raising=False)
    monkeypatch.delenv("CRYPTO_PAPER_TRADE_PAIRS", raising=False)
    monkeypatch.delenv("CRYPTO_RETRAIN_INSTRUMENTS", raising=False)
    return journal


def test_retraining_is_opt_in_and_only_runs_for_configured_instruments(monkeypatch, tmp_path: Path) -> None:
    journal = _base_env(monkeypatch, tmp_path)
    monkeypatch.setattr(run_cycle, "should_retrain", lambda *a, **k: True)
    monkeypatch.setattr(
        run_cycle, "run_retrain_window",
        lambda conn, *, instrument, **k: _FakeRegistryEntry(
            model_id=f"fake_{instrument}", promoted=True,
            holdout_brier_skill=0.02, base_rate_brier_skill=0.0, promotion_reason="test",
        ),
    )

    result = run_cycle.main(
        ["--instruments", "BTC-USD,ETH-USD", "--intervals", "1h", "--horizons", "1h",
         "--skip-signals", "--quiet", "--retrain-instruments", "BTC-USD"]
    )

    assert result == 0
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    retrain_event = next(entry for entry in entries if entry["event"] == "retrain_completed")
    # Only BTC-USD was configured to retrain, even though ETH-USD is also scheduled.
    assert [a["instrument"] for a in retrain_event["attempts"]] == ["BTC-USD"]
    assert retrain_event["attempts"][0]["promoted"] is True


def test_retraining_skipped_when_not_due_produces_no_journal_entry(monkeypatch, tmp_path: Path) -> None:
    journal = _base_env(monkeypatch, tmp_path)
    monkeypatch.setattr(run_cycle, "should_retrain", lambda *a, **k: False)
    monkeypatch.setattr(run_cycle, "run_retrain_window", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))

    result = run_cycle.main(
        ["--instruments", "BTC-USD", "--intervals", "1h", "--horizons", "1h",
         "--skip-signals", "--quiet", "--retrain-instruments", "BTC-USD"]
    )

    assert result == 0
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    assert not any(entry["event"] == "retrain_completed" for entry in entries)


def test_a_failed_retrain_does_not_crash_the_cycle(monkeypatch, tmp_path: Path) -> None:
    journal = _base_env(monkeypatch, tmp_path)
    monkeypatch.setattr(run_cycle, "should_retrain", lambda *a, **k: True)

    def _boom(*a, **k):
        raise RuntimeError("not enough history")

    monkeypatch.setattr(run_cycle, "run_retrain_window", _boom)

    result = run_cycle.main(
        ["--instruments", "BTC-USD", "--intervals", "1h", "--horizons", "1h",
         "--skip-signals", "--quiet", "--retrain-instruments", "BTC-USD"]
    )

    assert result == 0  # one bad retrain must never take down the whole cycle
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    retrain_event = next(entry for entry in entries if entry["event"] == "retrain_completed")
    assert retrain_event["attempts"][0]["error"] == "not enough history"
    # The rest of the cycle still completed normally.
    assert any(entry["event"] == "cycle_completed" for entry in entries)


def test_live_model_scoring_runs_for_every_instrument_with_no_opt_in_needed(monkeypatch, tmp_path: Path) -> None:
    """The actual release mechanism: unlike retraining, this must run for every
    scheduled instrument with zero configuration -- that's what makes a
    promotion self-releasing instead of something a human has to wire up."""
    journal = _base_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        run_cycle, "predict_with_promoted_model",
        lambda conn, *, instrument, **k: {"predicted": True, "model_id": f"m_{instrument}", "probability_up": 0.6},
    )

    result = run_cycle.main(
        ["--instruments", "BTC-USD,ETH-USD,SOL-USD", "--intervals", "1h", "--horizons", "1h",
         "--skip-signals", "--quiet"]  # deliberately no --retrain-instruments at all
    )

    assert result == 0
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    live_event = next(entry for entry in entries if entry["event"] == "live_model_predictions")
    assert {a["instrument"] for a in live_event["attempts"]} == {"BTC-USD", "ETH-USD", "SOL-USD"}


def test_live_model_scoring_produces_no_journal_noise_when_nothing_promoted(monkeypatch, tmp_path: Path) -> None:
    journal = _base_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        run_cycle, "predict_with_promoted_model",
        lambda conn, *, instrument, **k: {"predicted": False, "reason": "no promoted model for this pair yet"},
    )

    result = run_cycle.main(
        ["--instruments", "BTC-USD", "--intervals", "1h", "--horizons", "1h", "--skip-signals", "--quiet"]
    )

    assert result == 0
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    assert not any(entry["event"] == "live_model_predictions" for entry in entries)
