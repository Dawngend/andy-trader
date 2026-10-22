from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import random
import sqlite3
import subprocess
from urllib.error import HTTPError
from uuid import UUID

import pytest

from andy_trader.ablation import run_ablation
from andy_trader.baselines import PredictionContext, default_baselines
from andy_trader.llm_agent import CLI_TIMEOUT_SECONDS, LlmPredictor, _messages, _ordered_prices, _parse, ask_cli, ask_model, build_predictors, render_context
from andy_trader.llm_ledger import fetch_decisions, initialize_ledger, latest_decision, record_decision
from andy_trader.portfolio import execute_paper_trade
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
    monkeypatch.setenv("LLM_FALLBACKS", "")


def _predict(predictor, connection, instrument="BTC-USD", at=NOW):
    return predictor([], context=PredictionContext(connection, instrument, at.isoformat()))


def test_context_sections_order_and_determinism(tmp_path):
    with _db(tmp_path) as connection:
        first = render_context(connection, INSTRUMENTS, NOW.isoformat())
        assert first == render_context(connection, INSTRUMENTS, NOW.isoformat())
        assert first.index("1. ETH-USD") < first.index("2. BTC-USD")
        for name in ("prices", "funding_rate", "long_ratio", "fear_greed", "recent_results"):
            assert f"## SECTION: {name}" in first
        assert "24h volatility" in first
        assert "unavailable" in first


def test_recent_results_from_closed_probe_trades(tmp_path):
    with _db(tmp_path) as connection:
        for instrument, exits in (("BTC-USD", (110, 90, 120, 80, 105, 95)),
                                  ("ETH-USD", (110, 110, 90))):
            for index, exit_price in enumerate(exits):
                opened = NOW - timedelta(hours=20 - 2 * index)
                for side, price, at, reason in (
                    ("long", 100, opened, "open"),
                    ("flat", exit_price, opened + timedelta(hours=1), f"close {instrument} {index}"),
                ):
                    execute_paper_trade(connection, predictor="probe", instrument=instrument,
                                        target_side=side, price=price, now_iso=at.isoformat(),
                                        reason=reason, fee_bps=0, slippage_bps=0, starting_cash=1000)
        for side, price, reason in (("short", 100, "x10 open"), ("flat", 90, "x10 close")):
            execute_paper_trade(connection, predictor="probe_x10", instrument="BTC-USD",
                                target_side=side, price=price, now_iso=(NOW - timedelta(hours=2)).isoformat(),
                                reason=reason, fee_bps=0, slippage_bps=0, starting_cash=1000,
                                leverage=10)
        execute_paper_trade(connection, predictor="probe", instrument="BTC-USD",
                            target_side="short", price=100, now_iso=(NOW - timedelta(minutes=30)).isoformat(),
                            reason="still open", fee_bps=0, slippage_bps=0)

        section = render_context(connection, INSTRUMENTS, NOW.isoformat()).split("## SECTION: recent_results\n")[1]
        assert "probe book: closed trades 9; wins 5; average pnl +1.11%" in section
        assert "1. ETH-USD:\n  3 closed trades only, too few to mean much" in section
        assert "2. BTC-USD:" in section
        assert "close BTC-USD 0" not in section
        assert "close BTC-USD 5; pnl -5.00%" in section
        assert "close BTC-USD 2; pnl +20.00%" in section
        assert "x10" not in section and "still open" not in section
        assert section.count("close BTC-USD") == 5


def test_recent_results_empty_database_and_ablation(tmp_path):
    with _db(tmp_path) as connection:
        context = render_context(connection, INSTRUMENTS, NOW.isoformat())
        assert "probe book: closed trades 0; wins 0; average pnl +0.00%" in context
        assert "0 closed trades only, too few to mean much" in context
        record_decision(connection, created_at=NOW.isoformat(), batch_id="test", instrument="BTC-USD",
                        model="fake-model", rendered_context=context, p_up=0.6, status="ok")
        seen = []
        run_ablation(connection, ask=lambda reduced: (
            seen.append(reduced) or _reply({"instrument": "BTC-USD", "p_up": 0.6})
        ))
        assert any("## SECTION: recent_results" not in reduced and "## SECTION: prices" in reduced
                   for reduced in seen)


