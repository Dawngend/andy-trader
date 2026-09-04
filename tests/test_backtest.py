from pathlib import Path
from typing import Sequence

import pytest

from andy_trader.backtest import BacktestError, run_backtest
from andy_trader.baselines import Baseline, PredictionContext
from andy_trader.store import Candle, connect, record_observations


def _series(closes: Sequence[float], *, interval: str = "1h") -> list[Candle]:
    return [
        Candle(
            instrument="BTC-USD",
            venue="kraken",
            interval=interval,
            open_time=f"2026-09-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00",
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1.0,
        )
        for index, close in enumerate(closes)
    ]


def test_predictor_cannot_see_a_future_close(tmp_path: Path) -> None:
    """The cheating predictor asks for closes[-1], but receives only the simulated present."""

    seen: list[tuple[float, ...]] = []

    def cheat(closes: Sequence[float]) -> float:
        seen.append(tuple(closes))
        return 0.9 if closes[-1] == 999.0 else 0.1

    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series([100.0, 101.0, 102.0, 103.0, 999.0]))
        result = run_backtest(
            connection,
            instrument="BTC-USD",
            predictors=(Baseline("cheat", cheat, minimum_history=1),),
            minimum_train_bars=2,
        )[0]

    assert seen == [(100.0, 101.0, 102.0), (100.0, 101.0, 102.0, 103.0)]
    assert all(history[-1] != 999.0 for history in seen)
    assert result.windows == 2


def test_fees_and_slippage_are_charged_on_entry_and_exit(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series([100.0, 100.0, 110.0]))
        result = run_backtest(
            connection,
            instrument="BTC-USD",
            predictors=(Baseline("long", lambda _closes: 0.6, minimum_history=1),),
            minimum_train_bars=1,
            fee_bps=10.0,
            slippage_bps=5.0,
        )[0]

    assert result.gross_return == pytest.approx(0.10)
    # 10 bps fee + 5 bps slippage, each on both legs, is a 30 bps round trip.
    assert result.net_return == pytest.approx(0.097)
    assert result.net_return < result.gross_return
    assert result.trades == 1


def test_four_hour_pnl_compounds_only_non_overlapping_trades(tmp_path: Path) -> None:
    """Five forecasts exist, but one unit of capital can take only two serial trades."""

    closes = [99.0, 100.0, 102.0, 105.0, 108.0, 110.0, 112.0, 115.0, 118.0, 121.0]
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(closes))
        result = run_backtest(
            connection,
            instrument="BTC-USD",
            interval="1h",
            horizon="4h",
            predictors=(Baseline("long", lambda _closes: 0.6, minimum_history=1),),
            minimum_train_bars=1,
            fee_bps=0.0,
            slippage_bps=0.0,
        )[0]

    # Entries at indexes 1 and 5 each earn 10%. The three forecasts between
    # them remain calibration observations but cannot reuse committed capital.
    assert result.gross_return == pytest.approx(0.21)
    assert result.net_return == pytest.approx(0.21)
    assert result.trades == 2
    assert result.windows == 5


class _FitRecorder:
    name = "fit_recorder"
    minimum_history = 1

    def __init__(self) -> None:
        self.fit_lengths: list[int] = []
        self.predict_lengths: list[int] = []

    def fit(self, closes: Sequence[float]) -> None:
        self.fit_lengths.append(len(closes))

    @property
    def prediction_name(self) -> str:
        return self.name

    def __call__(
        self,
        closes: Sequence[float],
        *,
        context: PredictionContext,
    ) -> float:
        self.predict_lengths.append(len(closes))
        return 0.5


def test_expanding_fit_excludes_the_current_evaluation_bar(tmp_path: Path) -> None:
    predictor = _FitRecorder()
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series([100.0, 101.0, 100.0, 101.0, 100.0, 101.0]))
        run_backtest(
            connection,
            instrument="BTC-USD",
            predictors=(predictor,),
            minimum_train_bars=2,
            window="expanding",
        )

    assert predictor.fit_lengths == [2, 3, 4]
    assert predictor.predict_lengths == [3, 4, 5]


def test_rolling_window_keeps_a_fixed_training_length(tmp_path: Path) -> None:
    predictor = _FitRecorder()
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series([100.0, 101.0, 100.0, 101.0, 100.0, 101.0]))
        run_backtest(
            connection,
            instrument="BTC-USD",
            predictors=(predictor,),
            minimum_train_bars=2,
            window="rolling",
        )

    assert predictor.fit_lengths == [2, 2, 2]
    assert predictor.predict_lengths == [3, 3, 3]


def test_degenerate_outcomes_are_surfaced(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series([100.0, 101.0, 102.0, 103.0, 104.0]))
        result = run_backtest(
            connection,
            instrument="BTC-USD",
            predictors=(Baseline("up", lambda _closes: 0.6, minimum_history=1),),
            minimum_train_bars=2,
        )[0]

    assert result.report.degenerate
    assert not result.report.beats_base_rate


def test_results_are_ranked_by_brier_skill_score(tmp_path: Path) -> None:
    closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0]
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(closes))
        results = run_backtest(
            connection,
            instrument="BTC-USD",
            predictors=(
                Baseline("wrong", lambda closes: 0.1 if closes[-1] == 100.0 else 0.9),
                Baseline("right", lambda closes: 0.9 if closes[-1] == 100.0 else 0.1),
            ),
            minimum_train_bars=2,
        )

    assert [result.predictor for result in results] == ["right", "wrong"]
    assert results[0].report.brier_skill_score > results[1].report.brier_skill_score


def test_backtest_rejects_a_fractional_horizon_bar(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series([100.0] * 5, interval="4h"))
        with pytest.raises(BacktestError, match="whole number"):
            run_backtest(
                connection,
                instrument="BTC-USD",
                interval="4h",
                horizon="1h",
                predictors=(Baseline("half", lambda _closes: 0.5),),
                minimum_train_bars=2,
            )
