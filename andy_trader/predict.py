"""Log baseline predictions in advance, settle the due ones, and score what settled."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Mapping, Sequence

from andy_trader.baselines import Baseline, BaselineError, default_baselines
from andy_trader.calibration import CalibrationError, evaluate, format_report
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.store import (
    Prediction,
    connect,
    default_database_path,
    fetch_settled,
    horizon_delta,
    record_prediction,
    settle_due_predictions,
    utc_now_iso,
)

DEFAULT_HISTORY_BARS = 200


def load_closes(
    connection: sqlite3.Connection,
    instrument: str,
    *,
    interval: str = "1h",
    limit: int = DEFAULT_HISTORY_BARS,
) -> list[tuple[str, float]]:
    """Most recent non-degraded closes, oldest first.

    One row per open_time. Where several venues reported the same bar the most
    frequently confirmed observation wins, which is a crude consensus but a
    deterministic one, and it keeps a single flaky venue from steering history.
    """

    rows = connection.execute(
        """
        SELECT open_time, close
        FROM (
            SELECT open_time, close, venue, times_seen,
                   ROW_NUMBER() OVER (
                       PARTITION BY open_time ORDER BY times_seen DESC, venue ASC
                   ) AS rank
            FROM crypto_observations
            WHERE instrument = ? AND interval = ? AND degraded = 0 AND close IS NOT NULL
        )
        WHERE rank = 1
        ORDER BY open_time DESC
        LIMIT ?
        """,
        (instrument, interval, limit),
    ).fetchall()
    return [(row["open_time"], float(row["close"])) for row in reversed(rows)]


def predict_once(
    connection: sqlite3.Connection,
    *,
    instruments: Sequence[str],
    horizons: Sequence[str],
    interval: str = "1h",
    baselines: Sequence[Baseline] | None = None,
    now_iso: str | None = None,
    mode: str = "advisory",
) -> dict[str, object]:
    """Write one prediction per baseline per instrument per horizon.

    Every call is written before its outcome exists. Nothing here reads a future
    price, and the settlement job is what fills the outcome later.

    **This is a live-only operation.** `reference_price` is always the most
    recent close in the store, regardless of `now_iso`. Passing a past `now_iso`
    therefore prices the call at today and resolves it against a bar that came
    before it, which is meaningless. `now_iso` exists so tests can pin the clock,
    not so history can be backfilled. Retrospective evaluation belongs in the
    walk-forward backtest (CT-04), which must reconstruct the reference price as
    of each simulated bar rather than calling this function.
    """

    active = tuple(baselines) if baselines is not None else default_baselines()
    now = now_iso or utc_now_iso()
    written = 0
    skipped: list[dict[str, str]] = []

    for instrument in instruments:
        history = load_closes(connection, instrument, interval=interval)
        if not history:
            skipped.append({"instrument": instrument, "reason": "no non-degraded history"})
            continue
        closes = [close for _, close in history]
        reference_price = closes[-1]
        for horizon in horizons:
            resolves_at = (datetime.fromisoformat(now) + horizon_delta(horizon)).isoformat()
            for baseline in active:
                try:
                    probability = baseline(closes)
                except BaselineError as exc:
                    skipped.append(
                        {
                            "instrument": instrument,
                            "baseline": baseline.name,
                            "reason": str(exc),
                        }
                    )
                    continue
                record_prediction(
                    connection,
                    Prediction(
                        predictor=f"baseline:{baseline.name}",
                        instrument=instrument,
                        horizon=horizon,
                        probability_up=probability,
                        reference_price=reference_price,
                        created_at=now,
                        resolves_at=resolves_at,
                        mode=mode,
                        features={
                            "interval": interval,
                            "history_bars": len(closes),
                            "last_close_at": history[-1][0],
                        },
                    ),
                )
                written += 1
    return {"written": written, "skipped": skipped}


def score_all(
    connection: sqlite3.Connection,
    *,
    horizon: str | None = None,
    instrument: str | None = None,
    minimum: int = 1,
) -> dict[str, object]:
    """Score every predictor that has settled calls, ranked by skill score."""

    predictors = [
        row["predictor"]
        for row in connection.execute(
            "SELECT DISTINCT predictor FROM crypto_predictions WHERE settled_at IS NOT NULL"
        ).fetchall()
    ]
    reports: dict[str, object] = {}
    for predictor in sorted(predictors):
        rows = fetch_settled(
            connection, predictor=predictor, instrument=instrument, horizon=horizon
        )
        if len(rows) < minimum:
            continue
        probabilities = [float(row["probability_up"]) for row in rows]
        outcomes = [int(row["outcome_up"]) for row in rows]
        try:
            reports[predictor] = evaluate(probabilities, outcomes)
        except CalibrationError as exc:  # pragma: no cover - defensive
            reports[predictor] = {"error": str(exc)}
    return reports


def _csv(environ: Mapping[str, str], key: str, default: Sequence[str]) -> tuple[str, ...]:
    raw = environ.get(key, "").strip()
    if not raw:
        return tuple(default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("predict", "settle", "score"))
    parser.add_argument("--instruments", help="Comma-separated, e.g. BTC-USD,ETH-USD")
    parser.add_argument("--horizons", help="Comma-separated, e.g. 1h,4h")
    parser.add_argument("--interval", default="1h", help="Observation interval to read")
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--minimum", type=int, default=1, help="Minimum settled calls to score")
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    environ = os.environ
    instruments = tuple(args.instruments.split(",")) if args.instruments else _csv(
        environ, "CRYPTO_INSTRUMENTS", ("BTC-USD", "ETH-USD")
    )
    horizons = tuple(args.horizons.split(",")) if args.horizons else _csv(
        environ, "CRYPTO_HORIZONS", ("1h", "4h")
    )
    database_path = Path(args.database) if args.database else default_database_path(environ)

    with connect(database_path) as connection:
        if args.command == "predict":
            result = predict_once(
                connection,
                instruments=instruments,
                horizons=horizons,
                interval=args.interval,
            )
            print(json.dumps(result) if args.json else f"logged {result['written']} predictions")
            for skip in result["skipped"]:
                print(f"  skipped: {skip}")
            return 0

        if args.command == "settle":
            stats = settle_due_predictions(connection, interval=args.interval)
            print(json.dumps(stats) if args.json else
                  f"due {stats['due']}, settled {stats['settled']}, "
                  f"unresolvable {stats['unresolvable']}")
            return 0

        reports = score_all(connection, minimum=args.minimum)
        if not reports:
            print("nothing settled yet; run 'settle' after a horizon has elapsed")
            return 0
        if args.json:
            print(json.dumps({k: v.as_dict() for k, v in reports.items()}, indent=2))
            return 0
        ranked = sorted(reports.items(), key=lambda kv: kv[1].brier_skill_score, reverse=True)
        for predictor, report in ranked:
            print(format_report(report, predictor=predictor))
            print()
        best = ranked[0]
        print(f"BAR TO BEAT: {best[0]} at skill {best[1].brier_skill_score:+.4f}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