def test_recent_results_short_includes_costs_and_respects_time(tmp_path):
    with _db(tmp_path) as connection:
        execute_paper_trade(connection, predictor="probe", instrument="BTC-USD",
                            target_side="short", price=100, now_iso=(NOW - timedelta(hours=1)).isoformat(),
                            reason="short open", starting_cash=1000)
        execute_paper_trade(connection, predictor="probe", instrument="BTC-USD",
                            target_side="flat", price=80, now_iso=NOW.isoformat(), reason="short close")
        execute_paper_trade(connection, predictor="probe", instrument="BTC-USD",
                            target_side="long", price=100, now_iso=(NOW + timedelta(hours=1)).isoformat(),
                            reason="future open")
        section = render_context(connection, INSTRUMENTS, NOW.isoformat()).split("## SECTION: recent_results\n")[1]
        assert "short; entry 100; exit 80; reason short close; pnl +19.73%" in section
        assert "probe book: closed trades 1; wins 1; average pnl +19.73%" in section
        assert "future open" not in section


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


def test_gateway_402_falls_to_agy_and_records_provider(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.delenv("LLM_FALLBACKS")
    def gateway(_):
        raise HTTPError("https://example.invalid", 402, "credits", {}, None)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", gateway)
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: f"/{name}")
    seen = []
    def run(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps({"calls": [
            {"instrument": name, "p_up": 0.6} for name in INSTRUMENTS]}), "")
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.6
        rows = fetch_decisions(connection, 10)
        assert {row["model"] for row in rows} == {"fallback/agy"}
        assert {row["request_count"] for row in rows} == {2}
    args, kwargs = seen[0]
    assert args[:5] == ["/agy", "--model", "gemini-3.8-flash-high", "--sandbox", "-p"]
    assert len(args) == 6 and json.loads(args[-1])[1]["content"].startswith("## SECTION: prices")
    assert kwargs["timeout"] == CLI_TIMEOUT_SECONDS
    assert kwargs["cwd"] is not None and Path(kwargs["cwd"]) != Path.cwd()


@pytest.mark.parametrize("stdout, expected", [
    ('```json\n{"calls": [{"instrument": "BTC-USD", "p_up": 0.6}]}\n```', {"calls": [{"instrument": "BTC-USD", "p_up": 0.6}]}),
    ('Here you go:\n{"calls": [{"instrument": "BTC-USD", "p_up": 0.6}]}\nHope that helps.',
     {"calls": [{"instrument": "BTC-USD", "p_up": 0.6}]}),
    ('{"calls": [{"instrument": "BTC-USD", "p_up": 0.6}]}', {"calls": [{"instrument": "BTC-USD", "p_up": 0.6}]}),
])
def test_ask_cli_unwraps_fenced_and_surrounded_json(monkeypatch, stdout, expected):
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: f"/{name}")
    seen = []
    def run(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout, "")
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    response = ask_cli("context", "claude")
    assert json.loads(response["choices"][0]["message"]["content"]) == expected
    assert seen[0][:7] == ["/claude", "-p", "--model", "haiku", "--setting-sources", "project", "--strict-mcp-config"]
    assert seen[0][7:9] == ["--disable-slash-commands", "--no-session-persistence"]
    assert seen[0][-1] == json.dumps(_messages("context"), ensure_ascii=False)


def test_missing_agy_falls_to_codex(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_FALLBACKS", "agy,codex")
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _: (_ for _ in ()).throw(TimeoutError("gateway")))
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: None if name == "agy" else f"/{name}")
    seen = []
    def run(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"calls": [
            {"instrument": name, "p_up": 0.7} for name in INSTRUMENTS]}), "")
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.65
        assert {row["model"] for row in fetch_decisions(connection, 10)} == {"fallback/codex"}
        assert {row["request_count"] for row in fetch_decisions(connection, 10)} == {3}
    assert seen[0][:6] == ["/codex", "exec", "-m", "gpt-6-astra", "--sandbox", "read-only"]
    assert len(seen[0]) == 7


def test_all_providers_fail_one_error_row(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.delenv("LLM_FALLBACKS")
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _: (_ for _ in ()).throw(TimeoutError("gateway")))
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: f"/{name}")
    def run(args, **kwargs):
        if args[0] == "/agy":
            return subprocess.CompletedProcess(args, 0, "bad JSON", "")
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.5
        rows = fetch_decisions(connection, 10)
        assert len(rows) == 1
        assert rows[0]["status"] == "error" and rows[0]["p_up"] is None
        assert rows[0]["request_count"] == 4
        assert all(name in rows[0]["reason"] for name in ("gateway", "agy", "codex", "claude"))


