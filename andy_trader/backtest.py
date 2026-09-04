"""Replay predictors walk-forward against historical closes with explicit trading costs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Mapping, Sequence

from andy_trader.baselines import Baseline, PredictionContext, Predictor, default_baselines
from andy_trader.calibration import CalibrationReport, evaluate
from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.predict import load_closes
from andy_trader.store import connect, default_database_path, horizon_delta


class BacktestError(ValueError):
    """Raised when a backtest configuration cannot produce an honest comparison."""


@dataclass(frozen=True)
class BacktestResult:
    predictor: str
    report: CalibrationReport
    gross_return: float
    net_return: float
    trades: int
    max_drawdown: float
    windows: int

    def as_dict(self) -> dict[str, object]:
        return {
            "predictor": self.predictor,
            "report": self.report.as_dict(),
            "gross_return": self.gross_return,
            "net_return": self.net_return,
            "trades": self.trades,
            "max_drawdown": self.max_drawdown,
            "windows": self.windows,
        }


@dataclass
class _Run:
    probabilities: list[float]
    outcomes: list[int]
    gross_equity: float = 1.0
    net_equity: float = 1.0
    net_peak: float = 1.0
    max_drawdown: float = 0.0
    trades: int = 0
    next_available_index: int = 0


def _name(predictor: object) -> str:
    value = getattr(predictor, "name", None)
    if not isinstance(value, str) or not value:
        raise BacktestError("Every predictor must expose a non-empty string name")
    return value


def _minimum_history(predictor: object) -> int:
    value = getattr(predictor, "minimum_history", 1)
    if not isinstance(value, int) or value < 1:
        raise BacktestError(f"{_name(predictor)} has invalid minimum_history {value!r}")
    return value


def _call(
    predictor: Predictor,
    closes: Sequence[float],
    *,
    context: PredictionContext,
) -> float:
    if not callable(predictor):
        raise BacktestError(f"{_name(predictor)} is not callable")
    probability = float(predictor(closes, context=context))
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise BacktestError(f"{_name(predictor)} produced invalid probability {probability!r}")
    return probability


def _step_bars(interval: str, horizon: str) -> int:
    interval_seconds = horizon_delta(interval).total_seconds()
    horizon_seconds = horizon_delta(horizon).total_seconds()
    ratio = horizon_seconds / interval_seconds
    if ratio < 1 or not ratio.is_integer():
        raise BacktestError(
            f"horizon {horizon!r} must be a whole number of {interval!r} bars"
        )
    return int(ratio)


def _visible_histories(
    closes: Sequence[float], current_index: int, window: str, minimum_train_bars: int
) -> tuple[list[float], list[float]]:
    start = 0 if window == "expanding" else current_index - minimum_train_bars
    # The current simulated bar is evaluation context, never a fitted label.
    # A model's fit() gets only bars strictly before it, while predict gets the
    # current close as the final element. This split is the anti-lookahead wall.
    train = list(closes[start:current_index])
    visible = list(closes[start:current_index + 1])
    return train, visible


def run_backtest(
    connection: sqlite3.Connection,
    *,
    instrument: str,
    interval: str = "1h",
    horizon: str = "1h",
    predictors: Sequence[Predictor],
    fee_bps: float = 10.0,
    slippage_bps: float = 5.0,
    window: str = "expanding",
    minimum_train_bars: int = 100,
) -> list[BacktestResult]:
    """Replay each predictor without ever exposing a future close.

    Expanding windows keep every prior bar. Rolling windows keep exactly
    `minimum_train_bars` bars for fitting plus the current bar for prediction;
    the existing argument doubles as the rolling length because the public CT-04
    signature has no separate window-size parameter.

    Calibration scores every forecast window, including overlapping horizons.
    Equity uses one fixed-size long or short position at a time: when a trade
    spans multiple bars, accounting advances by that many bars before deploying
    the same capital again. A probability of exactly 0.5 is no trade. Fee and
    slippage are each charged once on entry and once on exit, so net period
    return is gross less two round trips in bps.
    """

    if window not in {"expanding", "rolling"}:
        raise BacktestError(f"window must be 'expanding' or 'rolling', got {window!r}")
    if minimum_train_bars < 1:
        raise BacktestError("minimum_train_bars must be at least 1")
    if fee_bps < 0 or slippage_bps < 0:
        raise BacktestError("fee_bps and slippage_bps cannot be negative")
    if not predictors:
        raise BacktestError("At least one predictor is required")

    names = [_name(predictor) for predictor in predictors]
    if len(names) != len(set(names)):
        raise BacktestError("Predictor names must be unique")

    # Reuse the live loader's deterministic venue selection instead of creating
    # a second, subtly different cross-venue series. Bybit remains USDT-quoted;
    # this is the same deliberate one-row-per-open-time convention documented by
    # predict.load_closes, not a claim that USDT and USD are identical.
    history = load_closes(connection, instrument, interval=interval, limit=2_147_483_647)
    closes = [close for _, close in history]
    step = _step_bars(interval, horizon)
    if len(closes) < minimum_train_bars + step + 1:
        raise BacktestError(
            f"{instrument} {interval} has {len(closes)} usable closes; need at least "
            f"{minimum_train_bars + step + 1}"
        )

    runs = {name: _Run([], []) for name in names}
    cost = 2.0 * (fee_bps + slippage_bps) / 10_000.0

    for current_index in range(minimum_train_bars, len(closes) - step):
        train, visible = _visible_histories(closes, current_index, window, minimum_train_bars)
        reference_price = closes[current_index]
        settle_price = closes[current_index + step]
        outcome = 1 if settle_price > reference_price else 0

        for predictor in predictors:
            if len(visible) < _minimum_history(predictor):
                continue
            fit = getattr(predictor, "fit", None)
            if callable(fit):
                fit(train)
            probability = _call(
                predictor,
                visible,
                context=PredictionContext(
                    connection=connection,
                    instrument=instrument,
                    at_or_before=history[current_index][0],
                ),
            )
            run = runs[_name(predictor)]
            run.probabilities.append(probability)
            run.outcomes.append(outcome)

            direction = 1 if probability > 0.5 else -1 if probability < 0.5 else 0
            # The 4h incident originally compounded every hourly 4h forecast as
            # though four overlapping positions could each reuse 100% of the
            # same equity. Keep those forecasts for calibration, but execute
            # only the serial trade schedule that one unit of capital can fund.
            if direction and current_index >= run.next_available_index:
                run.trades += 1
                run.next_available_index = current_index + step
                asset_return = (settle_price - reference_price) / reference_price
                gross_period = direction * asset_return
                net_period = gross_period - cost
                run.gross_equity *= 1.0 + gross_period
                run.net_equity *= 1.0 + net_period
            run.net_peak = max(run.net_peak, run.net_equity)
            drawdown = (run.net_peak - run.net_equity) / run.net_peak
            run.max_drawdown = max(run.max_drawdown, drawdown)

    results: list[BacktestResult] = []
    for name, run in runs.items():
        if not run.outcomes:
            raise BacktestError(f"{name} produced no evaluable windows")
        results.append(
            BacktestResult(
                predictor=name,
                report=evaluate(run.probabilities, run.outcomes),
                gross_return=run.gross_equity - 1.0,
                net_return=run.net_equity - 1.0,
                trades=run.trades,
                max_drawdown=run.max_drawdown,
                windows=len(run.outcomes),
            )
        )
    return sorted(results, key=lambda result: result.report.brier_skill_score, reverse=True)


def _number(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise BacktestError(f"{key} must be a number, got {raw!r}") from exc


def _format_results(results: Sequence[BacktestResult], baseline_names: set[str]) -> str:
    lines = [
        f"{'rank':>4}  {'predictor':<25} {'skill':>9} {'Brier':>9} "
        f"{'gross':>10} {'net':>10} {'trades':>7} {'drawdown':>10} {'windows':>8} status"
    ]
    for rank, result in enumerate(results, 1):
        status = "DEGENERATE" if result.report.degenerate else "ok"
        lines.append(
            f"{rank:>4}  {result.predictor:<25} "
            f"{result.report.brier_skill_score:>+9.4f} {result.report.brier:>9.4f} "
            f"{result.gross_return:>+10.2%} {result.net_return:>+10.2%} "
            f"{result.trades:>7} {result.max_drawdown:>10.2%} "
            f"{result.windows:>8} {status}"
        )

    baseline_results = [result for result in results if result.predictor in baseline_names]
    if baseline_results:
        best = max(baseline_results, key=lambda result: result.net_return)
        challengers = [
            result.predictor
            for result in results
            if result.predictor not in baseline_names and result.net_return > best.net_return
        ]
        lines.append("")
        lines.append(f"BEST BASELINE NET: {best.predictor} at {best.net_return:+.2%}")
        lines.append(
            "BEAT BEST BASELINE NET OF COSTS: " + (", ".join(challengers) if challengers else "none")
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", required=True, help="Instrument, e.g. BTC-USD")
    parser.add_argument("--interval", default="1h", help="Observation interval to replay")
    parser.add_argument("--horizon", default="1h", help="Prediction horizon")
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    parser.add_argument("--fee-bps", type=float, help="Fee charged on entry and exit")
    parser.add_argument("--slippage-bps", type=float, help="Slippage on entry and exit")
    parser.add_argument("--window", choices=("expanding", "rolling"), default="expanding")
    parser.add_argument("--minimum-train-bars", type=int, default=100)
    parser.add_argument(
        "--include-model",
        action="store_true",
        help="Evaluate the experimental PyTorch candidate alongside the baselines",
    )
    parser.add_argument(
        "--include-signals",
        action="store_true",
        help="Evaluate positioning and sentiment predictors alongside the baselines",
    )
    parser.add_argument("--model-seed", type=int, default=1729)
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    environ = os.environ
    fee_bps = args.fee_bps if args.fee_bps is not None else _number(
        environ, "CRYPTO_BACKTEST_FEE_BPS", 10.0
    )
    slippage_bps = args.slippage_bps if args.slippage_bps is not None else _number(
        environ, "CRYPTO_BACKTEST_SLIPPAGE_BPS", 5.0
    )
    database_path = Path(args.database) if args.database else default_database_path(environ)
    predictors: tuple[Predictor, ...] = default_baselines()
    if args.include_model:
        # CT-05 remains opt-in until its out-of-sample result clears CT-03. The
        # import stays lazy so collectors, storage, scoring, and baseline
        # backtests keep working on machines where optional torch is absent.
        from andy_trader.model import TorchPredictor

        predictors += (TorchPredictor(seed=args.model_seed),)
    if args.include_signals:
        from andy_trader.signal_predictors import default_signal_predictors

        predictors += default_signal_predictors()

    with connect(database_path) as connection:
        results = run_backtest(
            connection,
            instrument=args.instrument,
            interval=args.interval,
            horizon=args.horizon,
            predictors=predictors,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            window=args.window,
            minimum_train_bars=args.minimum_train_bars,
        )
    if args.json:
        print(json.dumps([result.as_dict() for result in results], indent=2))
    else:
        print(
            _format_results(
                results,
                {predictor.name for predictor in predictors if isinstance(predictor, Baseline)},
            )
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
