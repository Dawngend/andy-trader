from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Sequence

import pytest

from andy_trader.backtest import run_backtest
from andy_trader.baselines import MAX_CONFIDENCE, MIN_CONFIDENCE, PredictionContext
from andy_trader.predict import predict_once
from andy_trader.signal_predictors import (
    SignalPredictor,
    crowd_contrarian,
    crowd_momentum,
    default_signal_predictors,
    fear_greed_contrarian,
    funding_contrarian,
)
from andy_trader.signals import FEAR_GREED, FUNDING_RATE, LONG_RATIO, Signal, record_signals
from andy_trader.store import Candle, connect, record_observations


def _signals(
    name: str,
    values: Sequence[float],
    *,
    instrument: str | None,
    start: datetime,
    step: timedelta = timedelta(hours=1),
) -> list[Signal]:
    return [
        Signal(
            signal=name,
            source="test",
            instrument=instrument,
            value=value,
            observed_time=(start + index * step).isoformat(),
        )
        for index, value in enumerate(values)
    ]


def _candles(closes: Sequence[float]) -> list[Candle]:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return [
        Candle(
            instrument="BTC-USD",
            venue="kraken",
            interval="1h",
            open_time=(start + index * timedelta(hours=1)).isoformat(),
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1.0,
        )
        for index, close in enumerate(closes)
    ]


def _context(
    connection: sqlite3.Connection,
    instrument: str,
    at_or_before: str,
) -> PredictionContext:
    return PredictionContext(connection, instrument, at_or_before)


