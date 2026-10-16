import json
import sqlite3

import pytest

from andy_trader.ablation import run_ablation
from andy_trader.llm_agent import LlmPredictor
from andy_trader.llm_ledger import initialize_ledger, latest_decision, record_decision


CONTEXT = "## SECTION: prices\nprice data\n## SECTION: funding_rate\nfunding data\n"


def _connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_ledger(connection)
    return connection


def _original(connection, batch_id="batch", instrument="BTC-USD", p_up=0.6):
    return record_decision(
        connection, created_at="2026-10-16T12:00:00+00:00", batch_id=batch_id,
        instrument=instrument, model="fake-model", template_rev="v1", config_rev="rev",
        list_arm="ranked", list_seed=batch_id, list_position=1,
        rendered_context=CONTEXT, p_up=p_up, status="ok",
    )


def _reply(probabilities):
    calls = [{"instrument": instrument, "p_up": probability, "reason": "test"}
             for instrument, probability in probabilities.items()]
    return {"choices": [{"message": {"content": json.dumps({"calls": calls})}}]}


def test_removes_each_section_and_records_metrics_and_links():
    with _connection() as connection:
        btc = _original(connection)
        eth = _original(connection, instrument="ETH-USD", p_up=0.4)
        seen = []

        def ask(context):
            seen.append(context)
            if "price data" not in context:
                return _reply({"BTC-USD": 0.4, "ETH-USD": 0.7})
            return _reply({"BTC-USD": 0.61, "ETH-USD": 0.4})

        report = run_ablation(connection, ask=ask)
        assert seen == ["## SECTION: funding_rate\nfunding data",
                        "## SECTION: prices\nprice data"]
        assert report["calls_spent"] == 2
        assert report["capped"] is False
        prices = report["sections"]["prices"]
        assert prices["mean_abs_change"] == pytest.approx(0.225)
        assert prices["flip_share"] == 1
        assert prices["no_measurable_effect"] is False
        funding = report["sections"]["funding_rate"]
        assert funding["mean_abs_change"] == pytest.approx(0.005)
        assert funding["flip_share"] == 0
        assert funding["no_measurable_effect"] is True
        rows = connection.execute("SELECT * FROM llm_decisions WHERE status = 'ablation'").fetchall()
        assert len(rows) == 4
        assert {row["replay_of"] for row in rows} == {btc, eth}
        assert all(row["status"] == "ablation" for row in rows)
        assert len({row["batch_id"] for row in rows}) == 2


def test_latest_batches_and_call_cap():
    with _connection() as connection:
        old = _original(connection, batch_id="old")
        new = _original(connection, batch_id="new")
        record_decision(connection, batch_id="ignored", status="ablation", rendered_context=CONTEXT)
        report = run_ablation(connection, batches=2, max_calls=1,
                              ask=lambda _: _reply({"BTC-USD": 0.6}))
        assert report["calls_spent"] == 1
        assert report["capped"] is True
        assert set(report["sections"]) == {"prices"}
        assert connection.execute("SELECT replay_of FROM llm_decisions WHERE status = 'ablation' AND replay_of IS NOT NULL").fetchone()[0] == new
        assert old != new


def test_default_ask_uses_original_model(monkeypatch):
    seen = []
    monkeypatch.setattr("andy_trader.ablation.ask_model", lambda context, *, model: (
        seen.append(model) or _reply({"BTC-USD": 0.6})
    ))
    with _connection() as connection:
        _original(connection)
        run_ablation(connection)
    assert seen == ["fake-model", "fake-model"]


def test_ablation_rows_never_enter_trading_cache(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_LIST_ORDER", "ranked")
    with _connection() as connection:
        original = _original(connection)
        record_decision(connection, created_at="2026-10-16T12:00:00+00:00",
                        batch_id="batch", instrument="ETH-USD", config_rev="rev",
                        status="ablation", p_up=0.1, replay_of=original)
        assert latest_decision(connection, "ETH-USD", "2026-10-16T11:00:00+00:00") is None
        assert latest_decision(connection, "BTC-USD", "2026-10-16T11:00:00+00:00")["id"] == original

        # An ablation row with the same batch ID cannot satisfy a live cache hit.
        from andy_trader import llm_agent
        monkeypatch.setattr(llm_agent, "_ordered_prices", lambda *_: [("BTC-USD", [], None), ("ETH-USD", [], None)])
        monkeypatch.setattr(llm_agent, "render_context", lambda *_: CONTEXT)
        monkeypatch.setattr(llm_agent, "ask_model", lambda _: _reply({"BTC-USD": 0.7, "ETH-USD": 0.8}))
        # Use the exact config revision that the predictor computes.
        import hashlib
        rev = hashlib.sha1(b"fake-model|60|v1|ranked|ranked").hexdigest()[:8]
        connection.execute("UPDATE llm_decisions SET config_rev = ? WHERE batch_id = 'batch'", (rev,))
        probabilities = LlmPredictor(("BTC-USD", "ETH-USD"))._load_batch(
            connection, "2026-10-16T12:30:00+00:00")
        assert probabilities == {"BTC-USD": 0.65, "ETH-USD": 0.65}
