"""Load, evaluate, and learn loss rules from settled paper positions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from andy_trader.env import REPO_ROOT

DEFAULT_RULES_PATH = REPO_ROOT / "loss_rules.json"
ALLOWED_RULE_TAGS = ("slice", "entry_band", "asset", "time_to_end", "open_hour", "same_end_count")


def learn_rules(rows: list[dict]) -> list[dict]:
    """Learn loss rules from settled positions not in paused slices."""
    from andy_trader.bond_book import PAUSED_SLICES, strategy_slice
    from andy_trader.loss_review import group_by_end, row_tags, summarize, utc_time

    active_rows = [
        row for row in rows
        if strategy_slice(row.get("title") or "", row.get("side") or "") not in PAUSED_SLICES
    ]
    if not active_rows:
        return []

    overall = summarize(active_rows)
    overall_loss_rate = overall["loss_rate"]
    threshold_rate = 1.5 * overall_loss_rate

    active_ends = group_by_end(active_rows)
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in active_rows:
        end_count = 1
        end_val = row.get("ends_at")
        if end_val:
            try:
                key = utc_time(end_val).replace(microsecond=0)
                end_count = len(active_ends.get(key, []))
            except Exception:
                end_count = 1
        tags = row_tags(row, end_count)
        for tag, value in tags.items():
            if tag not in ALLOWED_RULE_TAGS:
                continue
            if tag == "same_end_count" and value != "3 or more":
                continue
            if not value or value == "unknown":
                continue
            groups.setdefault((tag, value), []).append(row)

    candidates = []
    for (tag, value), group in groups.items():
        stats = summarize(group)
        if stats["settled"] >= 30 and stats["loss_rate"] >= threshold_rate and stats["pnl"] < 0:
            loss_pct = stats["loss_rate"] * 100
            overall_pct = overall_loss_rate * 100
            pnl_dollars = f"-${abs(stats['pnl']):,.2f}" if stats["pnl"] < 0 else f"${stats['pnl']:,.2f}"
            reason = (
                f"Loss rate {loss_pct:.1f}% exceeds 1.5x overall rate "
                f"({overall_pct:.1f}%) with {pnl_dollars} total pnl "
                f"over {stats['settled']} settled trades."
            )
            candidates.append({
                "tag": tag,
                "value": value,
                "settled": stats["settled"],
                "loss_rate": round(stats["loss_rate"], 4),
                "pnl": round(stats["pnl"], 2),
                "reason": reason,
            })

    candidates.sort(key=lambda rule: (rule["pnl"], rule["tag"], rule["value"]))
    return candidates[:5]


def load_rules(path: Path | str | None = None) -> list[dict]:
    """Load loss rules from a JSON file. Return empty list on any error or if older than 14 days."""
    try:
        rules_path = Path(path) if path is not None else DEFAULT_RULES_PATH
        if not rules_path.is_file():
            return []

        now = datetime.now(UTC)
        mtime = datetime.fromtimestamp(rules_path.stat().st_mtime, tz=UTC)
        if now - mtime > timedelta(days=14):
            return []

        content = rules_path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            return []

        gen_at = data.get("generated_at")
        if gen_at:
            try:
                gen_time = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
                if gen_time.tzinfo is None:
                    gen_time = gen_time.replace(tzinfo=UTC)
                if now - gen_time.astimezone(UTC) > timedelta(days=14):
                    return []
            except Exception:
                pass

        rules = data.get("rules")
        if not isinstance(rules, list):
            return []
        valid_rules = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            tag = r.get("tag")
            if tag not in ALLOWED_RULE_TAGS:
                continue
            if tag == "same_end_count" and r.get("value") != "3 or more":
                continue
            valid_rules.append(r)
        return valid_rules
    except Exception:
        return []


def blocking_rule(rules: list[dict], tags: dict[str, str]) -> dict | None:
    """Return the first rule matching tags on tag and value, or None."""
    if not rules or not tags:
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tag = rule.get("tag")
        if tag and tag in tags and tags[tag] == rule.get("value"):
            return rule
    return None
