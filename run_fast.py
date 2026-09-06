"""One pass of the 1-minute continuation strategy. Built to run every minute.

Deliberately separate from `run_cycle.py`. That script collects eight
instruments across several venues, scores every predictor, retrains, checks
demotions and paper-trades; running it once a minute would rate-limit CoinGecko
within the hour and repeat an hour's work sixty times. This does the three
things the fast strategy actually needs and nothing else:

    1. pull fresh 1m bars from the one venue that serves them
    2. settle any sub-hourly calls that have come due, against those 1m bars
    3. emit a call, but only on a round's decision minute and only when the
       move so far is large enough to mean something

Most invocations correctly do nothing but step 1 and 2. The strategy's whole
premise is selectivity, so a run that declines to trade is the normal case, not
a failure.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from andy_trader.collector import FetchSettings, collect
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.fast_momentum import (
    PREDICTOR_NAME,
    build_rounds,
    fit_continuation_curve,
    load_minute_closes,
    predict_round_once,
    settle_fast_predictions,
)
from andy_trader.portfolio import paper_trade_once
from andy_trader.store import connect, default_database_path, record_observations

FAST_LOG_PATH = REPO_ROOT / ".fast-run.jsonl"
DEFAULT_INSTRUMENTS = ("BTC-USD",)
FAST_VENUE = "binance"
FAST_HORIZON = "2m"

# A round's decision point is 2 minutes before it closes; the call is only ever
# actionable inside that window. The 20-minute default freshness tolerance
# elsewhere in this project assumes a 15-minute cycle and would happily "trade"
# a call from a round that closed several cycles ago.
MAX_PREDICTION_AGE_MINUTES = 2.5
MAX_DATA_AGE_MINUTES = 2.5


def _journal(event: str, **details: object) -> None:
    payload = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **details}
    try:
        with FAST_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        # A diagnostic log being unwritable must never cost us a market
        # observation; the SQLite record is the authoritative one.
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", help="Comma-separated, defaults to BTC-USD")
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument("--quiet", action="store_true", help="Only print on a problem")
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    spec = args.instruments or os.environ.get("CRYPTO_FAST_INSTRUMENTS", "")
    instruments = tuple(i.strip() for i in spec.split(",") if i.strip()) or DEFAULT_INSTRUMENTS

    db_path = Path(args.database) if args.database else default_database_path()
    connection = connect(db_path)

    try:
        candles, problems = collect(
            instruments=instruments,
            intervals=("1m",),
            venues=(FAST_VENUE,),
            settings=FetchSettings(timeout_seconds=8.0, retries=1, rate_limit_seconds=0.2),
        )
        if candles:
            record_observations(connection, candles)

        settled = settle_fast_predictions(connection)

        moment = datetime.now(UTC)
        calls: list[dict[str, object]] = []
        trades: list[dict[str, object]] = []
        for instrument in instruments:
            # Fitted only on rounds that already settled, so the curve never
            # sees the round it is about to call.
            history = build_rounds(load_minute_closes(connection, instrument))
            curve = fit_continuation_curve(history)
            prediction = predict_round_once(connection, instrument=instrument, curve=curve)
            if prediction is not None:
                calls.append(
                    {
                        "instrument": instrument,
                        "probability_up": prediction.probability_up,
                        "reference_price": prediction.reference_price,
                        "resolves_at": prediction.resolves_at,
                        "move_bps": prediction.features.get("observed_move_bps"),
                    }
                )

            # Every pass, not only one that just made a new call: the decision
            # window spans the ~2 minutes between the call and the round
            # closing, and this task runs once a minute, so the pass right
            # after a call is exactly when it needs to be actionable. Gated by
            # the same skill gate as every other predictor -- this strategy
            # gets no exemption for being the newest one.
            attempt = paper_trade_once(
                connection,
                predictor=PREDICTOR_NAME,
                instrument=instrument,
                interval="1m",
                horizon=FAST_HORIZON,
                now=moment,
                max_prediction_age_minutes=MAX_PREDICTION_AGE_MINUTES,
                max_data_age_minutes=MAX_DATA_AGE_MINUTES,
            )
            trades.append(
                {
                    "instrument": instrument,
                    "traded": attempt.trade is not None,
                    "side": attempt.trade.side if attempt.trade else None,
                    "skipped_reason": attempt.skipped_reason,
                }
            )

        _journal(
            "fast_pass",
            instruments=list(instruments),
            candles=len(candles),
            problems=len(problems),
            settled=settled.get("settled", 0),
            unresolvable=settled.get("unresolvable", 0),
            calls=calls,
            trades=trades,
        )
        traded_now = [t for t in trades if t["traded"]]
        if not args.quiet or problems or traded_now:
            print(
                f"fast: candles={len(candles)} problems={len(problems)} "
                f"settled={settled.get('settled', 0)} calls={len(calls)}"
            )
            for call in calls:
                print(
                    f"  CALL {call['instrument']} p(up)={call['probability_up']:.4f} "
                    f"move={call['move_bps']}bps resolves={call['resolves_at']}"
                )
            for trade in traded_now:
                print(f"  TRADE {trade['instrument']} -> {trade['side']}")
        return 0
    except Exception as exc:  # noqa: BLE001 - a minute-cadence task must not die loudly
        _journal("fast_pass_failed", error_type=type(exc).__name__, error=str(exc)[:400])
        print(f"fast: failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
