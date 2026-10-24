from datetime import UTC, datetime, timedelta
import json
import statistics
import subprocess
import sys

import pytest

from andy_trader.store import Candle, connect, record_observations
from research import kronos_challenger

NOW = datetime(2026, 10, 25, 14, 30, tzinfo=UTC)
INSTRUMENT = "BTC-USD"


@pytest.fixture
def db(tmp_path):
    with connect(tmp_path / "test.db") as connection:
        yield connection


def make_candle(
    instrument: str,
    open_time: datetime,
    close: float = 100.0,
    *,
    venue: str = "bybit",
    interval: str = "1h",
    degraded: bool = False,
) -> Candle:
    return Candle(
        instrument=instrument,
        venue=venue,
        interval=interval,
        open_time=open_time.isoformat(),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=10.0,
        degraded=degraded,
    )


def make_candles(
    count: int,
    end_time: datetime,
    close: float = 100.0,
    instrument: str = INSTRUMENT,
) -> list[Candle]:
    """Chronologically sorted sequence of hourly candles ending at end_time."""
    return [
        make_candle(
            instrument,
            end_time - timedelta(hours=count - 1 - i),
            close=close,
        )
        for i in range(count)
    ]


def test_512_candles_give_two_predictions(db, monkeypatch):
    """(a) 512 candles give two predictions with horizons 4h and 1d."""
    monkeypatch.setattr(kronos_challenger, "INSTRUMENTS", (INSTRUMENT,))
    end_time = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    record_observations(db, make_candles(512, end_time=end_time, close=100.0))

    fake_forecast = lambda c: [[105.0] * 24 for _ in range(20)]
    kronos_challenger.run_once(db, forecast=fake_forecast, now=NOW)

    rows = db.execute("SELECT * FROM crypto_predictions WHERE predictor = 'kronos:small'").fetchall()
    assert len(rows) == 2
    horizons = {row["horizon"] for row in rows}
    assert horizons == {"4h", "1d"}

    for row in rows:
        assert row["instrument"] == INSTRUMENT
        assert row["mode"] == "advisory"
        assert row["reference_price"] == 100.0
        assert row["created_at"] == NOW.isoformat()
        if row["horizon"] == "4h":
            assert row["resolves_at"] == (NOW + timedelta(hours=4)).isoformat()
        else:
            assert row["resolves_at"] == (NOW + timedelta(hours=24)).isoformat()


def test_15_of_20_paths_up_gives_0_75(db, monkeypatch):
    """(b) 15 of 20 paths up gives 0.75."""
    monkeypatch.setattr(kronos_challenger, "INSTRUMENTS", (INSTRUMENT,))
    end_time = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    record_observations(db, make_candles(512, end_time=end_time, close=100.0))

    def fake_forecast(c):
        paths = []
        for i in range(20):
            val = 110.0 if i < 15 else 90.0
            paths.append([val] * 24)
        return paths

    kronos_challenger.run_once(db, forecast=fake_forecast, now=NOW)
    rows = db.execute(
        "SELECT horizon, probability_up FROM crypto_predictions WHERE predictor = 'kronos:small'"
    ).fetchall()
    probs = {row["horizon"]: row["probability_up"] for row in rows}
    assert probs["4h"] == pytest.approx(0.75)
    assert probs["1d"] == pytest.approx(0.75)


def test_unanimous_forecast_is_clamped(db, monkeypatch):
    """(c) a unanimous forecast is clamped to 0.98 and 0.02."""
    monkeypatch.setattr(kronos_challenger, "INSTRUMENTS", (INSTRUMENT,))
    end_time = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    record_observations(db, make_candles(512, end_time=end_time, close=100.0))

    # All paths up -> clamped to 0.98
    fake_forecast_up = lambda c: [[120.0] * 24 for _ in range(20)]
    kronos_challenger.run_once(db, forecast=fake_forecast_up, now=NOW)

    rows_up = db.execute(
        "SELECT probability_up FROM crypto_predictions WHERE predictor = 'kronos:small'"
    ).fetchall()
    assert len(rows_up) == 2
    for r in rows_up:
        assert r["probability_up"] == pytest.approx(0.98)

    # Next hour: all paths down -> clamped to 0.02
    next_hour = NOW + timedelta(hours=1)
    new_candle = make_candle(
        INSTRUMENT,
        NOW.replace(minute=0, second=0, microsecond=0),
        close=100.0,
    )
    record_observations(db, [new_candle])

    fake_forecast_down = lambda c: [[80.0] * 24 for _ in range(20)]
    kronos_challenger.run_once(db, forecast=fake_forecast_down, now=next_hour)

    rows_down = db.execute(
        "SELECT probability_up FROM crypto_predictions WHERE predictor = 'kronos:small' AND created_at = ?",
        (next_hour.isoformat(),),
    ).fetchall()
    assert len(rows_down) == 2
    for r in rows_down:
        assert r["probability_up"] == pytest.approx(0.02)


