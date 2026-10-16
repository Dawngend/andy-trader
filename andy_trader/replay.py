"""Compare model calls on saved agent contexts without making trades."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import re
import sqlite3
import uuid

from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.llm_agent import _parse, ask_model
from andy_trader.llm_ledger import initialize_ledger, record_decision
from andy_trader.store import close_price_at, connect, default_database_path


def _reference_price(context: str, instrument: str) -> float | None:
    prices = context.split("## SECTION: prices", 1)[-1].split("## SECTION:", 1)[0]
    match = re.search(r"^\d+\. " + re.escape(instrument) + r": close ([0-9.eE+-]+);", prices, re.MULTILINE)
    return float(match.group(1)) if match else None


def _outcome(connection: sqlite3.Connection, original: sqlite3.Row, now: datetime) -> int | None:
    start = datetime.fromisoformat(original["created_at"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    end = start + timedelta(hours=1)
    if end > now:
        return None
    reference = _reference_price(original["rendered_context"], original["instrument"])
    if reference is None:
        return None
    price, note = close_price_at(connection, original["instrument"], end.isoformat(), now_iso=now.isoformat())
    if price is None or "latest price before" in note:
        return None
    return int(price > reference)


def replay(connection: sqlite3.Connection, *, model: str, limit: int = 48, ask=None) -> dict:
    """Replay the newest successful original batches and summarize paired calls."""
    if limit < 1:
        raise ValueError("limit must be positive")
    initialize_ledger(connection)
    batch_ids = [row[0] for row in connection.execute("""
        SELECT batch_id FROM llm_decisions
        WHERE status = 'ok' AND replay_of IS NULL
        GROUP BY batch_id ORDER BY MAX(id) DESC LIMIT ?
    """, (limit,))]
    ask = ask or ask_model
    pairs = []
    batches = []
    now = datetime.now(UTC)
    for batch_id in batch_ids:
        originals = connection.execute("""
            SELECT * FROM llm_decisions WHERE batch_id = ? AND status = 'ok' AND replay_of IS NULL
            ORDER BY list_position, id
        """, (batch_id,)).fetchall()
        context = originals[0]["rendered_context"]
        instruments = [row["instrument"] for row in originals]
        response = ask(context, model=model)
        calls = _parse(response, instruments)
        usage = response.get("usage") or {}
        raw = json.dumps(response, sort_keys=True)
        replay_batch = uuid.uuid4().hex
        for original in originals:
            instrument = original["instrument"]
            probability, reason = calls.get(instrument, (0.5, "No call for instrument"))
            record_decision(
                connection, created_at=now.isoformat(), batch_id=replay_batch,
                instrument=instrument, model=model, template_rev=original["template_rev"],
                config_rev=original["config_rev"], list_arm=original["list_arm"],
                list_seed=original["list_seed"], list_position=original["list_position"],
                rendered_context=context, raw_response=raw, p_up=probability,
                reason=reason, status="replay", replay_of=original["id"],
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"), cost=usage.get("cost"),
            )
            pairs.append((original, probability, _outcome(connection, original, now)))
        batches.append((originals[0], usage))
    connection.commit()

    def totals(items):
        tokens = sum((item.get("prompt_tokens") or 0) + (item.get("completion_tokens") or 0) for item in items)
        cost = sum(item.get("cost") or 0 for item in items)
        count = len(items)
        return {"tokens_per_100_batches": 100 * tokens / count if count else None,
                "cost_per_100_batches": 100 * cost / count if count else None}

    replay_usage = [usage for _, usage in batches]
    repeated = totals(replay_usage)
    paired = [(row, p) for row, p, _ in pairs]
    settled = [(row, p, outcome) for row, p, outcome in pairs if outcome is not None]
    def brier(which, original_model=None):
        selected = [(row, p, outcome) for row, p, outcome in settled
                    if original_model is None or row["model"] == original_model]
        return sum(((float(row["p_up"]) if which == "original" else p) - outcome) ** 2
                   for row, p, outcome in selected) / len(selected) if selected else None

    models = {original_model: {**totals([dict(row) for row, _ in batches
                                        if row["model"] == original_model]),
                               "brier": brier("original", original_model)}
              for original_model in {row["model"] for row, _ in batches}}
    models[f"replay:{model}"] = {**repeated, "brier": brier("replay")}
    originals = [scores for name, scores in models.items() if not name.startswith("replay:")]
    original = originals[0] if len(originals) == 1 else None
    ratio = (repeated["cost_per_100_batches"] / original["cost_per_100_batches"]
             if original and original["cost_per_100_batches"] else None)

    return {
        "batches": len(batches), "calls": len(pairs), "settled_calls": len(settled),
        "agreement": sum((float(row["p_up"]) > 0.5) == (p > 0.5) for row, p in paired) / len(paired) if paired else None,
        "mean_absolute_difference": sum(abs(float(row["p_up"]) - p) for row, p in paired) / len(paired) if paired else None,
        "models": models,
        "cost_ratio": ratio,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=48)
    args = parser.parse_args()
    load_env_file(REPO_ROOT / ".env")
    with connect(default_database_path()) as connection:
        report = replay(connection, model=args.model, limit=args.limit)
    print(f"Batches: {report['batches']}  Calls: {report['calls']}  Closed: {report['settled_calls']}")
    print("Model | Brier | Tokens/100 batches | Cost/100 batches")
    for name, scores in report["models"].items():
        brier = "n/a" if scores["brier"] is None else f"{scores['brier']:.4f}"
        tokens = "n/a" if scores["tokens_per_100_batches"] is None else f"{scores['tokens_per_100_batches']:.0f}"
        cost = "n/a" if scores["cost_per_100_batches"] is None else f"{scores['cost_per_100_batches']:.4f}"
        print(f"{name} | {brier} | {tokens} | {cost}")
    agreement = "n/a" if report["agreement"] is None else f"{report['agreement']:.1%}"
    difference = "n/a" if report["mean_absolute_difference"] is None else f"{report['mean_absolute_difference']:.4f}"
    ratio = "n/a" if report["cost_ratio"] is None else f"{report['cost_ratio']:.3f}"
    print(f"Direction agreement: {agreement}  Mean |p_up difference|: {difference}  Cost ratio (replay/original): {ratio}")
    print("Replay evidence is about decisions on frozen contexts, not live profit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