def test_gateway_http_error_reports_status_code(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_FALLBACKS", "")
    def gateway(_):
        raise HTTPError("https://example.invalid", 429, "rate limited", {}, None)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", gateway)
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.5
        rows = fetch_decisions(connection, 10)
        assert rows[0]["reason"] == "gateway: HTTPError 429"
        assert "https://example.invalid" not in rows[0]["reason"]


def test_gateway_429_skips_gateway_for_sixty_minutes(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_MIN_MINUTES", "0")
    monkeypatch.setenv("LLM_FALLBACKS", "agy")
    gateway_calls = 0
    def gateway(_):
        nonlocal gateway_calls
        gateway_calls += 1
        raise HTTPError("https://example.invalid", 429, "rate limited", {}, None)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", gateway)
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: f"/{name}")
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps({"calls": [
            {"instrument": name, "p_up": 0.6} for name in INSTRUMENTS]}), "")
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection)
        assert gateway_calls == 1
        assert {row["request_count"] for row in fetch_decisions(connection, 2)} == {2}

        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=30))
        assert gateway_calls == 1
        assert {row["request_count"] for row in fetch_decisions(connection, 2)} == {1}


def test_gateway_retried_after_sixty_minutes(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_MIN_MINUTES", "0")
    monkeypatch.setenv("LLM_FALLBACKS", "agy")
    gateway_calls = 0
    def gateway(_):
        nonlocal gateway_calls
        gateway_calls += 1
        raise HTTPError("https://example.invalid", 429, "rate limited", {}, None)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", gateway)
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: f"/{name}")
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps({"calls": [
            {"instrument": name, "p_up": 0.6} for name in INSTRUMENTS]}), "")
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection)
        assert gateway_calls == 1
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=30))
        assert gateway_calls == 1
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=60))
        assert gateway_calls == 2


def test_non_429_gateway_error_does_not_skip_gateway(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_MIN_MINUTES", "0")
    monkeypatch.setenv("LLM_FALLBACKS", "agy")
    gateway_calls = 0
    def gateway(_):
        nonlocal gateway_calls
        gateway_calls += 1
        raise HTTPError("https://example.invalid", 500, "server error", {}, None)
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", gateway)
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: f"/{name}")
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps({"calls": [
            {"instrument": name, "p_up": 0.6} for name in INSTRUMENTS]}), "")
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection)
        assert gateway_calls == 1
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=1))
        assert gateway_calls == 2


def test_error_row_omits_prompt_and_cli_flags(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.delenv("LLM_FALLBACKS")
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", lambda _: (_ for _ in ()).throw(TimeoutError("gateway")))
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: f"/{name}")
    def run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    with _db(tmp_path) as connection:
        assert _predict(LlmPredictor(INSTRUMENTS), connection) == 0.5
        rows = fetch_decisions(connection, 10)
        reason = rows[0]["reason"]
        assert "## SECTION: prices" not in reason
        assert "-p" not in reason


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_subscription_fallback_waits_fifteen_minutes(monkeypatch, tmp_path, provider):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_MIN_MINUTES", "0")
    monkeypatch.setenv("LLM_FALLBACKS", provider)
    count = 0
    def gateway(_):
        nonlocal count
        count += 1
        raise TimeoutError("gateway")
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", gateway)
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda name: f"/{name}")
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps({"calls": [
            {"instrument": name, "p_up": 0.6} for name in INSTRUMENTS]}), "")
    monkeypatch.setattr("andy_trader.llm_agent.subprocess.run", run)
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection)
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=14))
        assert count == 1 and len(fetch_decisions(connection, 10)) == 2
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=15))
        assert count == 2


def test_fallback_attempts_respect_daily_cap(monkeypatch, tmp_path):
    _configured(monkeypatch)
    monkeypatch.setenv("LLM_FALLBACKS", "agy,codex,claude")
    monkeypatch.setenv("LLM_DAILY_CALLS", "2")
    monkeypatch.setenv("LLM_MIN_MINUTES", "0")
    count = 0
    def gateway(_):
        nonlocal count
        count += 1
        raise TimeoutError("gateway")
    monkeypatch.setattr("andy_trader.llm_agent.ask_model", gateway)
    monkeypatch.setattr("andy_trader.llm_agent.shutil.which", lambda _: None)
    with _db(tmp_path) as connection:
        _predict(LlmPredictor(INSTRUMENTS), connection)
        _predict(LlmPredictor(INSTRUMENTS), connection, at=NOW + timedelta(minutes=1))
        assert count == 1
        rows = fetch_decisions(connection, 10)
        assert {row["request_count"] for row in rows} == {0, 2}
        assert {row["status"] for row in rows} == {"error", "capped"}
