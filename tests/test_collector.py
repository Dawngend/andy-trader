import ssl
from urllib.error import URLError

import pytest

import andy_trader.collector as collector
from andy_trader.collector import (
    CollectorError,
    FetchSettings,
    collect,
    fetch_bybit,
    fetch_coinbase,
    fetch_coingecko,
    fetch_kraken,
)

SETTINGS = FetchSettings(retries=1, backoff_seconds=0, rate_limit_seconds=0)


def test_default_fetch_budget_cannot_retry_a_hung_request_inside_one_pass() -> None:
    settings = FetchSettings()
    assert settings.retries == 1
    assert settings.timeout_seconds <= 8.0


def _http(payload):
    return lambda _url, _settings: payload


def test_tls_verification_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = []
    sleeps = []

    def fail(_request, *, timeout):
        attempts.append(timeout)
        raise URLError(ssl.SSLCertVerificationError(1, "untrusted block page"))

    monkeypatch.setattr(collector, "urlopen", fail)
    with pytest.raises(ConnectionError, match="untrusted block page"):
        collector._http_json(
            "https://example.invalid",
            FetchSettings(retries=3, backoff_seconds=1),
            sleeper=sleeps.append,
        )

    assert attempts == [8.0]
    assert sleeps == []


def test_coinbase_parses_the_low_high_open_close_ordering() -> None:
    """Coinbase returns [time, low, high, open, close, volume], not OHLCV order.

    Getting this wrong silently swaps highs and opens, which no test downstream
    would catch because the numbers stay plausible.
    """

    payload = [[1788462000, 95.0, 110.0, 100.0, 105.0, 12.5]]
    candles = fetch_coinbase("BTC-USD", "1h", SETTINGS, http=_http(payload))
    assert len(candles) == 1
    candle = candles[0]
    assert (candle.open, candle.high, candle.low, candle.close) == (100.0, 110.0, 95.0, 105.0)
    assert candle.volume == 12.5
    assert candle.venue == "coinbase"
    assert not candle.degraded


def test_coinbase_returns_nothing_for_an_unsupported_interval() -> None:
    assert fetch_coinbase("BTC-USD", "4h", SETTINGS, http=_http([])) == []


def test_coinbase_skips_malformed_rows() -> None:
    payload = [[1788462000, 95.0, 110.0, 100.0, 105.0, 12.5], "junk", [1]]
    assert len(fetch_coinbase("BTC-USD", "1h", SETTINGS, http=_http(payload))) == 1


def test_kraken_parses_its_ohlc_series() -> None:
    payload = {
        "error": [],
        "result": {
            "XXBTZUSD": [[1788462000, "100.0", "110.0", "95.0", "105.0", "104.0", "12.5", 42]],
            "last": 1788462000,
        },
    }
    candles = fetch_kraken("BTC-USD", "4h", SETTINGS, http=_http(payload))
    assert len(candles) == 1
    assert candles[0].close == 105.0
    assert candles[0].interval == "4h"


def test_kraken_raises_on_an_api_error_rather_than_returning_empty() -> None:
    payload = {"error": ["EGeneral:Invalid arguments"], "result": {}}
    with pytest.raises(ValueError, match="Kraken API error"):
        fetch_kraken("BTC-USD", "1h", SETTINGS, http=_http(payload))


def test_kraken_returns_nothing_for_an_unknown_instrument() -> None:
    assert fetch_kraken("NOTACOIN-USD", "1h", SETTINGS, http=_http({})) == []


def test_coingecko_pairs_prices_with_volumes_and_leaves_ohl_null() -> None:
    payload = {
        "prices": [[1788462000000, 80000.0], [1788465600000, 80500.0]],
        "total_volumes": [[1788462000000, 3.5e10], [1788465600000, 3.6e10]],
    }
    candles = fetch_coingecko("BTC-USD", "1h", SETTINGS, http=_http(payload))
    assert len(candles) == 2
    first = candles[0]
    assert first.close == 80000.0
    assert first.volume == pytest.approx(3.5e10)
    # This venue reports close only. Nulls here are a real partial observation,
    # not a failure, so degraded stays False.
    assert (first.open, first.high, first.low) == (None, None, None)
    assert not first.degraded


