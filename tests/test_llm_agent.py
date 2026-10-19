from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import random
import sqlite3
from uuid import UUID

import pytest

from andy_trader.baselines import PredictionContext, default_baselines
from andy_trader.llm_agent import LlmPredictor, _ordered_prices, _parse, ask_model, build_predictors, render_context
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
    monkeypatch.delenv("LLM_LIST_ORDER", raising=False)


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


def test_missing_history_sorts_last_after_known_moves(tmp_path):
    with _db(tmp_path) as connection:
        ordered = _ordered_prices(connection, ("AAA-USD", "BTC-USD", "ETH-USD"))
        assert [row[0] for row in ordered] == ["ETH-USD", "BTC-USD", "AAA-USD"]


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
        assert len(calls) == 2
        rows = fetch_decisions(connection, 10)
        assert len(rows) == 2
        assert {row["status"] for row in rows} == {"ok", "missing"}
        assert len({row["batch_id"] for row in rows}) == 1
        assert len({row["rendered_context"] for row in rows}) == 2


def test_complete_batch_does_not_retry(monkeypatch, tmp_path):
    _configured(monkeypatch)
    contexts = []
    def fake(context):
        contexts.append(context)
        return _reply(*({"instrument": name, "p_up": 0.6} for name in INSTRUMENTS))
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", fake)
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection)
        assert len(contexts) == 1
        assert {row["request_count"] for row in fetch_decisions(connection, 10)} == {1}


def test_retry_only_missing_instruments_and_saves_response(monkeypatch, tmp_path):
    _configured(monkeypatch)
    instruments = (*INSTRUMENTS, "SOL-USD", "XRP-USD")
    contexts = []
    first = _reply({"instrument": "BTC-USD", "p_up": 0.6})
    retry = _reply(*({"instrument": name, "p_up": 0.7} for name in instruments[1:]))
    def fake(context):
        contexts.append(context)
        return first if len(contexts) == 1 else retry
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", fake)
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(instruments), connection)
        assert len(contexts) == 2
        assert "BTC-USD" not in contexts[1]
        assert all(name in contexts[1] for name in instruments[1:])
        rows = {row["instrument"]: row for row in fetch_decisions(connection, 10)}
        assert len(rows) == 4
        assert len({row["batch_id"] for row in rows.values()}) == 1
        assert {row["request_count"] for row in rows.values()} == {2}
        assert rows["BTC-USD"]["raw_response"] == json.dumps(first, sort_keys=True)
        assert all(rows[name]["raw_response"] == json.dumps(retry, sort_keys=True)
                   for name in instruments[1:])


def test_partial_retry_leaves_one_missing(monkeypatch, tmp_path):
    _configured(monkeypatch)
    instruments = (*INSTRUMENTS, "SOL-USD", "XRP-USD")
    replies = iter((_reply({"instrument": "BTC-USD", "p_up": 0.6}),
                    _reply({"instrument": "ETH-USD", "p_up": 0.7},
                           {"instrument": "SOL-USD", "p_up": 0.8})))
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _: next(replies))
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(instruments), connection)
        rows = {row["instrument"]: row for row in fetch_decisions(connection, 10)}
        assert {name for name, row in rows.items() if row["status"] == "ok"} == set(instruments[:3])
        assert (rows["XRP-USD"]["p_up"], rows["XRP-USD"]["status"], rows["XRP-USD"]["reason"]) == (
            0.5, "missing", "No call for instrument")


@pytest.mark.parametrize("retry_result", [TimeoutError("late"), {"choices": [{"message": {"content": "bad json"}}]}])
def test_failed_retry_preserves_first_answer(monkeypatch, tmp_path, retry_result):
    _configured(monkeypatch)
    count = 0
    def fake(_):
        nonlocal count
        count += 1
        if count == 1:
            return _reply({"instrument": "BTC-USD", "p_up": 0.6})
        if isinstance(retry_result, Exception):
            raise retry_result
        return retry_result
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", fake)
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.6
        rows = {row["instrument"]: row for row in fetch_decisions(connection, 10)}
        assert count == 2
        assert rows["BTC-USD"]["status"] == "ok"
        assert rows["ETH-USD"]["status"] == "missing"
        assert rows["ETH-USD"]["request_count"] == 2


