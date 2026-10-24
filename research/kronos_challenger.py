"""Advisory price forecaster using Kronos model predictions."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from typing import Callable, Sequence
import uuid

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from andy_trader.store import (
    Candle,
    Prediction,
    connect,
    default_database_path,
    initialize_database,
    record_prediction,
)

# Same universe as run_probe_watch.INSTRUMENTS, duplicated rather than
# imported so this module never depends on the probe watch script or risk modules.
INSTRUMENTS = tuple(f"{coin}-USD" for coin in ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"))

PREDICTOR_NAME = "kronos:small"
LOOKBACK_CANDLES = 512
HORIZONS = ("4h", "1d")
LOCK_PATH = REPO_ROOT / ".kronos-challenger.lock"


def acquire_lock() -> str | None:
    token = uuid.uuid4().hex
    try:
        if time.time() - LOCK_PATH.stat().st_mtime < 180:
            return None
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    try:
        with LOCK_PATH.open("x") as handle:
            handle.write(token)
    except FileExistsError:
        return None
    return token


def release_lock(token: str | None) -> None:
    if token and LOCK_PATH.exists():
        try:
            if LOCK_PATH.read_text().strip() == token:
                LOCK_PATH.unlink()
        except OSError:
            pass


def _as_utc(dt_or_str: datetime | str) -> datetime:
    if isinstance(dt_or_str, datetime):
        dt = dt_or_str
    else:
        dt = datetime.fromisoformat(dt_or_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_hour_completed(open_time_str: str, now: datetime) -> bool:
    """True if the 1-hour candle starting at open_time has completed as of now."""
    open_dt = _as_utc(open_time_str)
    return open_dt + timedelta(hours=1) <= now


def read_completed_candles(
    connection: sqlite3.Connection,
    instrument: str,
    *,
    now: datetime,
    limit: int = LOOKBACK_CANDLES,
) -> list[Candle]:
    """Read the last `limit` completed hourly Bybit candles, oldest first.

    One row per open_time (latest seen wins). Excludes unfinished candles
    and degraded observations. Returns empty list if fewer than `limit` exist.
    """
    rows = connection.execute(
        """
        SELECT open_time, open, high, low, close, volume, degraded, degraded_reason
        FROM crypto_observations
        WHERE instrument = ? AND venue = 'bybit' AND interval = '1h' AND degraded = 0 AND close IS NOT NULL
        ORDER BY open_time ASC, last_seen_at ASC
        """,
        (instrument,),
    ).fetchall()
    by_open_time: dict[str, Candle] = {}
    for row in rows:
        open_time = row["open_time"]
        if is_hour_completed(open_time, now):
            open_dt = _as_utc(open_time)
            by_open_time[open_dt.isoformat()] = Candle(
                instrument=instrument,
                venue="bybit",
                interval="1h",
                open_time=open_time,
                open=float(row["open"]) if row["open"] is not None else None,
                high=float(row["high"]) if row["high"] is not None else None,
                low=float(row["low"]) if row["low"] is not None else None,
                close=float(row["close"]),
                volume=float(row["volume"]) if row["volume"] is not None else None,
                degraded=bool(row["degraded"]),
                degraded_reason=row["degraded_reason"],
            )
    ordered = [by_open_time[key] for key in sorted(by_open_time)]
    if len(ordered) < limit:
        return []
    return ordered[-limit:]


def already_recorded_this_hour(
    connection: sqlite3.Connection,
    instrument: str,
    now: datetime,
) -> bool:
    """True if kronos:small already recorded predictions for instrument in now's UTC hour."""
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    rows = connection.execute(
        """
        SELECT created_at FROM crypto_predictions
        WHERE predictor = ? AND instrument = ?
        ORDER BY created_at DESC LIMIT 20
        """,
        (PREDICTOR_NAME, instrument),
    ).fetchall()
    for row in rows:
        created_dt = _as_utc(row["created_at"])
        if hour_start <= created_dt < hour_end:
            return True
    return False


def compute_prediction_stats(
    paths: Sequence[Sequence[float]],
    reference_price: float,
) -> tuple[float, float, float, float, float]:
    """Calculate probabilities, pred_vol_bps, and median closes for 4h and 1d horizons."""
    num_paths = len(paths)
    if num_paths == 0:
        raise ValueError("paths must not be empty")

    # Step 4 (index 3) and Step 24 (index 23)
    closes_4h = [float(p[3]) for p in paths]
    closes_1d = [float(p[23]) for p in paths]

    share_4h = sum(1 for c in closes_4h if c > reference_price) / num_paths
    share_1d = sum(1 for c in closes_1d if c > reference_price) / num_paths

    prob_4h = min(0.98, max(0.02, share_4h))
    prob_1d = min(0.98, max(0.02, share_1d))

    median_close_4h = float(statistics.median(closes_4h))
    median_close_1d = float(statistics.median(closes_1d))

    # Mean path across all simulated paths for 24 steps
    mean_path = [sum(float(p[step]) for p in paths) / num_paths for step in range(24)]
    levels = [reference_price] + mean_path
    log_returns = [
        math.log(levels[i] / levels[i - 1])
        for i in range(1, 25)
        if levels[i - 1] > 0 and levels[i] > 0
    ]
    pred_vol_bps = statistics.pstdev(log_returns) * 10_000.0 if len(log_returns) == 24 else 0.0

    return prob_4h, prob_1d, pred_vol_bps, median_close_4h, median_close_1d


