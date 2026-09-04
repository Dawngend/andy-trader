from pathlib import Path

import pytest

from andy_trader.signals import (
    FEAR_GREED,
    FUNDING_RATE,
    LONG_RATIO,
    OPEN_INTEREST,
    Signal,
    SignalError,
    collect_signals,
    latest_signal,
    parse_fear_greed,
    parse_funding,
    parse_long_ratio,
    parse_open_interest,
    record_signals,
)
from andy_trader.store import connect


def _bybit(rows: list[dict]) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": {"list": rows}}


def test_an_unknown_signal_name_is_refused() -> None:
    """A typo must crash rather than silently create an empty series nobody notices."""

    with pytest.raises(SignalError, match="Unknown signal"):
        Signal(signal="fear_gred", source="x", value=1.0, observed_time="2026-09-04T00:00:00+00:00")


def test_fear_greed_is_market_wide_so_has_no_instrument() -> None:
    payload = {"data": [{"value": "74", "value_classification": "Greed", "timestamp": "1788480000"}]}
    signals = parse_fear_greed(payload)
    assert len(signals) == 1
    assert signals[0].instrument is None
    assert signals[0].value == 74.0
    assert signals[0].metadata["classification"] == "Greed"


def test_funding_rate_keeps_its_sign() -> None:
    """Positive funding means longs pay shorts. Losing the sign inverts the reading."""

    positive = parse_funding(
        _bybit([{"fundingRate": "0.00006921", "fundingRateTimestamp": "1788508800000"}]), "BTC-USD"
    )
    negative = parse_funding(
        _bybit([{"fundingRate": "-0.00012", "fundingRateTimestamp": "1788508800000"}]), "BTC-USD"
    )
    assert positive[0].value > 0
    assert negative[0].value < 0
    assert positive[0].instrument == "BTC-USD"


def test_open_interest_parses() -> None:
    signals = parse_open_interest(
        _bybit([{"openInterest": "56656.254", "timestamp": "1788530400000"}]), "BTC-USD"
    )
    assert signals[0].value == pytest.approx(56656.254)
    assert signals[0].signal == OPEN_INTEREST


def test_long_ratio_records_the_sell_side_as_metadata() -> None:
    signals = parse_long_ratio(
        _bybit([{"buyRatio": "0.5318", "sellRatio": "0.4682", "timestamp": "1788530400000"}]),
        "BTC-USD",
    )
    assert signals[0].value == pytest.approx(0.5318)
    assert signals[0].metadata["sell_ratio"] == "0.4682"


def test_a_bybit_error_code_raises_rather_than_returning_empty() -> None:
    with pytest.raises(ValueError, match="Bybit API error"):
        parse_funding({"retCode": 10001, "retMsg": "bad", "result": {}}, "BTC-USD")


def test_repeat_signal_bumps_times_seen_without_duplicating(tmp_path: Path) -> None:
    signal = Signal(FEAR_GREED, "alternative.me", 74.0, "2026-09-04T00:00:00+00:00")
    with connect(tmp_path / "c.db") as connection:
        record_signals(connection, [signal])
        inserted, seen = record_signals(connection, [signal])
        assert (inserted, seen) == (0, 1)
        row = connection.execute("SELECT times_seen FROM crypto_signals").fetchone()
        assert row["times_seen"] == 2


def test_a_changed_value_lands_as_a_new_row(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_signals(connection, [Signal(FEAR_GREED, "alternative.me", 74.0, "2026-09-04T00:00:00+00:00")])
        record_signals(connection, [Signal(FEAR_GREED, "alternative.me", 51.0, "2026-09-04T00:00:00+00:00")])
        count = connection.execute("SELECT COUNT(*) AS n FROM crypto_signals").fetchone()
        assert count["n"] == 2


def test_latest_signal_ignores_degraded_rows(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_signals(connection, [
            Signal(FEAR_GREED, "alternative.me", 40.0, "2026-09-04T00:00:00+00:00"),
            Signal(FEAR_GREED, "alternative.me", None, "2026-09-04T05:00:00+00:00",
                   degraded=True, degraded_reason="unreachable"),
        ])
        assert latest_signal(connection, FEAR_GREED) == 40.0


def test_latest_signal_separates_market_wide_from_per_instrument(tmp_path: Path) -> None:
    with connect(tmp_path / "c.db") as connection:
        record_signals(connection, [
            Signal(FEAR_GREED, "alternative.me", 74.0, "2026-09-04T00:00:00+00:00"),
            Signal(LONG_RATIO, "bybit", 0.53, "2026-09-04T00:00:00+00:00", instrument="BTC-USD"),
        ])
        assert latest_signal(connection, FEAR_GREED) == 74.0
        assert latest_signal(connection, FEAR_GREED, instrument="BTC-USD") is None
        assert latest_signal(connection, LONG_RATIO, instrument="BTC-USD") == pytest.approx(0.53)


def test_latest_signal_respects_a_point_in_time_cutoff(tmp_path: Path) -> None:
    """A backtest must be able to ask what was knowable then, not what is known now."""

    with connect(tmp_path / "c.db") as connection:
        record_signals(connection, [
            Signal(FEAR_GREED, "alternative.me", 20.0, "2026-09-01T00:00:00+00:00"),
            Signal(FEAR_GREED, "alternative.me", 90.0, "2026-09-04T00:00:00+00:00"),
        ])
        assert latest_signal(connection, FEAR_GREED) == 90.0
        assert latest_signal(connection, FEAR_GREED, at_or_before="2026-09-02T00:00:00+00:00") == 20.0


def test_collect_records_a_failure_as_a_degraded_signal() -> None:
    def boom(_url: str) -> object:
        raise ConnectionError("timed out")

    signals, problems = collect_signals(("BTC-USD",), http=boom)
    assert all(s.degraded for s in signals)
    assert all(s.value is None for s in signals)
    # Fear and greed plus three per-instrument signals.
    assert len(signals) == 4
    assert len(problems) == 4


def test_collect_skips_an_unmapped_instrument_without_failing() -> None:
    def http(_url: str) -> object:
        return {"data": [{"value": "50", "value_classification": "Neutral", "timestamp": "1788480000"}]}

    signals, problems = collect_signals(("NOTACOIN-USD",), http=http)
    assert problems == []
    assert [s.signal for s in signals] == [FEAR_GREED]