@pytest.mark.parametrize("cap,expected_calls", [(1, 1), (2, 2)])
def test_daily_cap_limits_and_counts_retry(monkeypatch, tmp_path, cap, expected_calls):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_DAILY_CALLS", str(cap))
    monkeypatch.setenv("LLM_MIN_MINUTES", "0")
    calls = []
    def fake(context):
        calls.append(context)
        return _reply({"instrument": "BTC-USD", "p_up": 0.6})
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", fake)
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection)
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=1))
        assert len(calls) == expected_calls
        rows = fetch_decisions(connection, 10)
        assert {row["status"] for row in rows} == {"ok", "missing", "capped"}
        assert max(row["request_count"] for row in rows) == expected_calls


def test_lower_probability_is_clamped(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _context: _reply(
        {"instrument": "BTC-USD", "p_up": 0.0, "reason": "Down."}
    ))
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == pytest.approx(0.35)


def test_parse_accepts_multi_sentence_decimal_reason_and_extra_fields():
    parsed = _parse(_reply(
        {"instrument": "BTC-USD", "p_up": 0.6,
         "reason": "Funding is 0.05, so crowding is high.\nWatch for a reversal.", "extra": True},
    ), INSTRUMENTS)
    assert parsed["BTC-USD"] == (0.6, "Funding is 0.05, so crowding is high. Watch for a reversal.")


def test_parse_cuts_reason_to_300_characters():
    parsed = _parse(_reply({"instrument": "BTC-USD", "p_up": 0.6, "reason": "x" * 350}), INSTRUMENTS)
    assert parsed["BTC-USD"][1] == "x" * 300


def test_one_bad_call_does_not_discard_valid_calls(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _context: _reply(
        {"instrument": "BTC-USD", "p_up": 0.7, "reason": "valid"},
        {"instrument": "ETH-USD", "p_up": 2, "reason": "invalid"},
    ))
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == pytest.approx(0.65)
        rows = {row["instrument"]: row for row in fetch_decisions(connection, 10)}
        assert rows["BTC-USD"]["status"] == "ok"
        assert rows["ETH-USD"]["status"] == "missing"
        assert rows["ETH-USD"]["p_up"] == 0.5


@pytest.mark.parametrize("reply", [
    {"choices": [{"message": {"content": "bad json"}}]},
    {"choices": [{"message": {"content": json.dumps({"other": []})}}]},
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


@pytest.mark.parametrize("mode,at,expected_arm", [
    ("ranked", NOW + timedelta(hours=1), "ranked"),
    ("shuffled", NOW, "shuffled"),
    ("alternate", NOW, "ranked"),
    ("alternate", NOW + timedelta(hours=1), "shuffled"),
    (None, NOW + timedelta(hours=1), "shuffled"),
])
def test_list_order_modes(monkeypatch, tmp_path, mode, at, expected_arm):
    _configured(monkeypatch)
    if mode is not None:
        monkeypatch.setenv("LLM_LIST_ORDER", mode)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _context: _reply())
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection, at=at)
        rows = fetch_decisions(connection, 10)
        by_position = sorted(rows, key=lambda row: row["list_position"])
        assert {row["list_arm"] for row in rows} == {expected_arm}
        seed = rows[0]["list_seed"]
        assert seed == rows[0]["batch_id"]
        assert {row["list_seed"] for row in rows} == {seed}
        expected = ["ETH-USD", "BTC-USD"]
        if expected_arm == "shuffled":
            random.Random(seed).shuffle(expected)
        assert [row["instrument"] for row in by_position] == expected
        context = rows[0]["rendered_context"]
        assert context.index(f"1. {expected[0]}") < context.index(f"2. {expected[1]}")


def test_same_batch_seed_reproduces_shuffle(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_LIST_ORDER", "shuffled")
    monkeypatch.setattr("andy_trader.llm_agent.uuid.uuid4", lambda: UUID(int=1))
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _context: _reply())
    orders = []
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        with _db(directory) as connection:
            _predict(LlmPredictor(INSTRUMENTS), connection)
            rows = sorted(fetch_decisions(connection, 10), key=lambda row: row["list_position"])
            orders.append([row["instrument"] for row in rows])
    assert orders[0] == orders[1]


def test_alternate_switches_arm_within_cache_window(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _context: _reply())
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=59))
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(hours=1))
        assert {row["list_arm"] for row in fetch_decisions(connection, 10)} == {"ranked", "shuffled"}


def test_ledger_migration_runs_twice():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE llm_decisions (id INTEGER PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO llm_decisions (status) VALUES ('ok')")
        initialize_ledger(connection)
        initialize_ledger(connection)
        columns = [row[1] for row in connection.execute("PRAGMA table_info(llm_decisions)")]
        assert columns.count("list_arm") == 1
        assert columns.count("list_seed") == 1
        assert connection.execute("SELECT status FROM llm_decisions").fetchone()[0] == "ok"
