"""One unattended cycle: collect, log predictions, settle whatever is due.

Designed to be run by a scheduler with no agent involved. Deliberately cheap and
deterministic: it makes HTTP calls and writes rows, and it never reasons about
anything. Reasoning belongs in a scheduled analysis pass over accumulated data,
not inside a loop that runs every fifteen minutes.

Exit code is 0 whenever the cycle completed, including when a venue was
unreachable, because a degraded row is a successful observation of a failure.
It returns non-zero only when the store itself could not be used, which is the
one condition a scheduler should surface.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
import sys
from typing import Sequence

from andy_trader.collector import _http_json, _settings_from_env, collect
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.portfolio import paper_trade_once
from andy_trader.predict import DEFAULT_MAX_DATA_AGE_MINUTES, predict_once
from andy_trader.signals import collect_signals, record_signals
from andy_trader.store import connect, default_database_path, record_observations, settle_due_predictions
from andy_trader.training import predict_with_promoted_model, run_retrain_window, should_retrain

DEFAULT_INSTRUMENTS = (
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD",
    "DOGE-USD", "ADA-USD", "AVAX-USD", "LINK-USD",
)
DEFAULT_INTERVALS = ("1h", "4h")
DEFAULT_HORIZONS = ("1h", "4h")
CYCLE_LOG_PATH = REPO_ROOT / ".cycle-run.jsonl"


def _journal(event: str, **details: object) -> None:
    """Best-effort phase journal for failures that kill Python externally."""

    payload = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **details}
    try:
        with CYCLE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        # Market collection must not fail merely because a diagnostic log is
        # unavailable. The SQLite audit remains the authoritative data record.
        pass


# Bybit alone on the schedule, deliberately. It returns full OHLCV for every
# instrument here, while CoinGecko's free tier starts answering 429 at eight
# instruments and would contribute two degraded rows every quarter of an hour,
# roughly two hundred a day of pure noise. CoinGecko stays available as an
# explicit --venues override and as the fallback when an exchange is DNS-blocked.
SCHEDULED_VENUES = ("bybit",)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", help="Comma-separated override")
    parser.add_argument("--intervals", help="Comma-separated override")
    parser.add_argument("--horizons", help="Comma-separated override")
    parser.add_argument("--venues", help="Comma-separated override")
    parser.add_argument("--quiet", action="store_true", help="Only print on a problem")
    parser.add_argument(
        "--skip-signals", action="store_true",
        help="Prices only. Signals move slowly, so skipping them on a fast cadence is fine.",
    )
    parser.add_argument(
        "--paper-trade",
        help=(
            "Comma-separated predictor=instrument pairs to paper-trade this cycle, e.g. "
            "'baseline:momentum=BTC-USD,baseline:momentum=ETH-USD'. Opt-in only -- nothing "
            "paper-trades unless explicitly listed here or in CRYPTO_PAPER_TRADE_PAIRS."
        ),
    )
    parser.add_argument(
        "--retrain-instruments",
        help=(
            "Comma-separated instruments to check/run CT-07 retraining for this cycle, e.g. "
            "'BTC-USD,ETH-USD'. Opt-in only -- retraining spends real compute, so nothing "
            "retrains unless explicitly listed here or in CRYPTO_RETRAIN_INSTRUMENTS. Unlike "
            "retraining, checking whether a promoted model exists and logging its live call is "
            "always on for every scheduled instrument -- that's the actual release mechanism: "
            "a model starts being scored the moment it earns promotion, with no flag to flip."
        ),
    )
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    settings = _settings_from_env(os.environ)
    instruments = tuple(args.instruments.split(",")) if args.instruments else DEFAULT_INSTRUMENTS
    intervals = tuple(args.intervals.split(",")) if args.intervals else DEFAULT_INTERVALS
    horizons = tuple(args.horizons.split(",")) if args.horizons else DEFAULT_HORIZONS
    venues = tuple(args.venues.split(",")) if args.venues else SCHEDULED_VENUES
    maximum_data_age_minutes = float(
        os.environ.get("CRYPTO_MAX_DATA_AGE_MINUTES", str(DEFAULT_MAX_DATA_AGE_MINUTES))
    )
    paper_trade_spec = args.paper_trade or os.environ.get("CRYPTO_PAPER_TRADE_PAIRS", "")
    paper_trade_pairs: list[tuple[str, str]] = []
    for chunk in paper_trade_spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            print(f"Ignoring malformed --paper-trade entry (expected predictor=instrument): {chunk!r}", file=sys.stderr)
            continue
        predictor_name, _, instrument_name = chunk.partition("=")
        paper_trade_pairs.append((predictor_name.strip(), instrument_name.strip()))

    retrain_spec = args.retrain_instruments or os.environ.get("CRYPTO_RETRAIN_INSTRUMENTS", "")
    retrain_instruments = tuple(i.strip() for i in retrain_spec.split(",") if i.strip())

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    _journal(
        "cycle_started",
        instruments=list(instruments),
        intervals=list(intervals),
        horizons=list(horizons),
        venues=list(venues),
    )
    try:
        candles, problems = collect(
            instruments=instruments,
            intervals=intervals,
            venues=venues,
            settings=settings,
        )
        _journal("primary_prices_collected", candles=len(candles), problems=len(problems))

        # Bybit is intermittently replaced by a PLDT block page. CoinGecko is
        # intentionally queried only for instruments whose live reference
        # interval has no usable primary row, avoiding its free-tier 429s on
        # healthy passes while still preventing a total price outage.
        reference_interval = intervals[0]
        usable = {
            (candle.instrument, candle.interval)
            for candle in candles
            if not candle.degraded and candle.close is not None
        }
        fallback_instruments = tuple(
            instrument
            for instrument in instruments
            if (instrument, reference_interval) not in usable
        )
        if fallback_instruments:
            fallback_candles, fallback_problems = collect(
                instruments=fallback_instruments,
                intervals=(reference_interval,),
                venues=("coingecko",),
                settings=settings,
            )
            candles.extend(fallback_candles)
            problems.extend(fallback_problems)
            _journal(
                "fallback_prices_collected",
                instruments=list(fallback_instruments),
                candles=len(fallback_candles),
                problems=len(fallback_problems),
            )
        signals: list = []
        signal_problems: list = []
        if not args.skip_signals:
            # Funding updates 8-hourly and Fear and Greed daily, so most cycles
            # re-observe unchanged values. That is deliberately cheap: the
            # content hash collapses repeats onto one row and bumps times_seen,
            # so the cost is a request rather than a duplicated series.
            signals, signal_problems = collect_signals(
                instruments,
                http=lambda url: _http_json(url, settings),
            )
        _journal("signals_collected", signals=len(signals), problems=len(signal_problems))

        paper_trade_results: list[dict[str, object]] = []
        with connect(default_database_path()) as connection:
            inserted, seen = record_observations(connection, candles)
            if signals:
                record_signals(connection, signals)
            written = predict_once(
                connection,
                instruments=instruments,
                horizons=horizons,
                interval=reference_interval,
                maximum_data_age_minutes=maximum_data_age_minutes,
            )
            settled = settle_due_predictions(connection, interval=reference_interval)

            # Opt-in, since retraining spends real compute: only the
            # instruments explicitly configured get a new training pass
            # attempted, and only when should_retrain() says enough fresh
            # settled history has accumulated since the last attempt.
            retrain_results: list[dict[str, object]] = []
            for instrument in retrain_instruments:
                if not should_retrain(connection, instrument=instrument, horizon=reference_interval, interval=reference_interval):
                    continue
                try:
                    entry = run_retrain_window(connection, instrument=instrument, interval=reference_interval, horizon=reference_interval)
                    retrain_results.append(
                        {
                            "instrument": instrument,
                            "model_id": entry.model_id,
                            "promoted": entry.promoted,
                            "holdout_brier_skill": entry.holdout_brier_skill,
                            "base_rate_brier_skill": entry.base_rate_brier_skill,
                            "reason": entry.promotion_reason,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - one bad retrain must not kill the cycle
                    retrain_results.append({"instrument": instrument, "error": str(exc)})
            if retrain_results:
                _journal("retrain_completed", attempts=retrain_results)

            # Always on, for every scheduled instrument, regardless of the
            # retrain opt-in above: this is the actual release mechanism. A
            # model starts being scored under the stable "model:promoted"
            # name the moment it clears the gate, with no flag to flip and
            # no human needing to notice. Cheap when nothing is promoted --
            # one registry lookup that returns immediately.
            live_model_results: list[dict[str, object]] = []
            for instrument in instruments:
                result = predict_with_promoted_model(connection, instrument=instrument, interval=reference_interval, horizon=reference_interval)
                if result.get("predicted"):
                    live_model_results.append({"instrument": instrument, **result})
            if live_model_results:
                _journal("live_model_predictions", attempts=live_model_results)

            # Opt-in only: nothing paper-trades unless a pair was explicitly
            # configured. A prediction/price freshness check happens inside
            # paper_trade_once itself, same rule the manual CLI uses -- one
            # refusal rule, not a second copy that could quietly drift.
            for predictor_name, instrument_name in paper_trade_pairs:
                attempt = paper_trade_once(
                    connection,
                    predictor=predictor_name,
                    instrument=instrument_name,
                    interval=reference_interval,
                    horizon=reference_interval,
                    max_data_age_minutes=maximum_data_age_minutes,
                )
                paper_trade_results.append(
                    {
                        "predictor": predictor_name,
                        "instrument": instrument_name,
                        "traded": attempt.trade is not None,
                        "side": attempt.trade.side if attempt.trade else None,
                        "equity": attempt.equity,
                        "skipped_reason": attempt.skipped_reason,
                        # Distinguishes a risk-caused non-event from the
                        # predictor simply deciding not to trade -- before
                        # this, both looked identical in this exact journal.
                        "risk_allowed": attempt.risk_allowed,
                        "risk_reason": attempt.risk_reason,
                        "forced_exit": attempt.forced_exit,
                    }
                )
        if paper_trade_pairs:
            _journal("paper_trade_completed", attempts=paper_trade_results)
        _journal(
            "store_updated",
            observed=seen,
            inserted=inserted,
            predictions=written["written"],
            skipped=len(written["skipped"]),
            settled=settled,
        )
        problems = problems + signal_problems
    except Exception as exc:  # noqa: BLE001 - the scheduler needs one clear failure signal
        _journal("cycle_failed", error_type=type(exc).__name__, error=str(exc)[:400])
        print(f"{stamp} CYCLE FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not args.quiet or problems:
        print(
            f"{stamp} observed={seen} new={inserted} predictions={written['written']} "
            f"settled={settled['settled']}/{settled['due']} degraded={len(problems)}"
        )
    for problem in problems:
        # Price problems carry venue/interval; signal problems carry signal.
        # Reading a fixed key set would KeyError exactly when a source fails,
        # which is the moment the message matters most.
        what = problem.get("venue") or problem.get("signal") or "?"
        where = problem.get("interval") or ""
        print(
            f"  degraded: {what} {problem.get('instrument', '-')} "
            f"{where}: {problem['reason'][:120]}".replace("  ", " ")
        )
    for attempt in retrain_results:
        if "error" in attempt:
            print(f"  retrain: {attempt['instrument']} FAILED: {attempt['error']}")
        else:
            verdict = "PROMOTED" if attempt["promoted"] else "rejected"
            print(
                f"  retrain: {attempt['instrument']} {attempt['model_id']} {verdict} "
                f"(skill={attempt['holdout_brier_skill']:+.4f} vs base={attempt['base_rate_brier_skill']:+.4f})"
            )
    for attempt in live_model_results:
        print(
            f"  model:promoted {attempt['instrument']} -> p(up)={attempt['probability_up']:.4f} "
            f"(model {attempt['model_id']})"
        )
    for attempt in paper_trade_results:
        label = f"{attempt['predictor']} {attempt['instrument']}"
        if attempt["skipped_reason"]:
            print(f"  paper: {label} skipped: {attempt['skipped_reason']}")
        elif attempt["forced_exit"]:
            print(f"  paper: {label} RISK INTERLOCK forced exit: {attempt['risk_reason']}")
        elif not attempt["risk_allowed"]:
            print(f"  paper: {label} risk-blocked: {attempt['risk_reason']}")
        elif attempt["traded"]:
            print(f"  paper: {label} -> {attempt['side']} (equity={attempt['equity']:.2f})")
    _journal("cycle_completed", degraded=len(problems))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