def run_once(
    connection: sqlite3.Connection,
    *,
    forecast: Callable[[list[Candle]], list[list[float]]],
    now: datetime | str | None = None,
) -> list[dict[str, object]]:
    """Run one pass of Kronos challenger forecasts across configured instruments."""
    initialize_database(connection)
    moment = _as_utc(now) if now is not None else datetime.now(UTC)
    results: list[dict[str, object]] = []

    for instrument in INSTRUMENTS:
        if already_recorded_this_hour(connection, instrument, moment):
            results.append({"instrument": instrument, "status": "skipped", "reason": "already recorded this hour"})
            continue

        candles = read_completed_candles(connection, instrument, now=moment, limit=LOOKBACK_CANDLES)
        if len(candles) < LOOKBACK_CANDLES:
            results.append({"instrument": instrument, "status": "skipped", "reason": f"fewer than {LOOKBACK_CANDLES} candles"})
            continue

        reference_price = float(candles[-1].close)
        paths = forecast(candles)
        prob_4h, prob_1d, pred_vol_bps, median_4h, median_1d = compute_prediction_stats(paths, reference_price)

        created_at_iso = moment.isoformat()
        resolves_at_4h = (moment + timedelta(hours=4)).isoformat()
        resolves_at_1d = (moment + timedelta(hours=24)).isoformat()

        features_4h = {
            "paths": len(paths),
            "pred_vol_bps": pred_vol_bps,
            "median_close": median_4h,
        }
        features_1d = {
            "paths": len(paths),
            "pred_vol_bps": pred_vol_bps,
            "median_close": median_1d,
        }

        pred_4h = Prediction(
            predictor=PREDICTOR_NAME,
            instrument=instrument,
            horizon="4h",
            probability_up=prob_4h,
            reference_price=reference_price,
            mode="advisory",
            features=features_4h,
            created_at=created_at_iso,
            resolves_at=resolves_at_4h,
        )
        pred_1d = Prediction(
            predictor=PREDICTOR_NAME,
            instrument=instrument,
            horizon="1d",
            probability_up=prob_1d,
            reference_price=reference_price,
            mode="advisory",
            features=features_1d,
            created_at=created_at_iso,
            resolves_at=resolves_at_1d,
        )

        record_prediction(connection, pred_4h)
        record_prediction(connection, pred_1d)

        results.append({
            "instrument": instrument,
            "status": "recorded",
            "p_4h": prob_4h,
            "p_1d": prob_1d,
        })

    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="Path to SQLite database")
    args = parser.parse_args(argv)

    token = acquire_lock()
    if token is None:
        return 0

    try:
        kronos_dir = Path(r"D:\Verify_Posts\src_kronos")
        if str(kronos_dir) not in sys.path:
            sys.path.insert(0, str(kronos_dir))

        import torch
        import pandas as pd
        from model import Kronos, KronosTokenizer, KronosPredictor

        device = "cuda" if torch.cuda.is_available() else "cpu"
        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
        predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)

        def kronos_forecast(candles: list[Candle]) -> list[list[float]]:
            df = pd.DataFrame([
                {
                    "open": float(getattr(c, "open", c["open"])),
                    "high": float(getattr(c, "high", c["high"])),
                    "low": float(getattr(c, "low", c["low"])),
                    "close": float(getattr(c, "close", c["close"])),
                    "volume": float(getattr(c, "volume", c["volume"]) or 0.0),
                }
                for c in candles
            ])
            open_times = [getattr(c, "open_time", c["open_time"]) for c in candles]
            x_timestamp = pd.Series(pd.to_datetime(open_times))
            last_dt = x_timestamp.iloc[-1]
            y_timestamp = pd.Series(pd.date_range(start=last_dt + pd.Timedelta(hours=1), periods=24, freq="1h"))

            df_list = [df.copy() for _ in range(20)]
            x_list = [x_timestamp for _ in range(20)]
            y_list = [y_timestamp for _ in range(20)]

            pred_dfs = predictor.predict_batch(
                df_list=df_list,
                x_timestamp_list=x_list,
                y_timestamp_list=y_list,
                pred_len=24,
                T=1.0,
                top_p=0.9,
                sample_count=1,
                verbose=False,
            )
            return [[float(v) for v in pdf["close"]] for pdf in pred_dfs]

        db_path = Path(args.database) if args.database else default_database_path()
        with connect(db_path) as connection:
            results = run_once(connection, forecast=kronos_forecast)

        for item in results:
            if item["status"] == "recorded":
                print(f"{item['instrument']}: 4h={item['p_4h']:.2f} 1d={item['p_1d']:.2f}")
            else:
                print(f"{item['instrument']}: skipped ({item['reason']})")
        return 0
    except Exception as exc:
        print(f"kronos_challenger failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        release_lock(token)


if __name__ == "__main__":
    raise SystemExit(main())
