from pathlib import Path

from andy_trader.baselines import Baseline
from andy_trader.predict import load_closes, predict_once, score_all
from andy_trader.store import Candle, connect, record_observations, settle_due_predictions


def _series(instrument: str = "BTC-USD", venue: str = "kraken", bars: int = 40) -> list[Candle]:
    return [
        Candle(
            instrument=instrument,
            venue=venue,
            interval="1h",
            open_time=f"2026-09-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00",
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1.0,
        )
        for i in range(bars)
    ]


def test_load_closes_returns_oldest_first(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(bars=5))
        closes = load_closes(connection, "BTC-USD")
        assert [c for _, c in closes] == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_load_closes_excludes_degraded_rows(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(
            connection,
            _series(bars=3)
            + [
                Candle(
                    instrument="BTC-USD", venue="coinbase", interval="1h",
                    open_time="2026-09-01T09:00:00+00:00",
                    open=None, high=None, low=None, close=None, volume=None,
                    degraded=True, degraded_reason="unreachable",
                )
            ],
        )
        assert len(load_closes(connection, "BTC-USD")) == 3


def test_load_closes_deduplicates_the_same_bar_across_venues(tmp_path: Path) -> None:
    """Two venues reporting one bar must not become two rows of history."""

    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(venue="kraken", bars=3))
        record_observations(connection, _series(venue="coinbase", bars=3))
        assert len(load_closes(connection, "BTC-USD")) == 3


def test_predict_once_writes_one_call_per_baseline_horizon(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(bars=40))
        baselines = (
            Baseline("always_half", lambda _c: 0.5, minimum_history=1),
            Baseline("always_up", lambda _c: 0.6, minimum_history=1),
        )
        result = predict_once(
            connection,
            instruments=("BTC-USD",),
            horizons=("1h", "4h"),
            baselines=baselines,
            now_iso="2026-09-04T00:00:00+00:00",
        )
        assert result["written"] == 4
        rows = connection.execute("SELECT predictor, horizon, resolves_at FROM crypto_predictions").fetchall()
        assert {row["resolves_at"] for row in rows} == {
            "2026-09-04T01:00:00+00:00",
            "2026-09-04T04:00:00+00:00",
        }


def test_predict_once_records_the_last_close_as_reference_price(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(bars=5))
        predict_once(
            connection,
            instruments=("BTC-USD",),
            horizons=("1h",),
            baselines=(Baseline("half", lambda _c: 0.5, minimum_history=1),),
            now_iso="2026-09-04T00:00:00+00:00",
        )
        row = connection.execute("SELECT reference_price FROM crypto_predictions").fetchone()
        assert row["reference_price"] == 104.0


