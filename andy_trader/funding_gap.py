"""Collect and report cross-exchange perpetual funding rate gaps.

Paper research only: read-only logger of public funding rates across Binance,
Bybit and OKX. No keys, no orders, no risk code.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Callable, Mapping, Sequence
import urllib.error
import urllib.request

INSTRUMENTS = (
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "XRP-USD",
    "DOGE-USD",
    "ADA-USD",
    "AVAX-USD",
    "LINK-USD",
)

HOURS_PER_YEAR = 8760.0
DEFAULT_TIMEOUT_SECONDS = 10.0


def instrument_to_symbols(instrument: str) -> tuple[str, str, str]:
    """Map an instrument (e.g. BTC-USD) to (binance_symbol, bybit_symbol, okx_inst_id)."""
    coin = instrument.split("-")[0]
    return f"{coin}USDT", f"{coin}USDT", f"{coin}-USDT-SWAP"


def default_fetch(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "andy-trader/1.0"})
    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_binance(payload: object) -> tuple[float, float]:
    """Parse Binance premiumIndex payload into (funding_rate, interval_hours)."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"Unexpected Binance payload {type(payload).__name__}")
    rate_val = payload.get("lastFundingRate")
    if rate_val is None or str(rate_val).strip() == "":
        raise ValueError("Binance payload missing lastFundingRate")
    rate = float(rate_val)
    interval_val = payload.get("fundingIntervalHours")
    interval = float(interval_val) if interval_val is not None and str(interval_val).strip() != "" else 8.0
    if interval <= 0:
        interval = 8.0
    return rate, interval


def parse_bybit(payload: object) -> tuple[float, float]:
    """Parse Bybit tickers payload into (funding_rate, interval_hours)."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"Unexpected Bybit payload {type(payload).__name__}")
    if "retCode" in payload and payload.get("retCode") != 0:
        raise ValueError(f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}")
    item: object = payload
    if "result" in payload and isinstance(payload["result"], Mapping):
        item = payload["result"]
    if isinstance(item, Mapping) and "list" in item and isinstance(item["list"], list):
        if not item["list"]:
            raise ValueError("Bybit list is empty")
        item = item["list"][0]
    if not isinstance(item, Mapping) or "fundingRate" not in item:
        raise ValueError("Bybit item missing fundingRate")
    rate_val = item.get("fundingRate")
    if rate_val is None or str(rate_val).strip() == "":
        raise ValueError("Bybit fundingRate is empty")
    rate = float(rate_val)
    interval_val = item.get("fundingIntervalHour")
    interval = float(interval_val) if interval_val is not None and str(interval_val).strip() != "" else 8.0
    if interval <= 0:
        interval = 8.0
    return rate, interval


def parse_okx(payload: object) -> tuple[float, float]:
    """Parse OKX funding-rate payload into (funding_rate, interval_hours)."""
    if not isinstance(payload, Mapping):
        raise ValueError(f"Unexpected OKX payload {type(payload).__name__}")
    if "code" in payload and str(payload.get("code")) != "0":
        raise ValueError(f"OKX API error {payload.get('code')}: {payload.get('msg')}")
    item: object = payload
    if "data" in payload and isinstance(payload["data"], list):
        if not payload["data"]:
            raise ValueError("OKX data is empty")
        item = payload["data"][0]
    if not isinstance(item, Mapping) or "fundingRate" not in item:
        raise ValueError("OKX item missing fundingRate")
    rate_val = item.get("fundingRate")
    if rate_val is None or str(rate_val).strip() == "":
        raise ValueError("OKX fundingRate is empty")
    rate = float(rate_val)

    interval = 8.0
    ft = item.get("fundingTime")
    next_ft = item.get("nextFundingTime")
    if ft is not None and next_ft is not None:
        try:
            diff = float(next_ft) - float(ft)
            if diff > 100_000:
                interval = diff / 3_600_000.0
            elif diff > 100:
                interval = diff / 3600.0
            elif diff > 0:
                interval = diff
        except (ValueError, TypeError):
            interval = 8.0
    if interval <= 0:
        interval = 8.0
    return rate, interval


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS funding_gap_observations (
            id INTEGER PRIMARY KEY,
            observed_at TEXT,
            instrument TEXT,
            venue TEXT,
            funding_rate REAL,
            interval_hours REAL,
            annualized REAL
        )
        """
    )


def _to_json(payload: object) -> object:
    if isinstance(payload, (str, bytes, bytearray)):
        return json.loads(payload)
    return payload


