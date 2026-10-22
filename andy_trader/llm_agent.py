"""Optional model requests for a batch of hourly price forecasts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
import random
import queue
import shutil
import subprocess
import tempfile
import threading
import sqlite3
import statistics
from typing import Sequence
import urllib.request
import uuid

from andy_trader.baselines import MAX_CONFIDENCE, MIN_CONFIDENCE, PredictionContext, default_baselines
from andy_trader.llm_ledger import initialize_ledger, record_decision
from andy_trader.predict import load_closes
from andy_trader.probe_book import PROBE_BOOK
from andy_trader.signals import FEAR_GREED, FUNDING_RATE, LONG_RATIO, initialize_signals

TEMPLATE_REV = "v1"


def _ordered_prices(connection: sqlite3.Connection, instruments: Sequence[str]) -> list[tuple[str, list[float], float | None]]:
    result = []
    for instrument in instruments:
        closes = [value for _, value in load_closes(connection, instrument, limit=25)]
        move = (closes[-1] / closes[-25] - 1) * 100 if len(closes) >= 25 and closes[-25] else None
        result.append((instrument, closes, move))
    return sorted(result, key=lambda item: (
        item[2] is None,
        -abs(item[2]) if item[2] is not None else 0,
        item[0],
    ))


def _signal(connection: sqlite3.Connection, name: str, instrument: str | None, now_iso: str) -> str:
    row = connection.execute("""
        SELECT value, observed_time FROM crypto_signals
        WHERE signal = ? AND instrument IS ? AND observed_time <= ?
          AND degraded = 0 AND value IS NOT NULL
        ORDER BY observed_time DESC LIMIT 1
    """, (name, instrument, now_iso)).fetchone()
    if row is None:
        return "unavailable"
    age = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(row["observed_time"])).total_seconds() / 3600
    return f"{float(row['value']):.6g}; age {age:.2f}h"


def _recent_results(connection: sqlite3.Connection, ordered: list[tuple[str, list[float], float | None]],
                    now_iso: str) -> list[str]:
    lines = ["## SECTION: recent_results"]
    closed: dict[str, list[tuple[str, float, float, str, float]]] = {}
    openings = {}
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'paper_trades'").fetchone():
        rows = connection.execute("""
            SELECT instrument, side, qty, price, fee_cost, slippage_cost, executed_at, reason
            FROM paper_trades
            WHERE predictor = ? AND executed_at <= ?
            ORDER BY executed_at, id
        """, (PROBE_BOOK, now_iso))
        for row in rows:
            instrument = row["instrument"]
            if row["side"] != "flat":
                openings[instrument] = row
                continue
            entry = openings.pop(instrument, None)
            if entry is None:
                continue
            direction = 1 if entry["side"] == "long" else -1
            notional = entry["qty"] * entry["price"]
            pnl = (direction * entry["qty"] * (row["price"] - entry["price"])
                   - entry["fee_cost"] - entry["slippage_cost"]
                   - row["fee_cost"] - row["slippage_cost"])
            pnl_pct = pnl / notional * 100 if notional else 0.0
            closed.setdefault(instrument, []).append(
                (entry["side"], entry["price"], row["price"], row["reason"], pnl_pct))

    all_results = [trade[-1] for trades in closed.values() for trade in trades]
    wins = sum(pnl > 0 for pnl in all_results)
    average = sum(all_results) / len(all_results) if all_results else 0.0
    lines.append(f"probe book: closed trades {len(all_results)}; wins {wins}; average pnl {average:+.2f}%")
    for position, (instrument, _, _) in enumerate(ordered, 1):
        trades = closed.get(instrument, [])
        lines.append(f"{position}. {instrument}:")
        if len(trades) < 5:
            noun = "trade" if len(trades) == 1 else "trades"
            lines.append(f"  {len(trades)} closed {noun} only, too few to mean much")
        for side, entry, exit_price, reason, pnl_pct in reversed(trades[-5:]):
            lines.append(f"  {side}; entry {entry:.8g}; exit {exit_price:.8g}; reason {reason}; pnl {pnl_pct:+.2f}%")
    return lines


def render_context(connection: sqlite3.Connection, instruments: Sequence[str], now_iso: str,
                   ordered: list[tuple[str, list[float], float | None]] | None = None) -> str:
    """Render only observations stored at or before the requested time."""
    initialize_signals(connection)
    if ordered is None:
        ordered = _ordered_prices(connection, instruments)
    lines = ["## SECTION: prices"]
    for position, (instrument, closes, move) in enumerate(ordered, 1):
        if not closes:
            lines.append(f"{position}. {instrument}: unavailable")
            continue
        def change(hours: int) -> str:
            if len(closes) <= hours or closes[-hours - 1] <= 0:
                return "unavailable"
            return f"{(closes[-1] / closes[-hours - 1] - 1) * 100:.4f}%"
        returns = [math.log(b / a) for a, b in zip(closes[-25:-1], closes[-24:]) if a > 0 and b > 0]
        volatility = f"{statistics.pstdev(returns):.6f}" if len(returns) == 24 else "unavailable"
        lines.append(f"{position}. {instrument}: close {closes[-1]:.8g}; 1h {change(1)}; 4h {change(4)}; 24h {change(24)}; 24h volatility {volatility}")
    lines.append("## SECTION: minute_moves")
    moment = datetime.fromisoformat(now_iso)
    for position, (instrument, _, _) in enumerate(ordered, 1):
        bars = {datetime.fromisoformat(at): price for at, price in
                load_closes(connection, instrument, interval="1m", limit=60)
                if datetime.fromisoformat(at) <= moment - timedelta(minutes=1)}
        latest = max(bars, default=None)
        if (latest is None or (moment - latest).total_seconds() > 180
                or any(latest - timedelta(minutes=m) not in bars for m in (5, 15, 30))):
            text = "no fresh 1m bars"
        else:
            text = "; ".join(f"{m}m {(bars[latest] / bars[latest - timedelta(minutes=m)] - 1) * 100:.4f}%"
                             for m in (5, 15, 30))
        lines.append(f"{position}. {instrument}: {text}")
    for name in (FUNDING_RATE, LONG_RATIO, FEAR_GREED):
        lines.append(f"## SECTION: {name}")
        for position, (instrument, _, _) in enumerate(ordered, 1):
            lines.append(f"{position}. {instrument}: {_signal(connection, name, None if name == FEAR_GREED else instrument, now_iso)}")
    lines.extend(_recent_results(connection, ordered, now_iso))
    return "\n".join(lines)


def _messages(context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Return JSON only: {\"calls\": [{\"instrument\": \"...\", \"p_up\": 0.0, \"reason\": \"one sentence\"}]}. p_up is the probability that the next hourly close is above the current close. Code decides position size, leverage, and exits. Past results are a small sample and are not proof."},
        {"role": "user", "content": context},
    ]


def ask_model(context: str, *, model: str | None = None, opener=urllib.request.urlopen, timeout: float = 120) -> dict:
    body = {"model": model or os.environ["LLM_MODEL"], "messages": _messages(context)}
    request = urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"], "Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=timeout) as response:
        return json.loads(response.read())


def ask_with_timeout(context: str, timeout: float) -> dict:
    # Only the request runs in the daemon; late replies cannot write to SQLite.
    result = queue.Queue()
    def request():
        try:
            result.put((ask_model(context, timeout=timeout), None))
        except Exception as exc:
            result.put((None, exc))
    threading.Thread(target=request, daemon=True).start()
    try:
        response, error = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"Model request exceeded {timeout}s") from None
    if error is not None:
        raise error
    return response


CLI_TIMEOUT_SECONDS = 40


def _unwrap_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].strip() in ("```", "```json"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
    return text


def ask_cli(context: str, provider: str) -> dict:
    commands = {
        # agy's -p takes the next word as the prompt, so the prompt must follow -p directly.
        # Do not add --mode plan: with a real batch prompt it silences agy entirely in
        # headless mode (a tool needs the command permission that headless mode cannot prompt for).
        "agy": ["--model", "gemini-3.8-flash-high", "--sandbox", "-p"],
        "codex": ["exec", "-m", "gpt-6-astra", "--sandbox", "read-only"],
        "claude": ["-p", "--model", "haiku"],
    }
    if provider not in commands:
        raise ValueError(f"Unknown fallback provider: {provider}")
    executable = shutil.which(provider)
    if not executable:
        raise FileNotFoundError(f"{provider} CLI not found")
    prompt = json.dumps(_messages(context), ensure_ascii=False)
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run([executable, *commands[provider], prompt], capture_output=True,
                                text=True, timeout=CLI_TIMEOUT_SECONDS, check=True, cwd=cwd)
    return {"choices": [{"message": {"content": _unwrap_json(result.stdout)}}]}


def _parse(response: dict, instruments: Sequence[str]) -> dict[str, tuple[float, str]]:
    content = response["choices"][0]["message"]["content"]
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("calls"), list):
        raise ValueError("Invalid calls object")
    calls = {}
    for call in data["calls"]:
        if not isinstance(call, dict):
            continue
        instrument, probability = call.get("instrument"), call.get("p_up")
        if not isinstance(instrument, str) or instrument not in instruments or instrument in calls:
            continue
        if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            continue
        reason = call.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        reason = " ".join(reason.split())[:300]
        calls[instrument] = (max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, float(probability))), reason)
    return calls


class LlmPredictor:
    name = "agent"
    minimum_history = 1

    def __init__(self, instruments: Sequence[str], *, request_timeout: float | None = None):
        self.request_timeout = request_timeout
        self.batch_id = None
        self.instruments = tuple(dict.fromkeys(instruments))
        self._batch: dict[str, float] | None = None

    @property
    def prediction_name(self) -> str:
        return "llm:agent"

    def __call__(self, closes: Sequence[float], *, context: PredictionContext) -> float:
        del closes
        if not all(os.environ.get(key) for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")):
            return 0.5
        if self._batch is None:
            self._batch = self._load_batch(context.connection, context.at_or_before)
        return self._batch.get(context.instrument, 0.5)

    def _load_batch(self, connection: sqlite3.Connection, now_iso: str) -> dict[str, float]:
        initialize_ledger(connection)
        minutes = float(os.environ.get("LLM_MIN_MINUTES", "60"))
        daily_calls = int(os.environ.get("LLM_DAILY_CALLS", "100"))
        if minutes < 0 or daily_calls < 0:
            raise ValueError("LLM limits must be non-negative")
        model = os.environ["LLM_MODEL"]
        list_mode = os.environ.get("LLM_LIST_ORDER", "alternate").lower()
        if list_mode not in ("ranked", "shuffled", "alternate"):
            raise ValueError("LLM_LIST_ORDER must be ranked, shuffled, or alternate")
        hour = datetime.fromisoformat(now_iso).astimezone(UTC).hour
        list_arm = ("ranked" if hour % 2 == 0 else "shuffled") if list_mode == "alternate" else list_mode
        config_rev = hashlib.sha1(f"{model}|{minutes:g}|{TEMPLATE_REV}|{list_mode}|{list_arm}".encode()).hexdigest()[:8]
        since = (datetime.fromisoformat(now_iso) - timedelta(minutes=minutes)).isoformat()
        subscription_since = (datetime.fromisoformat(now_iso) - timedelta(minutes=15)).isoformat()
        subscription_recent = connection.execute("""
            SELECT 1 FROM llm_decisions
            WHERE created_at > ? AND created_at <= ? AND model IN ('fallback/codex', 'fallback/claude')
              AND status NOT IN ('replay', 'ablation', 'capped') LIMIT 1
        """, (subscription_since, now_iso)).fetchone()
        recent = connection.execute("""
            SELECT batch_id FROM llm_decisions
            WHERE created_at >= ? AND created_at <= ? AND config_rev = ? AND status NOT IN ('replay', 'ablation', 'capped')
            ORDER BY created_at DESC, id DESC LIMIT 1
        """, (since, now_iso, config_rev)).fetchone()
        if recent or subscription_recent:
            if subscription_recent and not recent:
                recent = connection.execute("""
                    SELECT batch_id FROM llm_decisions
                    WHERE created_at > ? AND created_at <= ? AND model IN ('fallback/codex', 'fallback/claude')
                      AND status NOT IN ('replay', 'ablation', 'capped')
                    ORDER BY created_at DESC, id DESC LIMIT 1
                """, (subscription_since, now_iso)).fetchone()
            rows = connection.execute(
                "SELECT instrument, p_up FROM llm_decisions WHERE batch_id = ? AND status != 'ablation'",
                (recent["batch_id"],),
            ).fetchall()
            if {row["instrument"] for row in rows} == set(self.instruments):
                return {row["instrument"]: row["p_up"] for row in rows}
            if subscription_recent:
                return {row["instrument"]: row["p_up"] for row in rows if row["p_up"] is not None}

        batch_id = uuid.uuid4().hex
        self.batch_id = batch_id
        list_seed = batch_id
        ordered = _ordered_prices(connection, self.instruments)
        if list_arm == "shuffled":
            random.Random(list_seed).shuffle(ordered)
        rendered = render_context(connection, self.instruments, now_iso, ordered)
        day = datetime.fromisoformat(now_iso).astimezone(UTC).date().isoformat()
        used = connection.execute("""
            SELECT COALESCE(SUM(requests), 0) FROM (
                SELECT MAX(request_count) AS requests FROM llm_decisions
                WHERE substr(created_at, 1, 10) = ? AND status NOT IN ('capped', 'replay', 'ablation')
                GROUP BY batch_id
            )
        """, (day,)).fetchone()[0]
        response = None
        raw = None
        error = None
        status = "ok"
        provider = "gateway"
        request_count = 0
        fallbacks = [name.strip().lower() for name in os.environ.get("LLM_FALLBACKS", "agy,codex,claude").split(",") if name.strip()]
        if used >= daily_calls:
            status = "capped"
        else:
            errors = []
            for candidate in ("gateway", *fallbacks):
                if used + request_count >= daily_calls:
                    errors.append("daily call cap reached")
                    break
                request_count += 1
                try:
                    candidate_response = (ask_model(rendered) if self.request_timeout is None else ask_with_timeout(rendered, self.request_timeout)) if candidate == "gateway" else ask_cli(rendered, candidate)
                    candidate_calls = _parse(candidate_response, self.instruments)
                    response = candidate_response
                    raw = json.dumps(response, sort_keys=True)
                    calls = candidate_calls
                    provider = candidate
                    break
                except Exception as exc:
                    errors.append(f"{candidate}: {type(exc).__name__}")
            if response is None:
                status = "error"
                error = "; ".join(errors)
                calls = {}
        if status == "capped":
            calls = {}
        if status == "error":
            record_decision(connection, created_at=now_iso, batch_id=batch_id, instrument=None,
                            model=model, template_rev=TEMPLATE_REV, config_rev=config_rev,
                            list_arm=list_arm, list_seed=list_seed, rendered_context=rendered,
                            p_up=None, reason=error, status="error", request_count=request_count)
            return {}
        retry_response = None
        retry_raw = None
        missing = [item for item in ordered if item[0] not in calls]
        missing_names = [item[0] for item in missing]
        if status == "ok" and missing and used + request_count < daily_calls:
            try:
                retry_context = render_context(connection, missing_names, now_iso, missing)
                retry_context += "\nReturn the same JSON for only the instruments listed above."
                request_count += 1
                retry_response = ((ask_model(retry_context) if self.request_timeout is None
                                   else ask_with_timeout(retry_context, self.request_timeout))
                                  if provider == "gateway" else ask_cli(retry_context, provider))
                retry_raw = json.dumps(retry_response, sort_keys=True)
                calls.update(_parse(retry_response, missing_names))
            except Exception:
                pass  # Keep the first response and missing defaults.
        probabilities = {}
        for position, (instrument, _, _) in enumerate(ordered, 1):
            probability, reason = calls.get(instrument, (0.5, "No call for instrument"))
            row_status = status if status != "ok" else ("ok" if instrument in calls else "missing")
            probabilities[instrument] = probability
            retried = retry_response is not None and instrument in missing_names
            row_response = retry_response if retried else response
            row_usage = row_response.get("usage", {}) if isinstance(row_response, dict) else {}
            record_decision(connection, created_at=now_iso, batch_id=batch_id, instrument=instrument,
                            model=model if provider == "gateway" else f"fallback/{provider}", template_rev=TEMPLATE_REV, config_rev=config_rev,
                            list_arm=list_arm, list_seed=list_seed, list_position=position,
                            rendered_context=retry_context if retried else rendered,
                            raw_response=retry_raw if retried else raw,
                            p_up=probability, reason=error or reason, status=row_status,
                            prompt_tokens=row_usage.get("prompt_tokens"), completion_tokens=row_usage.get("completion_tokens"),
                            cost=row_usage.get("cost"), request_count=request_count)
        return probabilities


def build_predictors(instruments: Sequence[str]):
    baselines = default_baselines()
    if all(os.environ.get(key) for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")):
        return (*baselines, LlmPredictor(instruments))
    return baselines