def test_predict_once_skips_an_instrument_with_no_history(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        result = predict_once(
            connection,
            instruments=("DOGE-USD",),
            horizons=("1h",),
            now_iso="2026-09-04T00:00:00+00:00",
        )
        assert result["written"] == 0
        assert result["skipped"][0]["reason"] == "no non-degraded history"


def test_predict_once_skips_a_baseline_that_lacks_history_but_keeps_the_rest(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(bars=3))
        baselines = (
            Baseline("hungry", lambda _c: 0.6, minimum_history=100),
            Baseline("modest", lambda _c: 0.6, minimum_history=1),
        )
        result = predict_once(
            connection,
            instruments=("BTC-USD",),
            horizons=("1h",),
            baselines=baselines,
            now_iso="2026-09-04T00:00:00+00:00",
        )
        assert result["written"] == 1
        assert result["skipped"][0]["baseline"] == "hungry"


def test_predictions_are_not_rewritten_on_a_second_identical_run(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(bars=5))
        kwargs = {
            "instruments": ("BTC-USD",),
            "horizons": ("1h",),
            "baselines": (Baseline("half", lambda _c: 0.5, minimum_history=1),),
            "now_iso": "2026-09-04T00:00:00+00:00",
        }
        predict_once(connection, **kwargs)
        predict_once(connection, **kwargs)
        count = connection.execute("SELECT COUNT(*) AS n FROM crypto_predictions").fetchone()
        assert count["n"] == 1


def _zigzag_bar(hour: int, close: float) -> Candle:
    return Candle(
        instrument="BTC-USD",
        venue="kraken",
        interval="1h",
        open_time=f"2026-09-01T{hour:02d}:00:00+00:00",
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1.0,
    )


def test_score_all_ranks_a_calibrated_predictor_above_an_overconfident_one(tmp_path: Path) -> None:
    """Walk the clock forward the way production does: observe, predict, observe, settle.

    The series alternates so the base rate lands near 0.5 and the ranking is
    actually decidable. On a one-sided series nothing can be ranked, which is
    what `degenerate` exists to say.
    """

    closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0]
    with connect(tmp_path / "c.db") as connection:
        baselines = (
            Baseline("always_up", lambda _c: 1.0, minimum_history=1),
            Baseline("flip", lambda _c: 0.5, minimum_history=1),
        )
        for hour, close in enumerate(closes):
            record_observations(connection, [_zigzag_bar(hour, close)])
            predict_once(
                connection,
                instruments=("BTC-USD",),
                horizons=("1h",),
                baselines=baselines,
                now_iso=f"2026-09-01T{hour:02d}:00:00+00:00",
            )
        settle_due_predictions(connection, now_iso="2026-09-02T00:00:00+00:00")

        reports = score_all(connection)
        assert not reports["baseline:flip"].degenerate
        # Constant 0.5 scores 0.25; certainty that is wrong half the time scores ~0.5.
        assert reports["baseline:flip"].brier < reports["baseline:always_up"].brier
        assert (
            reports["baseline:flip"].brier_skill_score
            > reports["baseline:always_up"].brier_skill_score
        )


def test_score_all_flags_a_one_sided_sample_as_undecidable(tmp_path: Path) -> None:
    """A rising-only run must not be reported as a win for anyone."""

    with connect(tmp_path / "c.db") as connection:
        for hour in range(1, 4):
            record_observations(connection, _series(bars=hour))
            predict_once(
                connection,
                instruments=("BTC-USD",),
                horizons=("1h",),
                baselines=(Baseline("always_up", lambda _c: 1.0, minimum_history=1),),
                now_iso=f"2026-09-01T{hour - 1:02d}:00:00+00:00",
            )
        record_observations(connection, _series(bars=4))
        settle_due_predictions(connection, now_iso="2026-09-02T00:00:00+00:00")

        report = score_all(connection)["baseline:always_up"]
        assert report.base_rate == 1.0
        assert report.degenerate
        assert not report.beats_base_rate


def test_predict_once_is_documented_as_live_only(tmp_path: Path) -> None:
    """Backdating now_iso prices the call at today, which is why CT-04 must not reuse this."""

    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(bars=6))
        predict_once(
            connection,
            instruments=("BTC-USD",),
            horizons=("1h",),
            baselines=(Baseline("half", lambda _c: 0.5, minimum_history=1),),
            now_iso="2026-09-01T00:00:00+00:00",
        )
        row = connection.execute("SELECT reference_price FROM crypto_predictions").fetchone()
        # The newest close, not the close at the backdated timestamp.
        assert row["reference_price"] == 105.0


def test_score_all_respects_the_minimum_sample_size(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_observations(connection, _series(bars=6))
        predict_once(
            connection,
            instruments=("BTC-USD",),
            horizons=("1h",),
            baselines=(Baseline("half", lambda _c: 0.5, minimum_history=1),),
            now_iso="2026-09-01T00:00:00+00:00",
        )
        settle_due_predictions(connection, now_iso="2026-09-02T00:00:00+00:00")
        assert score_all(connection, minimum=50) == {}
