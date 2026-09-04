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
import sys
from typing import Sequence

from andy_trader.collector import FetchSettings, _http_json as _raw_http, collect
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.predict import predict_once
from andy_trader.signals import collect_signals, record_signals
from andy_trader.store import connect, default_database_path, record_observations, settle_due_predictions

DEFAULT_INSTRUMENTS = (
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD",
    "DOGE-USD", "ADA-USD", "AVAX-USD", "LINK-USD",
)
DEFAULT_INTERVALS = ("1h", "4h")
DEFAULT_HORIZONS = ("1h", "4h")


def _http_json(url: str) -> object:
    """Adapter: the signal collector passes only a URL."""

    return _raw_http(url, FetchSettings())


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
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    instruments = tuple(args.instruments.split(",")) if args.instruments else DEFAULT_INSTRUMENTS
    intervals = tuple(args.intervals.split(",")) if args.intervals else DEFAULT_INTERVALS
    horizons = tuple(args.horizons.split(",")) if args.horizons else DEFAULT_HORIZONS
    venues = tuple(args.venues.split(",")) if args.venues else SCHEDULED_VENUES

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        candles, problems = collect(
            instruments=instruments,
            intervals=intervals,
            venues=venues,
            settings=FetchSettings(),
        )
        signals: list = []
        signal_problems: list = []
        if not args.skip_signals:
            # Funding updates 8-hourly and Fear and Greed daily, so most cycles
            # re-observe unchanged values. That is deliberately cheap: the
            # content hash collapses repeats onto one row and bumps times_seen,
            # so the cost is a request rather than a duplicated series.
            signals, signal_problems = collect_signals(instruments, http=_http_json)

        with connect(default_database_path()) as connection:
            inserted, seen = record_observations(connection, candles)
            if signals:
                record_signals(connection, signals)
            written = predict_once(
                connection, instruments=instruments, horizons=horizons, interval=intervals[0]
            )
            settled = settle_due_predictions(connection, interval=intervals[0])
        problems = problems + signal_problems
    except Exception as exc:  # noqa: BLE001 - the scheduler needs one clear failure signal
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
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
