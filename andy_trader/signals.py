"""Collect keyless market-positioning and sentiment signals into an append-only store.

This is the "what is everyone else doing" layer. It deliberately does **not**
try to track individual whale wallets and copy them, for three reasons that no
amount of engineering fixes:

1. **Latency.** An on-chain transfer is visible only after it settles, by which
   point the price has already moved. You would be copying into a worse fill.
2. **Ambiguity.** A large transfer to an exchange might be a sale, a hedge,
   collateral posting, or a custody move. The transfer does not carry intent,
   so any directional reading of it is a guess dressed as data.
3. **Survivorship.** "Top whales" are selected for having already won. Copying
   past winners is the same error as reading a trading screenshot: you are shown
   the wallet that succeeded and never the ones that did not.

Aggregate positioning avoids all three. Funding rate, open interest and the
long/short account ratio are published continuously, describe the whole book
rather than one actor's intent, and need no key. Crowding is then a *testable*
hypothesis rather than a story: extreme long crowding plausibly precedes
drawdowns, and the calibration harness is what decides whether it actually does.

Nothing here predicts. It records. Predictors that consume these signals are
scored against the same baselines as everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from typing import Callable, Mapping, Sequence

from andy_trader.store import utc_now_iso

# Signal names are a closed set on purpose. A typo that silently creates a new
# signal series is far worse than a crash, because the series looks real and
# nobody notices it is empty until a model trains on nothing.
FEAR_GREED = "fear_greed"
FUNDING_RATE = "funding_rate"
OPEN_INTEREST = "open_interest"
LONG_RATIO = "long_ratio"
KNOWN_SIGNALS = frozenset({FEAR_GREED, FUNDING_RATE, OPEN_INTEREST, LONG_RATIO})

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


class SignalError(RuntimeError):
    """Raised for configuration problems, never for a source being unreachable."""


@dataclass(frozen=True)
class Signal:
    """One observation of one signal at one moment.

    `instrument` is None for market-wide signals such as Fear and Greed. That is
    a real distinction rather than a missing value, and queries must respect it.
    """

    signal: str
    source: str
    value: float | None
    observed_time: str
    instrument: str | None = None
    metadata: Mapping[str, object] | None = None
    degraded: bool = False
    degraded_reason: str | None = None

    def __post_init__(self) -> None:
        if self.signal not in KNOWN_SIGNALS:
            known = ", ".join(sorted(KNOWN_SIGNALS))
            raise SignalError(f"Unknown signal {self.signal!r}; known: {known}")

    def content_hash(self) -> str:
        payload = "|".join(
            (
                self.signal,
                self.source,
                self.instrument or "",
                self.observed_time,
                "" if self.value is None else repr(round(float(self.value), 12)),
                "degraded" if self.degraded else "ok",
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def initialize_signals(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_signals (
            content_hash TEXT PRIMARY KEY,
            signal TEXT NOT NULL,
            source TEXT NOT NULL,
            instrument TEXT,
            observed_time TEXT NOT NULL,
            value REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            degraded INTEGER NOT NULL DEFAULT 0,
            degraded_reason TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            times_seen INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS crypto_signals_lookup "
        "ON crypto_signals(signal, instrument, observed_time)",
        "CREATE INDEX IF NOT EXISTS crypto_signals_degraded ON crypto_signals(degraded)",
    ):
        connection.execute(index_sql)


def record_signals(connection: sqlite3.Connection, signals: Sequence[Signal]) -> tuple[int, int]:
    """Append signals, bumping times_seen for ones already recorded."""

    initialize_signals(connection)
    now = utc_now_iso()
    inserted = 0
    for signal in signals:
        digest = signal.content_hash()
        connection.execute(
            """
            INSERT INTO crypto_signals
            (content_hash, signal, source, instrument, observed_time, value,
             metadata_json, degraded, degraded_reason, first_seen_at, last_seen_at, times_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(content_hash) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                times_seen = crypto_signals.times_seen + 1
            """,
            (
                digest,
                signal.signal,
                signal.source,
                signal.instrument,
                signal.observed_time,
                signal.value,
                json.dumps(dict(signal.metadata or {}), sort_keys=True),
                1 if signal.degraded else 0,
                signal.degraded_reason,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT times_seen FROM crypto_signals WHERE content_hash = ?", (digest,)
        ).fetchone()
        if row is not None and row["times_seen"] == 1:
            inserted += 1
    connection.commit()
    return inserted, len(signals)


def _ms_to_iso(value: object) -> str:
    return datetime.fromtimestamp(int(value) / 1000, UTC).isoformat()


def _s_to_iso(value: object) -> str:
    return datetime.fromtimestamp(int(value), UTC).isoformat()


def parse_fear_greed(payload: object) -> list[Signal]:
    """alternative.me Fear and Greed, 0 to 100. Market-wide, so no instrument."""

    if not isinstance(payload, Mapping):
        raise ValueError(f"unexpected fear/greed payload {type(payload).__name__}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("fear/greed payload missing data")
    out = []
    for entry in data:
        if not isinstance(entry, Mapping):
            continue
        out.append(
            Signal(
                signal=FEAR_GREED,
                source="alternative.me",
                instrument=None,
                value=float(entry["value"]),
                observed_time=_s_to_iso(entry["timestamp"]),
                metadata={"classification": entry.get("value_classification")},
            )
        )
    return out


def parse_funding(payload: object, instrument: str) -> list[Signal]:
    """Bybit perpetual funding. Positive means longs pay shorts, so longs are crowded."""

    rows = _bybit_list(payload)
    return [
        Signal(
            signal=FUNDING_RATE,
            source="bybit",
            instrument=instrument,
            value=float(row["fundingRate"]),
            observed_time=_ms_to_iso(row["fundingRateTimestamp"]),
        )
        for row in rows
        if isinstance(row, Mapping) and "fundingRate" in row
    ]


def parse_open_interest(payload: object, instrument: str) -> list[Signal]:
    """Total outstanding contracts. Rising with price means new money entering."""

    rows = _bybit_list(payload)
    return [
        Signal(
            signal=OPEN_INTEREST,
            source="bybit",
            instrument=instrument,
            value=float(row["openInterest"]),
            observed_time=_ms_to_iso(row["timestamp"]),
        )
        for row in rows
        if isinstance(row, Mapping) and "openInterest" in row
    ]


def parse_long_ratio(payload: object, instrument: str) -> list[Signal]:
    """Share of accounts positioned long. The closest keyless read on crowding."""

    rows = _bybit_list(payload)
    return [
        Signal(
            signal=LONG_RATIO,
            source="bybit",
            instrument=instrument,
            value=float(row["buyRatio"]),
            observed_time=_ms_to_iso(row["timestamp"]),
            metadata={"sell_ratio": row.get("sellRatio")},
        )
        for row in rows
        if isinstance(row, Mapping) and "buyRatio" in row
    ]


def _bybit_list(payload: object) -> list[object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"unexpected Bybit payload {type(payload).__name__}")
    if payload.get("retCode") != 0:
        raise ValueError(f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Bybit payload missing result")
    rows = result.get("list")
    if not isinstance(rows, list):
        raise ValueError("Bybit result missing list")
    return rows


def collect_signals(
    instruments: Sequence[str],
    *,
    http: Callable[[str], object],
    limit: int = 200,
) -> tuple[list[Signal], list[dict[str, str]]]:
    """Fetch every signal, recording failures as degraded rows rather than raising.

    Same contract as the price collector: a source that cannot be reached
    produces a degraded row with a named reason, never a silent gap.
    """

    collected: list[Signal] = []
    problems: list[dict[str, str]] = []

    def attempt(name: str, instrument: str | None, url: str, parse) -> None:
        try:
            collected.extend(parse(http(url)))
        except Exception as exc:  # noqa: BLE001 - any failure becomes a degraded row
            reason = f"{type(exc).__name__}: {exc}"[:400]
            problems.append({"signal": name, "instrument": instrument or "-", "reason": reason})
            collected.append(
                Signal(
                    signal=name,
                    source="bybit" if instrument else "alternative.me",
                    instrument=instrument,
                    value=None,
                    observed_time=utc_now_iso(),
                    degraded=True,
                    degraded_reason=reason,
                )
            )

    attempt(
        FEAR_GREED, None,
        "https://api.alternative.me/fng/?limit=30",
        parse_fear_greed,
    )
    for instrument in instruments:
        symbol = _BYBIT_SYMBOLS.get(instrument)
        if symbol is None:
            continue
        attempt(
            FUNDING_RATE, instrument,
            f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={symbol}&limit={limit}",
            lambda p, i=instrument: parse_funding(p, i),
        )
        attempt(
            OPEN_INTEREST, instrument,
            f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}&intervalTime=1h&limit={limit}",
            lambda p, i=instrument: parse_open_interest(p, i),
        )
        attempt(
            LONG_RATIO, instrument,
            f"https://api.bybit.com/v5/market/account-ratio?category=linear&symbol={symbol}&period=1h&limit={limit}",
            lambda p, i=instrument: parse_long_ratio(p, i),
        )
    return collected, problems


def latest_signal(
    connection: sqlite3.Connection,
    signal: str,
    *,
    instrument: str | None = None,
    at_or_before: str | None = None,
) -> float | None:
    """Most recent non-degraded value at or before a moment.

    `at_or_before` exists so a backtest can ask what was knowable at a simulated
    bar. Omitting it reads the present, which is correct live and wrong in a
    replay, exactly like `predict_once`.
    """

    initialize_signals(connection)
    clauses = ["signal = ?", "degraded = 0", "value IS NOT NULL"]
    params: list[object] = [signal]
    if instrument is None:
        clauses.append("instrument IS NULL")
    else:
        clauses.append("instrument = ?")
        params.append(instrument)
    if at_or_before is not None:
        clauses.append("observed_time <= ?")
        params.append(at_or_before)
    row = connection.execute(
        "SELECT value FROM crypto_signals WHERE " + " AND ".join(clauses)
        + " ORDER BY observed_time DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return None if row is None else float(row["value"])