def test_coingecko_tolerates_a_missing_volume_point() -> None:
    payload = {"prices": [[1788462000000, 80000.0]], "total_volumes": []}
    assert fetch_coingecko("BTC-USD", "1h", SETTINGS, http=_http(payload))[0].volume is None


def test_coingecko_folds_the_request_time_quote_into_its_hour() -> None:
    payload = {
        "prices": [[1788462000000, 80000.0], [1788463830000, 80100.0]],
        "total_volumes": [],
    }

    candles = fetch_coingecko("BTC-USD", "1h", SETTINGS, http=_http(payload))

    assert len(candles) == 1
    assert candles[0].open_time == "2026-09-03T19:00:00+00:00"
    assert candles[0].close == 80100.0


def test_coingecko_returns_nothing_for_an_unmapped_instrument() -> None:
    assert fetch_coingecko("NOTACOIN-USD", "1h", SETTINGS, http=_http({})) == []


def test_bybit_parses_a_full_ohlcv_bar_from_string_fields() -> None:
    payload = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "spot",
            "symbol": "BTCUSDT",
            "list": [
                ["1788487200000", "80968.01", "81025.39", "80669.45", "80831.99", "433.7", "3.5e7"],
            ],
        },
    }
    candles = fetch_bybit("BTC-USD", "1h", SETTINGS, http=_http(payload))
    assert len(candles) == 1
    candle = candles[0]
    assert (candle.open, candle.high, candle.low, candle.close) == (
        80968.01, 81025.39, 80669.45, 80831.99
    )
    assert candle.volume == pytest.approx(433.7)
    assert candle.venue == "bybit"
    assert not candle.degraded


def test_bybit_supports_four_hour_bars_which_coinbase_does_not() -> None:
    payload = {"retCode": 0, "result": {"list": [["1788487200000", "1", "2", "0.5", "1.5", "10"]]}}
    assert len(fetch_bybit("BTC-USD", "4h", SETTINGS, http=_http(payload))) == 1
    assert fetch_coinbase("BTC-USD", "4h", SETTINGS, http=_http([])) == []


def test_bybit_raises_on_a_non_zero_return_code() -> None:
    payload = {"retCode": 10001, "retMsg": "params error", "result": {}}
    with pytest.raises(ValueError, match="Bybit API error"):
        fetch_bybit("BTC-USD", "1h", SETTINGS, http=_http(payload))


def test_bybit_returns_nothing_for_an_unmapped_instrument() -> None:
    assert fetch_bybit("NOTACOIN-USD", "1h", SETTINGS, http=_http({})) == []


def test_collect_records_a_source_failure_as_a_degraded_row() -> None:
    def boom(_instrument, _interval, _settings):
        raise ConnectionError("URLError: certificate verify failed")

    candles, problems = collect(
        instruments=("BTC-USD",),
        intervals=("1h",),
        venues=("flaky",),
        settings=SETTINGS,
        fetchers={"flaky": boom},
        sleeper=lambda _s: None,
    )
    assert len(candles) == 1
    assert candles[0].degraded
    assert "certificate verify failed" in candles[0].degraded_reason
    assert candles[0].close is None
    assert problems[0]["venue"] == "flaky"


def test_collect_keeps_going_after_one_venue_fails() -> None:
    def boom(_i, _iv, _s):
        raise ConnectionError("down")

    def fine(instrument, interval, _s):
        return fetch_coingecko(
            instrument, interval, SETTINGS,
            http=_http({"prices": [[1788462000000, 1.0]], "total_volumes": []}),
        )

    candles, problems = collect(
        instruments=("BTC-USD",),
        intervals=("1h",),
        venues=("broken", "working"),
        settings=SETTINGS,
        fetchers={"broken": boom, "working": fine},
        sleeper=lambda _s: None,
    )
    assert len(problems) == 1
    assert sum(1 for c in candles if not c.degraded) == 1


def test_collect_rejects_an_unknown_venue() -> None:
    with pytest.raises(CollectorError, match="Unknown venue"):
        collect(
            instruments=("BTC-USD",),
            intervals=("1h",),
            venues=("nasdaq",),
            settings=SETTINGS,
            fetchers={"coingecko": fetch_coingecko},
            sleeper=lambda _s: None,
        )
