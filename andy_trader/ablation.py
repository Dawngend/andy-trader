"""Measure how removing each stored context section changes model calls."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime
import json
import re
import sqlite3
import uuid

from andy_trader.llm_agent import _parse, ask_model
from andy_trader.llm_ledger import initialize_ledger, record_decision
from andy_trader.store import connect, default_database_path


SECTION = re.compile(r"^## SECTION: ([^\n]+)(?:\n|$)", re.MULTILINE)


def run_ablation(connection: sqlite3.Connection, *, batches=12, max_calls=40, ask=None) -> dict:
    """Replay recent successful batches once for each omitted section."""
    if batches < 0 or max_calls < 0:
        raise ValueError("batches and max_calls must be non-negative")
    initialize_ledger(connection)
    batch_ids = [row[0] for row in connection.execute("""
        SELECT batch_id FROM llm_decisions WHERE status = 'ok'
        GROUP BY batch_id ORDER BY MAX(id) DESC LIMIT ?
    """, (batches,))]
    changes = defaultdict(list)
    flips = defaultdict(list)
    calls_spent = 0
    capped = False
    for batch_id in batch_ids:
        originals = connection.execute("""
            SELECT * FROM llm_decisions WHERE batch_id = ? AND status = 'ok'
            ORDER BY list_position, id
        """, (batch_id,)).fetchall()
        context = originals[0]["rendered_context"]
        matches = list(SECTION.finditer(context or ""))
        for index, match in enumerate(matches):
            if calls_spent >= max_calls:
                capped = True
                break
            name = match.group(1).strip()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(context)
            reduced = (context[:match.start()] + context[end:]).strip("\n")
            response = ask(reduced) if ask is not None else ask_model(reduced, model=originals[0]["model"])
            calls_spent += 1
            parsed = _parse(response, [row["instrument"] for row in originals])
            raw = json.dumps(response, sort_keys=True)
            usage = response.get("usage", {})
            replay_batch = uuid.uuid4().hex
            for original in originals:
                probability, reason = parsed.get(original["instrument"], (0.5, "No call for instrument"))
                record_decision(
                    connection, created_at=datetime.now(UTC).isoformat(), batch_id=replay_batch,
                    instrument=original["instrument"], model=original["model"],
                    template_rev=original["template_rev"], config_rev=original["config_rev"],
                    list_arm=original["list_arm"], list_seed=original["list_seed"],
                    list_position=original["list_position"], rendered_context=reduced,
                    raw_response=raw, p_up=probability, reason=reason, status="ablation",
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"), cost=usage.get("cost"),
                    replay_of=original["id"],
                )
                before = original["p_up"]
                changes[name].append(abs(probability - before))
                flips[name].append((probability >= 0.5) != (before >= 0.5))
        if capped:
            break
    sections = {}
    for name, values in changes.items():
        mean_change = sum(values) / len(values)
        flip_share = sum(flips[name]) / len(values)
        sections[name] = {
            "mean_abs_change": mean_change,
            "flip_share": flip_share,
            "no_measurable_effect": mean_change < 0.02 and flip_share == 0,
            "samples": len(values),
        }
    return {"sections": sections, "calls_spent": calls_spent, "capped": capped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ablate stored LLM context sections")
    parser.add_argument("--batches", type=int, default=12)
    parser.add_argument("--max-calls", type=int, default=40)
    args = parser.parse_args()
    with connect(default_database_path()) as connection:
        report = run_ablation(connection, batches=args.batches, max_calls=args.max_calls)
    for name, result in report["sections"].items():
        effect = "no measurable effect" if result["no_measurable_effect"] else "measurable effect"
        print(f"{name}: mean |Δp_up| {result['mean_abs_change']:.3f}, "
              f"flip share {result['flip_share']:.1%} ({result['samples']} answers); {effect}")
    print(f"Model calls spent: {report['calls_spent']}")
    if report["capped"]:
        print("Stopped at max_calls cap.")


if __name__ == "__main__":
    main()