def test_fewer_than_512_candles_skips(db, monkeypatch):
    """(d) fewer than 512 candles skips the instrument."""
    monkeypatch.setattr(kronos_challenger, "INSTRUMENTS", (INSTRUMENT,))
    end_time = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    record_observations(db, make_candles(511, end_time=end_time, close=100.0))

    called = []
    fake_forecast = lambda c: called.append(True) or [[105.0] * 24 for _ in range(20)]

    kronos_challenger.run_once(db, forecast=fake_forecast, now=NOW)
    assert not called
    rows = db.execute("SELECT * FROM crypto_predictions WHERE predictor = 'kronos:small'").fetchall()
    assert len(rows) == 0


def test_current_unfinished_hour_is_ignored(db, monkeypatch):
    """(e) the current unfinished hour is ignored."""
    monkeypatch.setattr(kronos_challenger, "INSTRUMENTS", (INSTRUMENT,))
    # 511 completed candles + 1 candle for current hour (14:00)
    end_time = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    record_observations(db, make_candles(511, end_time=end_time, close=100.0))

    current_hour = NOW.replace(minute=0, second=0, microsecond=0)
    current_candle = make_candle(INSTRUMENT, current_hour, close=200.0)
    record_observations(db, [current_candle])

    called = []
    fake_forecast = lambda c: called.append(c) or [[105.0] * 24 for _ in range(20)]

    # At NOW (14:30), the 14:00 candle is unfinished, so only 511 completed candles exist -> skipped!
    kronos_challenger.run_once(db, forecast=fake_forecast, now=NOW)
    assert not called
    assert db.execute("SELECT COUNT(*) FROM crypto_predictions").fetchone()[0] == 0

    # Now add one earlier completed candle to reach 512 completed candles
    earlier_candle = make_candle(INSTRUMENT, end_time - timedelta(hours=511), close=100.0)
    record_observations(db, [earlier_candle])

    kronos_challenger.run_once(db, forecast=fake_forecast, now=NOW)
    assert len(called) == 1
    passed_candles = called[0]
    assert len(passed_candles) == 512
    # Verify the reference price and passed candle closes are 100.0, ignoring 200.0 from unfinished hour
    rows = db.execute("SELECT reference_price FROM crypto_predictions WHERE predictor = 'kronos:small'").fetchall()
    assert len(rows) == 2
    assert rows[0]["reference_price"] == 100.0


def test_second_run_in_same_utc_hour_records_nothing_new(db, monkeypatch):
    """(f) a second run in the same UTC hour records nothing new."""
    monkeypatch.setattr(kronos_challenger, "INSTRUMENTS", (INSTRUMENT,))
    end_time = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    record_observations(db, make_candles(512, end_time=end_time, close=100.0))

    fake_forecast = lambda c: [[105.0] * 24 for _ in range(20)]

    kronos_challenger.run_once(db, forecast=fake_forecast, now=NOW)
    rows_first = db.execute("SELECT * FROM crypto_predictions WHERE predictor = 'kronos:small'").fetchall()
    assert len(rows_first) == 2

    # Run again 15 minutes later (same UTC hour)
    kronos_challenger.run_once(db, forecast=fake_forecast, now=NOW + timedelta(minutes=15))
    rows_second = db.execute("SELECT * FROM crypto_predictions WHERE predictor = 'kronos:small'").fetchall()
    assert len(rows_second) == 2


def test_features_contain_paths_pred_vol_bps_and_median_close(db, monkeypatch):
    """(g) features has paths, pred_vol_bps, median_close."""
    monkeypatch.setattr(kronos_challenger, "INSTRUMENTS", (INSTRUMENT,))
    end_time = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    record_observations(db, make_candles(512, end_time=end_time, close=100.0))

    def fake_forecast(c):
        paths = []
        for i in range(20):
            path = [100.0 + (step + 1) * 0.1 * (i + 1) for step in range(24)]
            paths.append(path)
        return paths

    kronos_challenger.run_once(db, forecast=fake_forecast, now=NOW)
    rows = db.execute(
        "SELECT horizon, features_json FROM crypto_predictions WHERE predictor = 'kronos:small'"
    ).fetchall()
    assert len(rows) == 2

    for row in rows:
        features = json.loads(row["features_json"])
        assert "paths" in features
        assert features["paths"] == 20

        assert "pred_vol_bps" in features
        assert isinstance(features["pred_vol_bps"], float)
        assert features["pred_vol_bps"] > 0

        assert "median_close" in features
        assert isinstance(features["median_close"], float)

        if row["horizon"] == "4h":
            expected_median = float(statistics.median(100.0 + 4 * 0.1 * (i + 1) for i in range(20)))
            assert features["median_close"] == pytest.approx(expected_median)
        else:
            expected_median = float(statistics.median(100.0 + 24 * 0.1 * (i + 1) for i in range(20)))
            assert features["median_close"] == pytest.approx(expected_median)


def test_importing_module_does_not_import_torch():
    """(h) importing the module does not import torch."""
    code = (
        "import sys\n"
        "import research.kronos_challenger\n"
        "assert 'torch' not in sys.modules, 'torch was imported'\n"
        "assert not any('risk' in m or 'sender' in m or 'order' in m for m in sys.modules if 'andy_trader' in m), "
        "'forbidden andy_trader module imported'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, f"Import verification failed:\n{result.stderr}"