def test_crowd_contrarian_and_momentum_take_opposite_sides(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    values = [0.50 + index / 1_000 for index in range(20)] + [0.80]
    with connect(tmp_path / "c.db") as connection:
        record_signals(connection, _signals(LONG_RATIO, values, instrument="BTC-USD", start=start))
        context = _context(connection, "BTC-USD", (start + timedelta(hours=20)).isoformat())
        contrarian = crowd_contrarian()([100.0], context=context)
        momentum = crowd_momentum()([100.0], context=context)

    assert contrarian < 0.5 < momentum
    assert contrarian >= MIN_CONFIDENCE
    assert momentum <= MAX_CONFIDENCE


@pytest.mark.parametrize(
    ("predictor", "signal", "instrument", "step"),
    (
        (funding_contrarian(), FUNDING_RATE, "BTC-USD", timedelta(hours=8)),
        (fear_greed_contrarian(), FEAR_GREED, None, timedelta(days=1)),
    ),
)
def test_other_contrarians_fade_a_high_own_history_percentile(
    tmp_path: Path,
    predictor: SignalPredictor,
    signal: str,
    instrument: str | None,
    step: timedelta,
) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    values = [float(index) for index in range(20)] + [100.0]
    with connect(tmp_path / "c.db") as connection:
        record_signals(
            connection,
            _signals(signal, values, instrument=instrument, start=start, step=step),
        )
        at_or_before = (start + 20 * step).isoformat()
        probability = predictor(
            [100.0], context=_context(connection, "BTC-USD", at_or_before)
        )

    assert MIN_CONFIDENCE <= probability < 0.5


def test_missing_or_stale_signal_abstains(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    with connect(tmp_path / "c.db") as connection:
        predictor = crowd_contrarian()
        missing = predictor(
            [100.0], context=_context(connection, "BTC-USD", start.isoformat())
        )
        record_signals(
            connection,
            _signals(
                LONG_RATIO,
                [0.50 + index / 1_000 for index in range(20)],
                instrument="BTC-USD",
                start=start,
            ),
        )
        stale = predictor(
            [100.0],
            context=_context(connection, "BTC-USD", (start + timedelta(days=2)).isoformat()),
        )

    assert missing == 0.5
    assert stale == 0.5


def test_doge_is_normalised_against_doge_not_an_absolute_long_ratio(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    doge_values = [0.75 + index * 0.002 for index in range(20)] + [0.77]
    btc_values = [0.50 + index * 0.001 for index in range(20)] + [0.60]
    at_or_before = (start + timedelta(hours=20)).isoformat()
    with connect(tmp_path / "c.db") as connection:
        record_signals(
            connection,
            _signals(LONG_RATIO, doge_values, instrument="DOGE-USD", start=start)
            + _signals(LONG_RATIO, btc_values, instrument="BTC-USD", start=start),
        )
        predictor = crowd_contrarian()
        doge = predictor(
            [0.1], context=_context(connection, "DOGE-USD", at_or_before)
        )
        btc = predictor(
            [100.0], context=_context(connection, "BTC-USD", at_or_before)
        )

    assert doge == 0.5
    assert btc < 0.5


class _RecordingSignalPredictor:
    name = "recording_crowd_contrarian"
    minimum_history = 1

    def __init__(self) -> None:
        self.inner = crowd_contrarian()
        self.cutoffs: list[str] = []
        self.probabilities: list[float] = []

    @property
    def prediction_name(self) -> str:
        return f"signal:{self.name}"

    def __call__(
        self,
        closes: Sequence[float],
        *,
        context: PredictionContext,
    ) -> float:
        self.cutoffs.append(context.at_or_before)
        probability = self.inner(closes, context=context)
        self.probabilities.append(probability)
        return probability


def test_backtest_never_exposes_a_future_signal(tmp_path: Path) -> None:
    """The future extreme must not reach either simulated prediction window."""

    start = datetime(2026, 8, 31, 6, tzinfo=UTC)
    historical = [0.50 + abs(10 - index) / 1_000 for index in range(20)]
    future = Signal(
        LONG_RATIO,
        "test",
        0.99,
        "2026-09-01T04:00:00+00:00",
        instrument="BTC-USD",
    )
    predictor = _RecordingSignalPredictor()
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _candles([100.0, 101.0, 102.0, 103.0, 104.0]))
        record_signals(
            connection, _signals(LONG_RATIO, historical, instrument="BTC-USD", start=start)
        )
        expected = crowd_contrarian()(
            [100.0],
            context=_context(connection, "BTC-USD", "2026-09-01T02:00:00+00:00"),
        )
        record_signals(connection, [future])
        result = run_backtest(
            connection,
            instrument="BTC-USD",
            predictors=(predictor,),
            minimum_train_bars=2,
        )[0]

    assert predictor.cutoffs == [
        "2026-09-01T02:00:00+00:00",
        "2026-09-01T03:00:00+00:00",
    ]
    assert predictor.probabilities == pytest.approx([expected, expected])
    assert expected > MIN_CONFIDENCE
    assert result.windows == 2


def test_predict_once_records_signal_predictor_through_the_shared_path(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _candles([100.0 + index for index in range(30)]))
        record_signals(
            connection,
            _signals(
                LONG_RATIO,
                [0.50 + index / 1_000 for index in range(29)] + [0.80],
                instrument="BTC-USD",
                start=start,
            ),
        )
        result = predict_once(
            connection,
            instruments=("BTC-USD",),
            horizons=("1h",),
            predictors=(crowd_contrarian(),),
            now_iso="2026-09-02T05:00:00+00:00",
        )
        row = connection.execute("SELECT * FROM crypto_predictions").fetchone()

    assert result["written"] == 1
    assert row["predictor"] == "signal:crowd_contrarian"
    assert 0.0 <= row["probability_up"] <= 1.0
    features = json.loads(row["features_json"])
    assert features["signal"] == LONG_RATIO
    assert features["signal_status"] == "used"


def test_default_signal_predictor_names_are_unique() -> None:
    predictors = default_signal_predictors()
    assert {predictor.name for predictor in predictors} == {
        "crowd_contrarian",
        "funding_contrarian",
        "fear_greed_contrarian",
        "crowd_momentum",
    }
