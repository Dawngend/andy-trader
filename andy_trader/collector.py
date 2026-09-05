"""Fetch OHLCV candles from keyless public venues into the append-only store."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import ssl
import sys
import time
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.store import Candle, connect, default_database_path, record_observations

USER_AGENT = "andy-trader-collector/1.0 (personal research)"

DEFAULT_INSTRUMENTS = ("BTC-USD", "ETH-USD")
DEFAULT_INTERVALS = ("1h", "4h")

# Coinbase Exchange granularities are a fixed set of seconds and 4h is not in
# it; the neighbouring option is 6h. Rather than silently substituting 6h data
# into a 4h series, Coinbase simply does not offer 4h here and Kraken covers it.
# Aggregating 1h into 4h is a derived value, not an observation, so it belongs
# in a feature layer and not in this table.
_COINBASE_GRANULARITY = {"1h": 3600, "1d": 86400}
_KRAKEN_INTERVAL_MINUTES = {"1h": 60, "4h": 240, "1d": 1440}
_KRAKEN_PAIRS = {
    "BTC-USD": "XBTUSD",
    "ETH-USD": "ETHUSD",
    "SOL-USD": "SOLUSD",
    "XRP-USD": "XRPUSD",
    "DOGE-USD": "XDGUSD",
    "ADA-USD": "ADAUSD",
    "AVAX-USD": "AVAXUSD",
    "LINK-USD": "LINKUSD",
}

# Exchange domains are INTERMITTENTLY blocked on Dawn's PLDT connection. On
# 2026-09-04 api.kraken.com and api.bybit.com resolved to 10.158.4.12, a
# block-page box, and presented a "CN=blocking-page-authority" certificate;
# twenty minutes later on the same machine they resolved to real IPs and
# returned 200. So the resolver is inconsistent rather than the venue being
# dead, and a run can fail for reasons that have nothing to do with the code.
# This is exactly why every fetch failure becomes a degraded row instead of an
# exception: over a long collection window some passes will simply lose DNS,
# and the record has to show that honestly rather than silently thinning out.
# CoinGecko has never been blocked in testing and is the reliable fallback.
_COINGECKO_IDS = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    # Higher-volatility instruments, added 2026-09-04, for a real mechanism
    # rather than a hunch: trading costs are proportional to position size in
    # basis points, while the move being captured is not. A 30 bps round trip is
    # trivial against a 3% hourly move and impossible against a 0.3% one, which
    # is exactly how BTC hourly turned +4.52% gross into -22.59% net. Whether
    # that advantage survives the wider spreads and worse fills on thinner books
    # is the open question these instruments exist to answer. The calibration
    # harness settles it; do not assume either way in the meantime.
    "SOL-USD": "solana",
    "XRP-USD": "ripple",
    "DOGE-USD": "dogecoin",
    "ADA-USD": "cardano",
    "AVAX-USD": "avalanche-2",
    "LINK-USD": "chainlink",
}
# market_chart returns hourly points for a 2 to 90 day window, which is the only
# CoinGecko endpoint that yields a clean 1h series. Its /ohlc endpoint picks
# granularity for you and cannot be pinned to an hour.
_COINGECKO_DAYS = {"1h": 7, "1d": 90}

# Bybit v5 spot klines. Keyless, and the only venue here that returns a full
# OHLCV bar at both 1h and 4h, which is why it leads the default order.
_BYBIT_INTERVALS = {"1h": "60", "4h": "240", "1d": "D"}
_BYBIT_SYMBOLS = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
    "SOL-USD": "SOLUSDT",
    "XRP-USD": "XRPUSDT",
    "DOGE-USD": "DOGEUSDT",
    "ADA-USD": "ADAUSDT",
    "AVAX-USD": "AVAXUSDT",
    "LINK-USD": "LINKUSDT",
}


class CollectorError(RuntimeError):
    """Raised for configuration problems, never for a source being unreachable."""


@dataclass(frozen=True)
class FetchSettings:
    timeout_seconds: float = 20.0
    retries: int = 3
    backoff_seconds: float = 1.0
    rate_limit_seconds: float = 1.0


def _http_json(url: str, settings: FetchSettings, sleeper: Callable[[float], None] = time.sleep) -> object:
    last_error: Exception | None = None
    for attempt in range(1, settings.retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(request, timeout=settings.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
            # The 2026-09-05 PLDT block-page incident presented an untrusted
            # certificate for every exchange request. Retrying the same bad
            # chain three times cannot repair trust and stretched one cycle by
            # more than a minute, so fail this source immediately and let the
            # existing degraded-row and fallback paths preserve the evidence.
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                break
            if attempt < settings.retries:
                sleeper(settings.backoff_seconds * attempt)
    raise ConnectionError(f"{type(last_error).__name__}: {last_error}")


def fetch_coinbase(
    instrument: str,
    interval: str,
    settings: FetchSettings,
    *,
    http: Callable[[str, FetchSettings], object] | None = None,
) -> list[Candle]:
    granularity = _COINBASE_GRANULARITY.get(interval)
    if granularity is None:
        return []
    url = (
        f"https://api.exchange.coinbase.com/products/{instrument}/candles"
        f"?granularity={granularity}"
    )
    getter = http or (lambda u, s: _http_json(u, s))
    payload = getter(url, settings)
    if not isinstance(payload, list):
        raise ValueError(f"unexpected Coinbase payload type {type(payload).__name__}")
    candles: list[Candle] = []
    for row in payload:
        # Coinbase returns [time, low, high, open, close, volume]. The ordering
        # is low/high before open/close, which is not the usual OHLCV order and
        # is a standing trap when reading this endpoint.
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        ts, low, high, open_, close, volume = row[:6]
        candles.append(
            Candle(
                instrument=instrument,
                venue="coinbase",
                interval=interval,
                open_time=datetime.fromtimestamp(int(ts), UTC).isoformat(),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
            )
        )
    return candles


def fetch_kraken(
    instrument: str,
    interval: str,
    settings: FetchSettings,
    *,
    http: Callable[[str, FetchSettings], object] | None = None,
) -> list[Candle]:
    minutes = _KRAKEN_INTERVAL_MINUTES.get(interval)
    pair = _KRAKEN_PAIRS.get(instrument)
    if minutes is None or pair is None:
        return []
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={minutes}"
    getter = http or (lambda u, s: _http_json(u, s))
    payload = getter(url, settings)
    if not isinstance(payload, Mapping):
        raise ValueError(f"unexpected Kraken payload type {type(payload).__name__}")
    errors = payload.get("error") or []
    if errors:
        raise ValueError(f"Kraken API error: {errors}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Kraken payload missing result")
    series = next((v for k, v in result.items() if k != "last"), None)
    if not isinstance(series, list):
        raise ValueError("Kraken result contained no OHLC series")
    candles: list[Candle] = []
    for row in series:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        ts, open_, high, low, close, _vwap, volume = row[:7]
        candles.append(
            Candle(
                instrument=instrument,
                venue="kraken",
                interval=interval,
                open_time=datetime.fromtimestamp(int(ts), UTC).isoformat(),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
            )
        )
    return candles


def fetch_coingecko(
    instrument: str,
    interval: str,
    settings: FetchSettings,
    *,
    http: Callable[[str, FetchSettings], object] | None = None,
) -> list[Candle]:
    """Hourly close and volume from CoinGecko's market_chart endpoint.

    This venue reports close and volume only. `open`, `high` and `low` are left
    NULL, and that is a real partial observation rather than a degraded one: the
    source succeeded and gave us everything it has. Any feature that needs a
    true high or low must filter these rows out explicitly instead of assuming
    every non-degraded row carries a full bar.
    """

    coin_id = _COINGECKO_IDS.get(instrument)
    days = _COINGECKO_DAYS.get(interval)
    if coin_id is None or days is None:
        return []
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        f"?vs_currency=usd&days={days}"
    )
    getter = http or (lambda u, s: _http_json(u, s))
    payload = getter(url, settings)
    if not isinstance(payload, Mapping):
        raise ValueError(f"unexpected CoinGecko payload type {type(payload).__name__}")
    prices = payload.get("prices")
    if not isinstance(prices, list):
        raise ValueError("CoinGecko payload missing prices")
    volumes = {
        int(point[0]): float(point[1])
        for point in payload.get("total_volumes") or []
        if isinstance(point, (list, tuple)) and len(point) >= 2
    }
    candles_by_open_time: dict[str, Candle] = {}
    for point in prices:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        stamp_ms, close = int(point[0]), float(point[1])
        observed_at = datetime.fromtimestamp(stamp_ms / 1000, UTC)
        open_time = (
            observed_at.replace(minute=0, second=0, microsecond=0)
            if interval == "1h"
            else observed_at.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        # market_chart appends a request-time spot quote after its regular
        # grid. The 2026-09-05 fallback run exposed 01:30 beside the 01:00
        # point. Both belong to the same hourly bucket, and the later quote is
        # the best live close; keeping both would turn one hour into two bars.
        candles_by_open_time[open_time.isoformat()] = Candle(
            instrument=instrument,
            venue="coingecko",
            interval=interval,
            open_time=open_time.isoformat(),
            open=None,
            high=None,
            low=None,
            close=close,
            volume=volumes.get(stamp_ms),
        )
    return list(candles_by_open_time.values())


def fetch_bybit(
    instrument: str,
    interval: str,
    settings: FetchSettings,
    *,
    http: Callable[[str, FetchSettings], object] | None = None,
) -> list[Candle]:
    """Full OHLCV bars from Bybit v5 spot klines. Keyless, no account needed.

    Note this is USDT-quoted, not USD. BTCUSDT is not the same instrument as
    BTC-USD to a purist, and the basis between them is small but real. Rows are
    stored under the requested instrument name with venue="bybit" so the
    difference stays visible rather than being silently merged into a
    cross-venue consensus price.
    """

    symbol = _BYBIT_SYMBOLS.get(instrument)
    bybit_interval = _BYBIT_INTERVALS.get(interval)
    if symbol is None or bybit_interval is None:
        return []
    url = (
        "https://api.bybit.com/v5/market/kline"
        f"?category=spot&symbol={symbol}&interval={bybit_interval}&limit=200"
    )
    getter = http or (lambda u, s: _http_json(u, s))
    payload = getter(url, settings)
    if not isinstance(payload, Mapping):
        raise ValueError(f"unexpected Bybit payload type {type(payload).__name__}")
    if payload.get("retCode") != 0:
        raise ValueError(f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Bybit payload missing result")
    rows = result.get("list")
    if not isinstance(rows, list):
        raise ValueError("Bybit result contained no kline list")
    candles: list[Candle] = []
    for row in rows:
        # [startTime_ms, open, high, low, close, volume, turnover], all strings,
        # newest first. The ordering does not matter to an append-only store.
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        stamp, open_, high, low, close, volume = row[:6]
        candles.append(
            Candle(
                instrument=instrument,
                venue="bybit",
                interval=interval,
                open_time=datetime.fromtimestamp(int(stamp) / 1000, UTC).isoformat(),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
            )
        )
    return candles


VENUES: Mapping[str, Callable[..., list[Candle]]] = {
    "bybit": fetch_bybit,
    "coingecko": fetch_coingecko,
    "coinbase": fetch_coinbase,
    "kraken": fetch_kraken,
}
# Bybit first because it is the only venue returning a full OHLCV bar at both 1h
# and 4h. CoinGecko second as the fallback that has never been DNS-blocked in
# testing, so a pass where the exchange is unreachable still collects something.
# Coinbase and Kraken stay registered and tested but out of the default, since
# adding venues multiplies requests without adding much beyond a consensus check.
DEFAULT_VENUES = ("bybit", "coingecko")


def collect(
    *,
    instruments: Sequence[str],
    intervals: Sequence[str],
    venues: Sequence[str],
    settings: FetchSettings,
    fetchers: Mapping[str, Callable[..., list[Candle]]] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[list[Candle], list[dict[str, str]]]:
    """Gather candles from every venue, recording failures as degraded rows.

    A source that cannot be reached produces a degraded Candle with null prices
    and a named reason, never a skipped row and never an invented value. A gap
    in the data is itself a fact about the market that we observed, and losing
    it would make the backtest quietly optimistic later.
    """

    registry = fetchers or VENUES
    collected: list[Candle] = []
    problems: list[dict[str, str]] = []
    first = True
    for venue in venues:
        fetcher = registry.get(venue)
        if fetcher is None:
            raise CollectorError(f"Unknown venue {venue!r}; known: {', '.join(sorted(registry))}")
        for instrument in instruments:
            for interval in intervals:
                if not first and settings.rate_limit_seconds:
                    sleeper(settings.rate_limit_seconds)
                first = False
                try:
                    rows = fetcher(instrument, interval, settings)
                except Exception as exc:  # noqa: BLE001 - any failure becomes a degraded row
                    reason = f"{type(exc).__name__}: {exc}"[:400]
                    problems.append(
                        {
                            "venue": venue,
                            "instrument": instrument,
                            "interval": interval,
                            "reason": reason,
                        }
                    )
                    collected.append(
                        Candle(
                            instrument=instrument,
                            venue=venue,
                            interval=interval,
                            open_time=datetime.now(UTC).isoformat(),
                            open=None,
                            high=None,
                            low=None,
                            close=None,
                            volume=None,
                            degraded=True,
                            degraded_reason=reason,
                        )
                    )
                    continue
                collected.extend(rows)
    return collected, problems


def _settings_from_env(environ: Mapping[str, str]) -> FetchSettings:
    def _num(key: str, default: float) -> float:
        raw = environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise CollectorError(f"{key} must be a number, got {raw!r}") from exc

    return FetchSettings(
        timeout_seconds=_num("CRYPTO_COLLECTOR_TIMEOUT_SECONDS", 20.0),
        retries=int(_num("CRYPTO_COLLECTOR_RETRIES", 3)),
        backoff_seconds=_num("CRYPTO_COLLECTOR_BACKOFF_SECONDS", 1.0),
        rate_limit_seconds=_num("CRYPTO_COLLECTOR_RATE_LIMIT_SECONDS", 1.0),
    )


def _csv(environ: Mapping[str, str], key: str, default: Sequence[str]) -> tuple[str, ...]:
    raw = environ.get(key, "").strip()
    if not raw:
        return tuple(default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", help="Comma-separated, e.g. BTC-USD,ETH-USD")
    parser.add_argument("--intervals", help="Comma-separated, e.g. 1h,4h")
    parser.add_argument("--venues", help="Comma-separated, e.g. coinbase,kraken")
    parser.add_argument("--database", help="Override CRYPTO_DB_PATH")
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    environ = os.environ

    instruments = tuple(args.instruments.split(",")) if args.instruments else _csv(
        environ, "CRYPTO_INSTRUMENTS", DEFAULT_INSTRUMENTS
    )
    intervals = tuple(args.intervals.split(",")) if args.intervals else _csv(
        environ, "CRYPTO_INTERVALS", DEFAULT_INTERVALS
    )
    venues = tuple(args.venues.split(",")) if args.venues else _csv(
        environ, "CRYPTO_VENUES", DEFAULT_VENUES
    )
    database_path = Path(args.database) if args.database else default_database_path(environ)

    candles, problems = collect(
        instruments=instruments,
        intervals=intervals,
        venues=venues,
        settings=_settings_from_env(environ),
    )
    with connect(database_path) as connection:
        inserted, seen = record_observations(connection, candles)

    print(f"observed {seen} candles, {inserted} new, into {database_path}")
    if problems:
        print(f"{len(problems)} source failure(s) recorded as degraded rows:")
        for problem in problems:
            print(f"  {problem['venue']} {problem['instrument']} {problem['interval']}: {problem['reason']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
