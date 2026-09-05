"""Tests for multimodal feature engineering and anti-leakage invariants."""

from datetime import timedelta
from pathlib import Path
import random
from typing import Sequence

import pytest

from andy_trader.features import (
    MULTIMODAL_FEATURE_NAMES,
    SIGNAL_MAX_AGE,
    SignalObservation,
    as_of_signal_lookup,
    engineer_multimodal_features,
    load_signals_for_series,
)
from andy_trader.signals import (
    FEAR_GREED,
    FUNDING_RATE,
    LONG_RATIO,
    OPEN_INTEREST,
    Signal,
    record_signals,
)
from andy_trader.store import connect


def _prices(count: int = 50) -> list[tuple[str, float]]:
    rng = random.Random(42)
    val = 100.0
    out = []
    for i in range(count):
        iso = f"2026-08-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00"
        val *= 1.0 + (0.005 if rng.random() > 0.5 else -0.005)
        out.append((iso, val))
    return out


def test_as_of_signal_lookup_returns_latest_prior_observation() -> None:
    obs = [
        SignalObservation(FUNDING_RATE, "2026-08-01T00:00:00+00:00", 0.0001),
        SignalObservation(FUNDING_RATE, "2026-08-01T08:00:00+00:00", 0.0002),
        SignalObservation(FUNDING_RATE, "2026-08-01T16:00:00+00:00", 0.0003),
    ]

    # Exactly on observation time
    assert as_of_signal_lookup(obs, "2026-08-01T08:00:00+00:00", timedelta(hours=12)) == 0.0002

    # Between observation times
    assert as_of_signal_lookup(obs, "2026-08-01T10:00:00+00:00", timedelta(hours=12)) == 0.0002

    # Before any observation
    assert as_of_signal_lookup(obs, "2026-07-31T23:59:59+00:00", timedelta(hours=12)) is None


def test_as_of_signal_lookup_respects_staleness_limit() -> None:
    obs = [
        SignalObservation(LONG_RATIO, "2026-08-01T00:00:00+00:00", 0.55),
    ]
    # Within 2 hours
    assert as_of_signal_lookup(obs, "2026-08-01T01:30:00+00:00", timedelta(hours=2)) == 0.55

    # Exceeds 2 hours (stale)
    assert as_of_signal_lookup(obs, "2026-08-01T02:00:01+00:00", timedelta(hours=2)) is None


def test_leakage_invariant_future_signals_never_leak_into_prior_features() -> None:
    """Deliberately inject extreme future signal data and assert prior features are bit-for-bit identical."""
    history = _prices(40)
    cutoff_time = history[25][0]

    past_signals = {
        FUNDING_RATE: [
            SignalObservation(FUNDING_RATE, "2026-08-01T00:00:00+00:00", 0.0001),
            SignalObservation(FUNDING_RATE, cutoff_time, 0.0002),
        ],
        LONG_RATIO: [
            SignalObservation(LONG_RATIO, cutoff_time, 0.52),
        ],
        OPEN_INTEREST: [
            SignalObservation(OPEN_INTEREST, "2026-08-01T00:00:00+00:00", 1000.0),
            SignalObservation(OPEN_INTEREST, cutoff_time, 1050.0),
        ],
        FEAR_GREED: [
            SignalObservation(FEAR_GREED, cutoff_time, 60.0),
        ],
    }

    # Engineer baseline feature rows
    rows_clean = engineer_multimodal_features(history[:26], past_signals, lookback=12)

    # Now deliberately inject poisoned future signals with extreme values at t > cutoff_time
    future_time = history[26][0]
    far_future_time = history[35][0]
    poisoned_signals = {
        sig: list(obs_list) for sig, obs_list in past_signals.items()
    }
    poisoned_signals[FUNDING_RATE].append(
        SignalObservation(FUNDING_RATE, future_time, 0.9999)  # Massive future funding
    )
    poisoned_signals[LONG_RATIO].append(
        SignalObservation(LONG_RATIO, far_future_time, 0.99)  # Extreme future long ratio
    )
    poisoned_signals[FEAR_GREED].append(
        SignalObservation(FEAR_GREED, future_time, 1.0)
    )

    # Engineer feature rows on full history with future signals present
    rows_with_future = engineer_multimodal_features(history, poisoned_signals, lookback=12)

    # Verify that for all rows up to cutoff_time (index <= 25), values are EXACTLY identical
    assert len(rows_clean) <= len(rows_with_future)
    for clean_row, future_row in zip(rows_clean, rows_with_future[: len(rows_clean)]):
        assert clean_row.close_index == future_row.close_index
        assert clean_row.reference_time == future_row.reference_time
        assert clean_row.values == future_row.values, (
            f"Future leakage detected at index {clean_row.close_index} ({clean_row.reference_time}): "
            f"clean {clean_row.values} != with_future {future_row.values}"
        )


def test_leakage_invariant_future_prices_never_change_past_rows() -> None:
    """Changing future prices must never alter earlier feature rows."""
    history = _prices(40)
    signals = {
        FUNDING_RATE: [SignalObservation(FUNDING_RATE, history[0][0], 0.0001)],
    }
    prefix = engineer_multimodal_features(history[:30], signals, lookback=12)

    poisoned_history = list(history[:30]) + [
        (history[30 + i][0], 999999.0) for i in range(10)
    ]
    full = engineer_multimodal_features(poisoned_history, signals, lookback=12)

    assert len(full) >= len(prefix)
    for p_row, f_row in zip(prefix, full[: len(prefix)]):
        assert p_row.values == f_row.values


def test_missing_and_stale_signals_fallback_to_neutral_indicators() -> None:
    history = _prices(30)
    empty_signals: dict[str, list[SignalObservation]] = {}
    rows = engineer_multimodal_features(history, empty_signals, lookback=12)

    assert len(rows) == 18
    for row in rows:
        assert len(row.values) == len(MULTIMODAL_FEATURE_NAMES)
        # funding_rate = 0.0, funding_is_valid = 0.0
        assert row.values[6] == 0.0
        assert row.values[7] == 0.0
        # long_ratio_spread = 0.0, long_ratio_is_valid = 0.0
        assert row.values[8] == 0.0
        assert row.values[9] == 0.0
        # open_interest_momentum = 0.0, open_interest_is_valid = 0.0
        assert row.values[10] == 0.0
        assert row.values[11] == 0.0
        # fear_greed_normalized = 0.0, fear_greed_is_valid = 0.0
        assert row.values[12] == 0.0
        assert row.values[13] == 0.0


def test_load_signals_for_series_from_db(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    with connect(db_file) as conn:
        record_signals(
            conn,
            [
                Signal(
                    signal=FUNDING_RATE,
                    source="bybit",
                    instrument="BTC-USD",
                    value=0.0001,
                    observed_time="2026-08-01T00:00:00+00:00",
                ),
                Signal(
                    signal=FEAR_GREED,
                    source="alternative.me",
                    instrument=None,  # market-wide
                    value=55.0,
                    observed_time="2026-08-01T00:00:00+00:00",
                ),
                Signal(
                    signal=FUNDING_RATE,
                    source="bybit",
                    instrument="ETH-USD",  # different instrument
                    value=0.0005,
                    observed_time="2026-08-01T00:00:00+00:00",
                ),
            ],
        )
        loaded = load_signals_for_series(conn, instrument="BTC-USD")
        assert len(loaded[FUNDING_RATE]) == 1
        assert loaded[FUNDING_RATE][0].value == 0.0001
        assert len(loaded[FEAR_GREED]) == 1
        assert loaded[FEAR_GREED][0].value == 55.0
