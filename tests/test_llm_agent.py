from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from andy_trader.baselines import PredictionContext, default_baselines
from andy_trader.llm_agent import LlmPredictor, ask_model, build_predictors, render_context
from andy_trader.llm_ledger import fetch_decisions, initialize_ledger, latest_decision, record_decision
from andy_trader.store import Candle, connect, record_observations


NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
INSTRUMENTS = ("BTC-USD", "ETH-USD")


def _db(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "prices.db")
    candles = []
    for instrument, step in (("BTC-USD", 1), ("ETH-USD", -2)):
        for hours in range(25):
            candles.append(Candle(
                instrument=instrument, venue="bybit", interval="1h",
                open_time=(NOW - timedelta(hours=24 - hours)).isoformat(),
                open=None, high=None, low=None, close=100 + step * hours, volume=None,
            ))
    record_observations(connection, candles)
    initialize_ledger(connection)
    return connection


def _reply(*calls):
    return {"choices": [{"message": {"content": json.dumps({"calls": list(calls)})}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7}}


def _configured(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    monkeypatch.delenv("LLM_MIN_MINUTES", raising=False)
    monkeypatch.delenv("LLM_DAILY_CALLS", raising=False)


def _predict(predictor, connection, instrument="BTC-USD", at=NOW):
    return predictor([], context=PredictionContext(connection, instrument, at.isoformat()))


def test_context_sections_order_and_determinism(tmp_path):
    with _db(tmp_path) as connection:
        first = render_context(connection, INSTRUMENTS, NOW.isoformat())
        assert first == render_context(connection, INSTRUMENTS, NOW.isoformat())
        assert first.index("1. ETH-USD") < first.index("2. BTC-USD")
        for name in ("prices", "funding_rate", "long_ratio", "fear_greed"):
            assert f"## SECTION: {name}" in first
        assert "24h volatility" in first
        assert "unavailable" in first


def test_ledger_is_append_only_and_filters_replays(tmp_path):
    with _db(tmp_path) as connection:
        for status in ("ok", "replay", "ablation"):
            record_decision(connection, created_at=NOW.isoformat(), batch_id=status,
                            instrument="BTC-USD", status=status, p_up=0.5)
        assert len(fetch_decisions(connection, 10)) == 3
        assert latest_decision(connection, "BTC-USD", NOW.isoformat())["status"] == "ok"
    source = Path(__file__).parents[1] / "andy_trader" / "llm_ledger.py"
    assert "UPDATE " not in source.read_text()
    assert "DELETE " not in source.read_text()


def test_http_request_is_one_post_with_only_model_and_messages(monkeypatch):
    _configured(monkeypatch)
    seen = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return json.dumps(_reply()).encode()

    def opener(request, timeout):
        seen.append((request, timeout))
        return Response()

    assert ask_model("context", opener=opener) == _reply()
    request, timeout = seen[0]
    assert len(seen) == 1 and timeout == 120
    assert request.get_method() == "POST"
    assert request.full_url == "https://example.invalid/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer fake"
    body = json.loads(request.data)
    assert set(body) == {"model", "messages"}


def test_batch_clamps_missing_and_reuses(monkeypatch, tmp_path):
    _configured(monkeypatch)
    calls = []
    def fake(context):
        calls.append(context)
        return _reply({"instrument": "BTC-USD", "p_up": 1.0, "reason": "Up."})
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", fake)
    with _db(tmp_path) as connection:
        first = LlmPredictor(INSTRUMENTS)
        assert _predict(first, connection) == 0.65
        assert _predict(first, connection, "ETH-USD") == 0.5
        second = LlmPredictor(INSTRUMENTS)
        assert _predict(second, connection, at=NOW + timedelta(minutes=30)) == 0.65
        assert len(calls) == 1
        rows = fetch_decisions(connection, 10)
        assert len(rows) == 2
        assert {row["status"] for row in rows} == {"ok", "missing"}
        assert len({row["batch_id"] for row in rows}) == 1
        assert len({row["rendered_context"] for row in rows}) == 1


def test_lower_probability_is_clamped(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _context: _reply(
        {"instrument": "BTC-USD", "p_up": 0.0, "reason": "Down."}
    ))
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == pytest.approx(0.35)


@pytest.mark.parametrize("reply", [
    {"choices": [{"message": {"content": "bad json"}}]},
    _reply({"instrument": "BTC-USD", "p_up": 1.1, "reason": "Up."}),
])
def test_bad_json_or_probability_writes_error(monkeypatch, tmp_path, reply):
    _configured(monkeypatch)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _context: reply)
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.5
        assert {row["status"] for row in fetch_decisions(connection, 10)} == {"error"}


def test_transport_failure_and_daily_cap(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_DAILY_CALLS", "1")
    def failing(_context):
        raise OSError("offline")
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", failing)
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.5
        monkeypatch.setenv("LLM_MIN_MINUTES", "0")
        assert _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=1)) == 0.5
        assert {row["status"] for row in fetch_decisions(connection, 10)} == {"error", "capped"}


def test_disabled_predictor_matches_baselines(monkeypatch, tmp_path):
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    assert [p.prediction_name for p in build_predictors(INSTRUMENTS)] == [p.prediction_name for p in default_baselines()]
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.5
        assert fetch_decisions(connection, 10) == []