def run_once(
    connection: sqlite3.Connection,
    *,
    fetch: Callable[[str], object] = default_fetch,
    now: datetime | str | None = None,
) -> tuple[int, int]:
    """Perform one collection pass over all instruments and venues."""
    initialize_database(connection)
    if now is None:
        observed_at = datetime.now(UTC).isoformat()
    elif isinstance(now, datetime):
        observed_at = now.isoformat()
    else:
        observed_at = str(now)

    rows_written = 0
    venues_failed = 0

    for instrument in INSTRUMENTS:
        binance_sym, bybit_sym, okx_sym = instrument_to_symbols(instrument)
        venues = (
            ("binance", f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={binance_sym}", parse_binance),
            ("bybit", f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={bybit_sym}", parse_bybit),
            ("okx", f"https://www.okx.com/api/v5/public/funding-rate?instId={okx_sym}", parse_okx),
        )
        for venue, url, parser in venues:
            try:
                raw = _to_json(fetch(url))
                rate, interval = parser(raw)
                annualized = rate * (HOURS_PER_YEAR / interval)
                connection.execute(
                    """
                    INSERT INTO funding_gap_observations
                    (observed_at, instrument, venue, funding_rate, interval_hours, annualized)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (observed_at, instrument, venue, rate, interval, annualized),
                )
                rows_written += 1
            except Exception:
                venues_failed += 1

    connection.commit()
    return rows_written, venues_failed


def compute_instrument_report(
    connection: sqlite3.Connection,
    instrument: str,
    threshold: float = 0.15,
) -> dict[str, object]:
    """Calculate gap statistics for one instrument."""
    initialize_database(connection)
    rows = connection.execute(
        """
        SELECT observed_at, venue, annualized
        FROM funding_gap_observations
        WHERE instrument = ?
        ORDER BY observed_at ASC, id ASC
        """,
        (instrument,),
    ).fetchall()

    if not rows:
        return {
            "instrument": instrument,
            "latest_venues": {},
            "latest_gap": 0.0,
            "snapshots_above_15": 0,
            "longest_consecutive_above_15": 0,
        }

    snapshots: dict[str, dict[str, float]] = {}
    for row in rows:
        obs_at = row[0]
        venue = row[1]
        ann = float(row[2])
        if obs_at not in snapshots:
            snapshots[obs_at] = {}
        snapshots[obs_at][venue] = ann

    count_above = 0
    current_run = 0
    longest_run = 0

    for obs_at, venues in snapshots.items():
        if len(venues) >= 2:
            gap = max(venues.values()) - min(venues.values())
        else:
            gap = 0.0
        if gap > threshold:
            count_above += 1
            current_run += 1
            if current_run > longest_run:
                longest_run = current_run
        else:
            current_run = 0

    latest_obs_at = list(snapshots.keys())[-1]
    latest_venues = dict(snapshots[latest_obs_at])
    if len(latest_venues) < 2:
        # Fill missing venues from their most recent observation
        for row in reversed(rows):
            v, a = row[1], float(row[2])
            if v not in latest_venues:
                latest_venues[v] = a

    if len(latest_venues) >= 2:
        latest_gap = max(latest_venues.values()) - min(latest_venues.values())
    else:
        latest_gap = 0.0

    return {
        "instrument": instrument,
        "latest_venues": latest_venues,
        "latest_gap": latest_gap,
        "snapshots_above_15": count_above,
        "longest_consecutive_above_15": longest_run,
    }


def format_report(connection: sqlite3.Connection, threshold: float = 0.15) -> str:
    """Format stored funding gaps into plain readable words."""
    initialize_database(connection)
    count_row = connection.execute(
        "SELECT COUNT(DISTINCT observed_at) FROM funding_gap_observations"
    ).fetchone()
    total_snapshots = int(count_row[0]) if count_row and count_row[0] is not None else 0

    lines = [f"Snapshots stored: {total_snapshots}"]

    seen_rows = connection.execute(
        "SELECT DISTINCT instrument FROM funding_gap_observations ORDER BY instrument"
    ).fetchall()
    seen = [r[0] for r in seen_rows]
    instruments_to_show = [i for i in INSTRUMENTS if i in seen]
    for i in seen:
        if i not in instruments_to_show:
            instruments_to_show.append(i)
    if not instruments_to_show:
        instruments_to_show = list(INSTRUMENTS)

    for instrument in instruments_to_show:
        rep = compute_instrument_report(connection, instrument, threshold=threshold)
        lines.append("")
        lines.append(f"{instrument}:")
        latest_venues = rep["latest_venues"]
        if latest_venues:
            for venue in sorted(latest_venues):
                rate = latest_venues[venue]
                lines.append(f"  {venue}: {rate:+.4f} ({rate * 100:+.2f}% annualized)")
            latest_gap = rep["latest_gap"]
            lines.append(f"  Latest gap: {latest_gap:.4f} ({latest_gap * 100:.2f}% annualized)")
        else:
            lines.append("  No observations recorded")
            lines.append("  Latest gap: 0.0000 (0.00% annualized)")
        lines.append(f"  Snapshots above 15%: {rep['snapshots_above_15']}")
        lines.append(f"  Longest run above 15%: {rep['longest_consecutive_above_15']}")

    return "\n".join(lines)


def print_report(connection: sqlite3.Connection, threshold: float = 0.15) -> None:
    print(format_report(connection, threshold=threshold))
