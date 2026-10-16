"""Compare paper stake rules on settled calls without changing the database."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import random
import sqlite3

from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.portfolio import (
    DEFAULT_FEE_BPS, DEFAULT_SLIPPAGE_BPS, configured_starting_cash,
)
from andy_trader.sizing import realized_volatility, size_fraction
from andy_trader.store import default_database_path

RULES = ("flat", "compound", "win_streak", "double")
SHUFFLES = 200
SEED = 42


@dataclass(frozen=True)
class Call:
    direction: int
    price_return: float
    fraction: float


@dataclass(frozen=True)
class Result:
    equity: float
    max_drawdown: float
    trades: int


def load_calls(connection: sqlite3.Connection) -> list[Call]:
    """Use only settled paper calls and hourly bars known when each call was made."""
    rows = connection.execute("""
        SELECT instrument, probability_up, reference_price, settle_price, created_at
        FROM crypto_predictions
        WHERE mode = 'paper' AND settled_at IS NOT NULL AND outcome_up IS NOT NULL
          AND settle_price IS NOT NULL AND reference_price > 0
        ORDER BY created_at, id
    """).fetchall()
    calls = []
    for instrument, probability, entry, exit_price, created_at in rows:
        direction = 1 if probability > 0.5 else -1 if probability < 0.5 else 0
        if not direction:
            continue
        # open_time is the start of the hourly bar; its close is known an hour later.
        cutoff = (datetime.fromisoformat(created_at) - timedelta(hours=1)).isoformat()
        closes = connection.execute("""
            SELECT close FROM (
                SELECT open_time, close, ROW_NUMBER() OVER (
                    PARTITION BY open_time ORDER BY times_seen DESC, venue, first_seen_at DESC
                ) AS rank
                FROM crypto_observations
                WHERE instrument = ? AND interval = '1h' AND degraded = 0
                  AND close IS NOT NULL AND open_time <= ? AND first_seen_at <= ?
            ) WHERE rank = 1
            ORDER BY open_time DESC LIMIT 24
        """, (instrument, cutoff, created_at)).fetchall()
        calls.append(Call(direction, (exit_price - entry) / entry,
                          size_fraction(realized_volatility([row[0] for row in reversed(closes)]))))
    return calls


def replay(calls: list[Call], rule: str, cash: float, cost_rate: float) -> Result:
    if rule not in RULES:
        raise ValueError(f"unknown stake rule: {rule}")
    equity = peak = cash
    max_drawdown = 0.0
    streak = 0
    trades = 0
    for call in calls:
        if equity <= 0:
            break
        multiplier = (1.25 if rule == "win_streak" else 2.0) ** streak if rule in ("win_streak", "double") else 1.0
        fraction = min(1.0, call.fraction * multiplier)
        basis = cash if rule == "flat" else equity
        stake = min(equity, basis * fraction)
        ratio = 1.0 + call.price_return
        if call.direction > 0:
            pnl = stake * ((1.0 - cost_rate) ** 2 * ratio - 1.0)
        else:
            pnl = stake * ((1.0 - cost_rate) - ratio * (1.0 + cost_rate))
        equity = max(0.0, equity + pnl)
        trades += 1
        streak = streak + 1 if pnl > 0 else 0
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return Result(equity, max_drawdown, trades)


def compare(calls: list[Call], cash: float, shuffles: int = SHUFFLES, seed: int = SEED) -> dict[str, tuple[Result, Result]]:
    if cash <= 0 or shuffles < 1:
        raise ValueError("cash and shuffles must be positive")
    cost_rate = (DEFAULT_FEE_BPS + DEFAULT_SLIPPAGE_BPS) / 10_000
    actual = {rule: replay(calls, rule, cash, cost_rate) for rule in RULES}
    totals = {rule: [0.0, 0.0, 0.0] for rule in RULES}
    rng = random.Random(seed)
    for _ in range(shuffles):
        outcomes = [(call.direction, call.price_return) for call in calls]
        rng.shuffle(outcomes)
        shuffled = [Call(direction, price_return, call.fraction)
                    for call, (direction, price_return) in zip(calls, outcomes)]
        for rule in RULES:
            result = replay(shuffled, rule, cash, cost_rate)
            totals[rule][0] += result.equity
            totals[rule][1] += result.max_drawdown
            totals[rule][2] += result.trades
    return {
        rule: (actual[rule], Result(totals[rule][0] / shuffles,
                                     totals[rule][1] / shuffles, totals[rule][2] / shuffles))
        for rule in RULES
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args(argv)
    load_env_file(REPO_ROOT / ".env")
    path = args.database or default_database_path()
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as connection:
        calls = load_calls(connection)
    results = compare(calls, configured_starting_cash())
    print("Rule        Final equity  Max drawdown  Trades  Shuffle equity  Shuffle drawdown  Shuffle trades")
    for rule, (actual, shuffled) in results.items():
        print(f"{rule:<11} {actual.equity:>12.2f}  {actual.max_drawdown:>11.2%}  {actual.trades:>6}"
              f"  {shuffled.equity:>14.2f}  {shuffled.max_drawdown:>16.2%}  {shuffled.trades:>14.1f}")
    if not calls:
        print("Verdict: No settled paper calls to compare.")
    else:
        best = max(results, key=lambda rule: results[rule][0].equity)
        print(f"Verdict: {best} finished with the highest equity in time order; shuffled figures are averages over {SHUFFLES} runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
