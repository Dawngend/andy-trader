"""One optional model request for a batch of hourly price forecasts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
import sqlite3
import statistics
from typing import Sequence
import urllib.request
import uuid

from andy_trader.baselines import MAX_CONFIDENCE, MIN_CONFIDENCE, PredictionContext, default_baselines
from andy_trader.llm_ledger import initialize_ledger, record_decision
from andy_trader.predict import load_closes
from andy_trader.signals import FEAR_GREED, FUNDING_RATE, LONG_RATIO, initialize_signals

TEMPLATE_REV = "v1"


def _ordered_prices(connection: sqlite3.Connection, instruments: Sequence[str]) -> list[tuple[str, list[float], float | None]]:
    result = []
    for instrument in instruments:
        closes = [value for _, value in load_closes(connection, instrument, limit=25)]
        move = (closes[-1] / closes[-25] - 1) * 100 if len(closes) >= 25 and closes[-25] else None
        result.append((instrument, closes, move))
    return sorted(result, key=lambda item: (-abs(item[2]) if item[2] is not None else float("-inf"), item[0]))


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


def render_context(connection: sqlite3.Connection, instruments: Sequence[str], now_iso: str) -> str:
    """Render only observations stored at or before the requested time."""
    initialize_signals(connection)
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
    for name in (FUNDING_RATE, LONG_RATIO, FEAR_GREED):
        lines.append(f"## SECTION: {name}")
        for position, (instrument, _, _) in enumerate(ordered, 1):
            lines.append(f"{position}. {instrument}: {_signal(connection, name, None if name == FEAR_GREED else instrument, now_iso)}")
    return "\n".join(lines)


def ask_model(context: str, *, opener=urllib.request.urlopen) -> dict:
    body = {"model": os.environ["LLM_MODEL"], "messages": [
        {"role": "system", "content": "Return JSON only: {\"calls\": [{\"instrument\": \"...\", \"p_up\": 0.0, \"reason\": \"one sentence\"}]}. p_up is the probability that the next hourly close is above the current close. Code decides position size, leverage, and exits."},
        {"role": "user", "content": context},
    ]}
    request = urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"], "Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=120) as response:
        return json.loads(response.read())


def _parse(response: dict, instruments: Sequence[str]) -> dict[str, tuple[float, str]]:
    content = response["choices"][0]["message"]["content"]
    data = json.loads(content)
    if not isinstance(data, dict) or set(data) != {"calls"} or not isinstance(data["calls"], list):
        raise ValueError("Invalid calls object")
    calls = {}
    for call in data["calls"]:
        if not isinstance(call, dict) or set(call) != {"instrument", "p_up", "reason"}:
            raise ValueError("Invalid call")
        instrument, probability, reason = call["instrument"], call["p_up"], call["reason"]
        if not isinstance(instrument, str) or instrument not in instruments or instrument in calls:
            raise ValueError("Unexpected or duplicate instrument")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Invalid probability")
        if not isinstance(reason, str) or not reason.strip() or "\n" in reason or reason.count(".") > 1:
            raise ValueError("Invalid reason")
        calls[instrument] = (max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, float(probability))), reason)
    return calls


class LlmPredictor:
    name = "agent"
    minimum_history = 1

    def __init__(self, instruments: Sequence[str]):
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
        config_rev = hashlib.sha1(f"{model}|{minutes:g}|{TEMPLATE_REV}".encode()).hexdigest()[:8]
        since = (datetime.fromisoformat(now_iso) - timedelta(minutes=minutes)).isoformat()
        recent = connection.execute("""
            SELECT batch_id FROM llm_decisions
            WHERE created_at >= ? AND created_at <= ? AND config_rev = ? AND status NOT IN ('replay', 'ablation', 'capped')
            ORDER BY created_at DESC, id DESC LIMIT 1
        """, (since, now_iso, config_rev)).fetchone()
        if recent:
            rows = connection.execute("SELECT instrument, p_up FROM llm_decisions WHERE batch_id = ?", (recent["batch_id"],)).fetchall()
            if {row["instrument"] for row in rows} == set(self.instruments):
                return {row["instrument"]: row["p_up"] for row in rows}

        ordered = _ordered_prices(connection, self.instruments)
        rendered = render_context(connection, self.instruments, now_iso)
        day = datetime.fromisoformat(now_iso).astimezone(UTC).date().isoformat()
        used = connection.execute("""
            SELECT COUNT(DISTINCT batch_id) FROM llm_decisions
            WHERE substr(created_at, 1, 10) = ? AND status NOT IN ('capped', 'replay', 'ablation')
        """, (day,)).fetchone()[0]
        response = None
        raw = None
        error = None
        status = "ok"
        if used >= daily_calls:
            status = "capped"
        else:
            try:
                response = ask_model(rendered)
                raw = json.dumps(response, sort_keys=True)
                calls = _parse(response, self.instruments)
            except Exception as exc:
                status = "error"
                error = f"{type(exc).__name__}: {exc}"
                calls = {}
        if status == "capped":
            calls = {}
        batch_id = uuid.uuid4().hex
        probabilities = {}
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        for position, (instrument, _, _) in enumerate(ordered, 1):
            probability, reason = calls.get(instrument, (0.5, "No call for instrument"))
            row_status = status if status != "ok" else ("ok" if instrument in calls else "missing")
            probabilities[instrument] = probability
            record_decision(connection, created_at=now_iso, batch_id=batch_id, instrument=instrument,
                            model=model, template_rev=TEMPLATE_REV, config_rev=config_rev,
                            list_position=position, rendered_context=rendered, raw_response=raw,
                            p_up=probability, reason=error or reason, status=row_status,
                            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                            cost=usage.get("cost"))
        return probabilities


def build_predictors(instruments: Sequence[str]):
    baselines = default_baselines()
    if all(os.environ.get(key) for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")):
        return (*baselines, LlmPredictor(instruments))
    return baselines
