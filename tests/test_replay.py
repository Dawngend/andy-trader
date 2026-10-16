from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from andy_trader.llm_ledger import initialize_ledger, latest_decision, record_decision
from andy_trader.llm_agent import LlmPredictor, TEMPLATE_REV
from andy_trader.replay import replay
from andy_trader.store import Candle, connect, record_observations


def _answer(*calls):
    return {
        "choices": [{"message": {"content": json.dumps({"calls": [
            {"instrument": instrument, "p_up": probability, "reason": "test"}
            for instrument, probability in calls
        ]})}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "cost": 0.02},
    }


def _original(connection, when, batch, instrument, probability, context):
    return record_decision(
        connection, created_at=when.isoformat(), batch_id=batch, instrument=instrument,
        model="expensive", rendered_context=context, p_up=probability, status="ok",
        prompt_tokens=40, completion_tokens=10, cost=0.10,
    )


def test_replay_pairs_scores_costs_and_never_becomes_latest(tmp_path):
    when = datetime(2026, 10, 15, 12, tzinfo=UTC)
    context = "## SECTION: prices\n1. BTC-USD: close 100; 1h 0%\n2. ETH-USD: close 100; 1h 0%\n## SECTION: funding_rate"
    with connect(tmp_path / "test.db") as connection:
        initialize_ledger(connection)
        btc = _original(connection, when, "batch", "BTC-USD", 0.8, context)
        eth = _original(connection, when, "batch", "ETH-USD", 0.4, context)
        for instrument, price in (("BTC-USD", 110), ("ETH-USD", 90)):
            record_observations(connection, [Candle(
                instrument=instrument, venue="bybit", interval="1h", open_time=when.isoformat(),
                open=None, high=None, low=None, close=price, volume=None,
            )], observed_at=(when + timedelta(hours=1)).isoformat())
        seen = []
        def ask(rendered, *, model):
            seen.append((rendered, model))
            return _answer(("BTC-USD", 0.6), ("ETH-USD", 0.6))

        result = replay(connection, model="cheap", ask=ask)
        assert seen == [(context, "cheap")]
        assert result["batches"] == 1 and result["calls"] == 2 and result["settled_calls"] == 2
        assert result["agreement"] == 0.5
        assert result["mean_absolute_difference"] == pytest.approx(0.2)
        assert result["models"]["expensive"]["brier"] == pytest.approx(0.10)
        assert result["models"]["cheap"]["brier"] == pytest.approx(0.26)
        assert result["models"]["expensive"]["tokens_per_100_batches"] == 5000
        assert result["models"]["cheap"]["tokens_per_100_batches"] == 2000
        assert result["cost_ratio"] == pytest.approx(0.2)
        rows = connection.execute("SELECT * FROM llm_decisions WHERE status = 'replay' ORDER BY instrument").fetchall()
        assert [(r["instrument"], r["replay_of"]) for r in rows] == [("BTC-USD", btc), ("ETH-USD", eth)]
        assert all(r["model"] == "cheap" and r["prompt_tokens"] == 12 and r["cost"] == 0.02 for r in rows)
        assert latest_decision(connection, "BTC-USD", when.isoformat())["id"] == btc


def test_open_hour_has_no_brier_and_replays_are_not_sources(tmp_path):
    when = datetime.now(UTC) - timedelta(minutes=10)
    context = "## SECTION: prices\n1. BTC-USD: close 100; 1h 0%\n## SECTION: funding_rate"
    with connect(tmp_path / "test.db") as connection:
        initialize_ledger(connection)
        _original(connection, when, "batch", "BTC-USD", 0.8, context)
        for _ in range(2):
            result = replay(connection, model="cheap", ask=lambda context, *, model: _answer(("BTC-USD", 0.6)))
            assert result["batches"] == 1 and result["settled_calls"] == 0
            assert result["models"]["expensive"]["brier"] is None
            assert result["models"]["cheap"]["brier"] is None
        assert connection.execute("SELECT COUNT(*) FROM llm_decisions WHERE status = 'replay'").fetchone()[0] == 2


def test_trading_does_not_reuse_replay_batch(tmp_path, monkeypatch):
    now = datetime.now(UTC).isoformat()
    monkeypatch.setenv("LLM_MODEL", "cheap")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_LIST_ORDER", "ranked")
    monkeypatch.delenv("LLM_MIN_MINUTES", raising=False)
    monkeypatch.delenv("LLM_DAILY_CALLS", raising=False)
    config = hashlib.sha1(f"cheap|60|{TEMPLATE_REV}|ranked|ranked".encode()).hexdigest()[:8]
    with connect(tmp_path / "test.db") as connection:
        initialize_ledger(connection)
        record_decision(connection, created_at=now, batch_id="old-replay", instrument="BTC-USD",
                        model="cheap", config_rev=config, status="replay", p_up=0.65)
        asked = []
        def fake_ask(context):
            asked.append(context)
            return _answer(("BTC-USD", 0.4))
        monkeypatch.setattr("andy_trader.llm_agent.ask_model", fake_ask)
        result = LlmPredictor(("BTC-USD",))._load_batch(connection, now)
        assert result == {"BTC-USD": 0.4}
        assert len(asked) == 1
