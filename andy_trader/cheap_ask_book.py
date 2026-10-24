"""Cheap Ask Paper Book: a forward test of the fast continuation cheap-ask lead.

The fast:continuation predictor calls BTC 5-minute up/down rounds about 3 minutes
in. Across the full historical book it loses overall, but when the ask for its chosen
side was under 0.70, matched historical bets showed a positive win rate and net profit.
This module paper-trades that filter on forward rounds without placing real orders.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import random
import sqlite3
from typing import Sequence

MAX_ASK: float = 0.70
SHARES: int = 10
FEE_PER_CONTRACT: float = 0.02
LOOKBACK_MINUTES: int = 30
MAX_BOOK_DELAY_SECONDS: int = 20
BOOTSTRAP_RESAMPLES: int = 2000
BOOTSTRAP_SEED: int = 42
VERDICT_THRESHOLD: int = 100


def initialize_cheap_ask_book(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cheap_ask_decisions (
            prediction_id INTEGER PRIMARY KEY,
            round_id TEXT,
            decided_at TEXT,
            side TEXT,
            ask REAL,
            depth REAL,
            decision TEXT,
            win INTEGER,
            settled_at TEXT
        )
        """
    )
    connection.commit()


def _parse_iso(timestamp_str: str) -> datetime:
    dt = datetime.fromisoformat(timestamp_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def round_id_from_round_start(round_start_val: str | int | float | datetime) -> str:
    if isinstance(round_start_val, (int, float)):
        epoch_seconds = int(round_start_val)
    elif isinstance(round_start_val, datetime):
        dt = round_start_val if round_start_val.tzinfo is not None else round_start_val.replace(tzinfo=UTC)
        epoch_seconds = int(dt.timestamp())
    else:
        val_str = str(round_start_val).strip()
        try:
            epoch_seconds = int(float(val_str))
        except ValueError:
            dt = _parse_iso(val_str)
            epoch_seconds = int(dt.timestamp())
    return f"btc-updown-5m-{epoch_seconds}"


def classify_decision(ask: float | None, depth: float | None) -> str:
    if ask is not None and 0.0 < ask < MAX_ASK:
        if depth is not None and depth >= SHARES:
            return "bet"
        return "thin_book"
    return "ask_too_high"


def bet_profit(
    win: int,
    ask: float,
    *,
    shares: float = SHARES,
    fee_per_contract: float = FEE_PER_CONTRACT,
) -> float:
    return shares * (win - ask) - shares * fee_per_contract


def contract_profit(
    win: int,
    ask: float,
    *,
    fee_per_contract: float = FEE_PER_CONTRACT,
) -> float:
    return (win - ask) - fee_per_contract


def compute_bootstrap_interval(
    profits: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    if not profits:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(profits)
    boot_means = [sum(rng.choices(profits, k=n)) / n for _ in range(resamples)]
    boot_means.sort()
    low = boot_means[int(0.025 * resamples) - 1]
    high = boot_means[int(0.975 * resamples) - 1]
    return low, high


def _find_matching_observation(
    connection: sqlite3.Connection,
    round_id: str,
    created_at_dt: datetime,
) -> sqlite3.Row | None:
    max_observed_at = created_at_dt + timedelta(seconds=MAX_BOOK_DELAY_SECONDS)
    rows = connection.execute(
        """
        SELECT observed_at, up_best_ask, down_best_ask,
               up_best_ask_depth_shares, down_best_ask_depth_shares
        FROM complete_set_observations
        WHERE round_id = ?
        ORDER BY observed_at ASC
        """,
        (round_id,),
    ).fetchall()
    valid_obs: list[tuple[datetime, sqlite3.Row]] = []
    for row in rows:
        try:
            obs_dt = _parse_iso(row["observed_at"])
        except Exception:
            continue
        if created_at_dt <= obs_dt <= max_observed_at:
            valid_obs.append((obs_dt, row))
    if valid_obs:
        valid_obs.sort(key=lambda item: item[0])
        return valid_obs[0][1]
    return None


def settle_decisions(connection: sqlite3.Connection, now_iso: str) -> None:
    rows = connection.execute(
        """
        SELECT cad.prediction_id, cad.side, cp.outcome_up
        FROM cheap_ask_decisions cad
        JOIN crypto_predictions cp ON cad.prediction_id = cp.id
        WHERE cad.settled_at IS NULL AND cp.outcome_up IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        win = 1 if (row["outcome_up"] == 1) == (row["side"] == "up") else 0
        connection.execute(
            "UPDATE cheap_ask_decisions SET win = ?, settled_at = ? WHERE prediction_id = ?",
            (win, now_iso, row["prediction_id"]),
        )


def run_once(connection: sqlite3.Connection, *, now: datetime | None = None) -> None:
    initialize_cheap_ask_book(connection)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment_iso = moment.isoformat()
    cutoff = moment - timedelta(minutes=LOOKBACK_MINUTES)
    cutoff_floor = min(cutoff.isoformat(), cutoff.isoformat().replace("T", " "))

    candidate_rows = connection.execute(
        """
        SELECT cp.id, cp.probability_up, cp.created_at, cp.features_json
        FROM crypto_predictions cp
        LEFT JOIN cheap_ask_decisions cad ON cad.prediction_id = cp.id
        WHERE cp.predictor = 'fast:continuation'
          AND cp.instrument = 'BTC-USD'
          AND cad.prediction_id IS NULL
          AND cp.created_at >= ?
        ORDER BY cp.created_at ASC, cp.id ASC
        """,
        (cutoff_floor,),
    ).fetchall()

    for row in candidate_rows:
        try:
            created_dt = _parse_iso(row["created_at"])
        except Exception:
            continue
        if created_dt < cutoff:
            continue

        try:
            features = json.loads(row["features_json"]) if row["features_json"] else {}
        except Exception:
            features = {}
        round_start = features.get("round_start") if isinstance(features, dict) else None
        round_id = None
        if round_start is not None:
            try:
                round_id = round_id_from_round_start(round_start)
            except Exception:
                round_id = None

        prob_up = row["probability_up"]
        if prob_up is None:
            side = None
        else:
            try:
                side = "up" if float(prob_up) > 0.5 else "down"
            except (ValueError, TypeError):
                side = None

        if side is None or round_id is None:
            obs = None
        else:
            obs = _find_matching_observation(connection, round_id, created_dt)

        if obs is None:
            if (
                side is not None
                and round_id is not None
                and moment < created_dt + timedelta(seconds=MAX_BOOK_DELAY_SECONDS)
            ):
                continue
            ask = None
            depth = None
            decision = "no_book"
        else:
            if side == "up":
                ask = obs["up_best_ask"]
                depth = obs["up_best_ask_depth_shares"]
            else:
                ask = obs["down_best_ask"]
                depth = obs["down_best_ask_depth_shares"]
            decision = classify_decision(ask, depth)

        connection.execute(
            """
            INSERT INTO cheap_ask_decisions (
                prediction_id, round_id, decided_at, side, ask, depth, decision, win, settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                round_id,
                moment_iso,
                side,
                ask,
                depth,
                decision,
                None,
                None,
            ),
        )

    settle_decisions(connection, moment_iso)
    connection.commit()


def format_report(connection: sqlite3.Connection) -> str:
    counts_rows = connection.execute(
        "SELECT decision, COUNT(*) AS count FROM cheap_ask_decisions GROUP BY decision ORDER BY decision"
    ).fetchall()
    counts = {row["decision"]: row["count"] for row in counts_rows}

    bet_rows = connection.execute(
        """
        SELECT ask, win
        FROM cheap_ask_decisions
        WHERE decision = 'bet' AND settled_at IS NOT NULL AND win IS NOT NULL
        ORDER BY decided_at ASC, prediction_id ASC
        """
    ).fetchall()

    high_rows = connection.execute(
        """
        SELECT ask, win
        FROM cheap_ask_decisions
        WHERE decision = 'ask_too_high' AND settled_at IS NOT NULL AND win IS NOT NULL
        ORDER BY decided_at ASC, prediction_id ASC
        """
    ).fetchall()

    lines: list[str] = [
        "Cheap Ask Paper Book Report",
        "===========================",
        "",
        "Counts by decision:",
    ]
    for d in ("bet", "ask_too_high", "thin_book", "no_book"):
        lines.append(f"  {d}: {counts.get(d, 0)}")

    lines.append("")
    lines.append("Settled bets:")
    n_bets = len(bet_rows)
    lines.append(f"  Number of bets: {n_bets}")

    if n_bets > 0:
        hit_rate = sum(r["win"] for r in bet_rows) / n_bets
        avg_ask = sum(r["ask"] for r in bet_rows) / n_bets
        profits = [contract_profit(r["win"], r["ask"]) for r in bet_rows]
        avg_profit = sum(profits) / n_bets
        low_ci, high_ci = compute_bootstrap_interval(profits)

        half = n_bets // 2
        if half > 0:
            first_half_avg = sum(profits[:half]) / half
            second_half_avg = sum(profits[half:]) / len(profits[half:])
        else:
            first_half_avg = avg_profit
            second_half_avg = avg_profit

        lines.append(f"  Hit rate: {hit_rate:.3f}")
        lines.append(f"  Average ask: {avg_ask:.3f}")
        lines.append(f"  Average profit per contract: {avg_profit:+.4f} (after {FEE_PER_CONTRACT:.2f} fee)")
        lines.append(f"  95% bootstrap interval: [{low_ci:+.4f}, {high_ci:+.4f}]")
        lines.append(f"  First half average profit: {first_half_avg:+.4f}")
        lines.append(f"  Second half average profit: {second_half_avg:+.4f}")
    else:
        lines.append("  Hit rate: 0.000")
        lines.append("  Average ask: 0.000")
        lines.append(f"  Average profit per contract: +0.0000 (after {FEE_PER_CONTRACT:.2f} fee)")
        lines.append("  95% bootstrap interval: [+0.0000, +0.0000]")
        lines.append("  First half average profit: +0.0000")
        lines.append("  Second half average profit: +0.0000")

    lines.append(f"  Bets so far: {n_bets} of {VERDICT_THRESHOLD} needed for a first verdict")

    lines.append("")
    lines.append("Settled ask_too_high (comparison):")
    n_high = len(high_rows)
    lines.append(f"  Number of rows: {n_high}")
    if n_high > 0:
        high_hit_rate = sum(r["win"] for r in high_rows) / n_high
        high_avg_ask = sum(r["ask"] for r in high_rows) / n_high
        lines.append(f"  Hit rate: {high_hit_rate:.3f}")
        lines.append(f"  Average ask: {high_avg_ask:.3f}")
    else:
        lines.append("  Hit rate: 0.000")
        lines.append("  Average ask: 0.000")

    return "\n".join(lines)
