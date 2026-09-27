"""Append-only SQLite store for crypto market observations and dated predictions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Collection, Iterable, Mapping, Sequence

from andy_trader.env import REPO_ROOT

DEFAULT_DB_FILENAME = "crypto_observations.db"

# Horizons the settlement job knows how to resolve. Adding one here without
# adding it to _HORIZON_DELTAS makes every prediction at that horizon
# permanently unsettleable, which is silent and therefore worse than a crash.
_HORIZON_DELTAS: Mapping[str, timedelta] = {
    # Sub-hourly horizons exist for the intra-round continuation strategy. They
    # are only settleable against 1m bars: settling a 2m call against the
    # default 1h series would compare a price up to 90 minutes away and score
    # pure noise as skill. Callers using these MUST pass interval="1m" and a
    # tight tolerance to `settle_due_predictions`; `settle_fast_predictions`
    # in andy_trader.fast_momentum is the safe entry point.
    "1m": timedelta(minutes=1),
    "2m": timedelta(minutes=2),
    "5m": timedelta(minutes=5),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}

# Horizons that must never be settled against the hourly series.
FAST_HORIZONS = frozenset({"1m", "2m", "5m"})


class CryptoStoreError(RuntimeError):
    """Raised when the store is asked to do something that would corrupt the record."""


def horizon_delta(horizon: str) -> timedelta:
    try:
        return _HORIZON_DELTAS[horizon]
    except KeyError as exc:
        known = ", ".join(sorted(_HORIZON_DELTAS))
        raise CryptoStoreError(f"Unknown horizon {horizon!r}; known horizons: {known}") from exc


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar as reported by one venue at one moment.

    `degraded` is not decoration. A collector that could not reach its source
    writes a degraded row with nulls and a reason rather than inventing a price
    or silently skipping, so a gap in the data is itself recorded as an
    observation. Downstream code must check it.
    """

    instrument: str
    venue: str
    interval: str
    open_time: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    degraded: bool = False
    degraded_reason: str | None = None

    def content_hash(self) -> str:
        """Hash the values, not the observation time.

        Re-fetching an unchanged closed candle must collapse onto the same row
        and bump times_seen. A candle whose values actually changed, which
        happens for the still-open bar and occasionally for venue revisions,
        hashes differently and lands as a new row. That is the honest record:
        we saw two different things and kept both.
        """

        payload = "|".join(
            (
                self.instrument,
                self.venue,
                self.interval,
                self.open_time,
                _fmt(self.open),
                _fmt(self.high),
                _fmt(self.low),
                _fmt(self.close),
                _fmt(self.volume),
                "degraded" if self.degraded else "ok",
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fmt(value: float | None) -> str:
    return "" if value is None else repr(round(float(value), 10))


@dataclass(frozen=True)
class Prediction:
    """A directional call, recorded before its outcome can be known.

    `probability_up` is always the probability that close(resolves_at) is
    strictly greater than `reference_price`. It is deliberately not "confidence
    in my direction": that formulation needs a transform before it can be
    scored, and the transform is where people quietly get Brier wrong.
    """

    predictor: str
    instrument: str
    horizon: str
    probability_up: float
    reference_price: float
    created_at: str
    resolves_at: str
    mode: str = "advisory"
    features: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability_up <= 1.0:
            raise CryptoStoreError(
                f"probability_up must be in [0, 1], got {self.probability_up!r}"
            )
        if self.reference_price <= 0:
            raise CryptoStoreError(f"reference_price must be positive, got {self.reference_price!r}")
        if self.mode not in {"advisory", "paper", "live"}:
            raise CryptoStoreError(f"Unknown mode {self.mode!r}")
        horizon_delta(self.horizon)


def default_database_path(environ: Mapping[str, str] | None = None) -> Path:
    import os

    source = os.environ if environ is None else environ
    configured = source.get("CRYPTO_DB_PATH", DEFAULT_DB_FILENAME)
    path = Path(configured)
    return path if path.is_absolute() else REPO_ROOT / path


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the append-only observation and prediction tables."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_observations (
            content_hash TEXT PRIMARY KEY,
            instrument TEXT NOT NULL,
            venue TEXT NOT NULL,
            interval TEXT NOT NULL,
            open_time TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            degraded INTEGER NOT NULL DEFAULT 0,
            degraded_reason TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            times_seen INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS crypto_observations_lookup "
        "ON crypto_observations(instrument, interval, open_time)",
        "CREATE INDEX IF NOT EXISTS crypto_observations_venue ON crypto_observations(venue)",
        "CREATE INDEX IF NOT EXISTS crypto_observations_degraded ON crypto_observations(degraded)",
    ):
        connection.execute(index_sql)

    # Predictions are written before the outcome exists and are never updated
    # except by the settlement job, which fills only the settle_* columns. Any
    # other UPDATE against this table is a bug: it would rewrite history and
    # destroy the one property that makes the evaluation trustworthy.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            predictor TEXT NOT NULL,
            instrument TEXT NOT NULL,
            horizon TEXT NOT NULL,
            probability_up REAL NOT NULL,
            reference_price REAL NOT NULL,
            mode TEXT NOT NULL,
            features_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            resolves_at TEXT NOT NULL,
            settled_at TEXT,
            settle_price REAL,
            outcome_up INTEGER,
            settle_note TEXT,
            UNIQUE(predictor, instrument, horizon, created_at)
        )
        """
    )
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS crypto_predictions_pending "
        "ON crypto_predictions(resolves_at) WHERE settled_at IS NULL",
        "CREATE INDEX IF NOT EXISTS crypto_predictions_predictor ON crypto_predictions(predictor)",
    ):
        connection.execute(index_sql)

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS complete_set_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            target_notional REAL NOT NULL,
            up_best_ask REAL,
            down_best_ask REAL,
            up_best_ask_depth_shares REAL,
            down_best_ask_depth_shares REAL,
            up_best_ask_depth_notional REAL,
            down_best_ask_depth_notional REAL,
            naive_combined_cost REAL,
            up_fill_shares REAL NOT NULL,
            down_fill_shares REAL NOT NULL,
            up_fill_cost REAL,
            down_fill_cost REAL,
            up_fee_cost REAL,
            down_fee_cost REAL,
            combined_cost REAL,
            mispriced INTEGER CHECK (mispriced IN (0, 1) OR mispriced IS NULL),
            net_combined_cost REAL,
            net_mispriced INTEGER CHECK (net_mispriced IN (0, 1) OR net_mispriced IS NULL),
            unmeasurable_reason TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS complete_set_observations_round "
        "ON complete_set_observations(round_id, observed_at)"
    )
    complete_set_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(complete_set_observations)")
    }
    # Fee-awareness was added a few hours after this table's first night of
    # data collection (the fee formula needed confirming against Polymarket's
    # own docs first). These columns are additive and nullable specifically so
    # the already-recorded rows are never rewritten -- they simply carry no fee
    # figure, which is honestly what is true: it was not computed at the time.
    for column in ("up_fee_cost", "down_fee_cost", "net_combined_cost"):
        if column not in complete_set_columns:
            connection.execute(
                f"ALTER TABLE complete_set_observations ADD COLUMN {column} REAL"
            )
    if "net_mispriced" not in complete_set_columns:
        connection.execute(
            "ALTER TABLE complete_set_observations ADD COLUMN net_mispriced INTEGER "
            "CHECK (net_mispriced IN (0, 1) OR net_mispriced IS NULL)"
        )


def record_observations(
    connection: sqlite3.Connection,
    candles: Iterable[Candle],
    *,
    observed_at: str | None = None,
) -> tuple[int, int]:
    """Insert new observations, bumping times_seen for ones already recorded.

    Returns (inserted, seen). Nothing is ever overwritten.

    `observed_at` is the capture time stamped on new rows (first_seen_at) and
    on re-observed ones (last_seen_at). Collectors leave it unset, meaning now;
    settlement treats these stamps as when each price was true, so tests that
    walk a simulated clock pass it explicitly instead of rewriting rows.
    """

    now = observed_at or utc_now_iso()
    inserted = 0
    seen = 0
    for candle in candles:
        seen += 1
        digest = candle.content_hash()
        cursor = connection.execute(
            """
            INSERT INTO crypto_observations
            (content_hash, instrument, venue, interval, open_time, open, high, low, close,
             volume, degraded, degraded_reason, first_seen_at, last_seen_at, times_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(content_hash) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                times_seen = crypto_observations.times_seen + 1
            """,
            (
                digest,
                candle.instrument,
                candle.venue,
                candle.interval,
                candle.open_time,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                1 if candle.degraded else 0,
                candle.degraded_reason,
                now,
                now,
            ),
        )
        # rowcount is 1 for both INSERT and the upsert path, so compare timestamps
        # instead of trusting it.
        row = connection.execute(
            "SELECT first_seen_at, times_seen FROM crypto_observations WHERE content_hash = ?",
            (digest,),
        ).fetchone()
        if row is not None and row["times_seen"] == 1:
            inserted += 1
        del cursor
    connection.commit()
    return inserted, seen


def record_prediction(connection: sqlite3.Connection, prediction: Prediction) -> int:
    """Write one call before its outcome is knowable. Returns the prediction id."""

    cursor = connection.execute(
        """
        INSERT INTO crypto_predictions
        (predictor, instrument, horizon, probability_up, reference_price, mode,
         features_json, created_at, resolves_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(predictor, instrument, horizon, created_at) DO NOTHING
        """,
        (
            prediction.predictor,
            prediction.instrument,
            prediction.horizon,
            prediction.probability_up,
            prediction.reference_price,
            prediction.mode,
            json.dumps(dict(prediction.features), sort_keys=True),
            prediction.created_at,
            prediction.resolves_at,
        ),
    )
    connection.commit()
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    existing = connection.execute(
        """
        SELECT id FROM crypto_predictions
        WHERE predictor = ? AND instrument = ? AND horizon = ? AND created_at = ?
        """,
        (prediction.predictor, prediction.instrument, prediction.horizon, prediction.created_at),
    ).fetchone()
    if existing is None:  # pragma: no cover - only reachable on a corrupted store
        raise CryptoStoreError("Prediction neither inserted nor found after conflict")
    return int(existing["id"])


# How long settlement keeps waiting for a price at or after the resolve time
# before falling back to the latest earlier one. Collectors refetch history
# (120 hourly bars, 500 one-minute bars ~ 8.3h), so a bar missed during an
# outage usually arrives on recovery; 6h stays inside the shortest of those.
SETTLEMENT_FALLBACK_GRACE = timedelta(hours=6)


def price_moments(row: Mapping[str, object], interval: str) -> tuple[datetime, ...]:
    """The moments at which a stored close is known to have been the price.

    A row is captured at `first_seen_at` and, if the identical bar was fetched
    again, last at `last_seen_at`; the same close held at both. A bar's close
    is the price at the bar's END, so a capture after the bar closed speaks for
    the end time, not the fetch time -- which is what makes a completed bar
    fetched late (after an outage) settle correctly. A capture can also not
    precede the bar's open; a clock-skewed or future-dated bar is clamped.

    Capture stamps are written when the whole collection pass ends, so they
    can trail the real fetch by the length of the pass (about a minute live):
    a small, one-directional approximation.
    """

    bar_open = _as_utc(datetime.fromisoformat(str(row["open_time"])))
    bar_end = bar_open + horizon_delta(interval)
    moments = set()
    for column in ("first_seen_at", "last_seen_at"):
        stamp = row[column]
        if stamp:
            captured = _as_utc(datetime.fromisoformat(str(stamp)))
            moments.add(min(max(captured, bar_open), bar_end))
    return tuple(sorted(moments))


def close_price_at(
    connection: sqlite3.Connection,
    instrument: str,
    at_iso: str,
    *,
    interval: str = "1h",
    tolerance_minutes: int = 90,
    now_iso: str | None = None,
) -> tuple[float | None, str]:
    """The first known price at or after `at_iso`, and a note explaining it.

    Returns (price, note). A None price means the store cannot settle this yet,
    which is a legitimate state and must not be filled with a guess.

    Every stored close is given its `price_moments`, and the call settles on
    the earliest one in [at_iso, at_iso + tolerance], across every bar in range.
    Found 2026-09-28: the old rule took the bar whose OPEN time was nearest and
    broke ties between that bar's still-forming snapshots by `times_seen`,
    which is 1 for all of them. 58% of 1h calls settled on a price captured
    before they resolved (median 15, up to 45 minutes early), and a completed
    bar opening exactly at `at_iso` was settled on its close an interval LATE.
    Codex's review of the first fix showed why choosing the bar first is still
    wrong: at 01:47 the 01:00 bar may hold a price captured at 01:47 while the
    02:00 bar's first price comes later.

    If nothing at or after `at_iso` has been captured yet, the call waits.
    Once `now` is past `at_iso + tolerance + SETTLEMENT_FALLBACK_GRACE`,
    waiting cannot help -- old bars stop being refetched -- so it settles on the
    latest price in [at_iso - tolerance, at_iso) instead, and says so in the
    note, rather than staying pending forever. The grace leaves room for a
    collector recovering from an outage to refetch the missing bar first.
    """

    target = _as_utc(datetime.fromisoformat(at_iso))
    window = timedelta(minutes=tolerance_minutes)
    bar = horizon_delta(interval)
    rows = connection.execute(
        """
        SELECT content_hash, open_time, close, venue, times_seen, first_seen_at, last_seen_at
        FROM crypto_observations
        WHERE instrument = ? AND interval = ? AND degraded = 0 AND close IS NOT NULL
          AND open_time BETWEEN ? AND ?
        """,
        (instrument, interval, (target - window - bar).isoformat(), (target + window).isoformat()),
    ).fetchall()
    moments = [(moment, row) for row in rows for moment in price_moments(row, interval)]

    def best(candidates: list[tuple[datetime, sqlite3.Row]], latest: bool) -> tuple[datetime, sqlite3.Row]:
        # Same moment from several rows or venues: most-confirmed, then venue,
        # then close and content hash, so the choice never depends on row order.
        chosen = max(m for m, _ in candidates) if latest else min(m for m, _ in candidates)
        tied = [row for m, row in candidates if m == chosen]
        row = sorted(
            tied,
            key=lambda r: (-int(r["times_seen"]), str(r["venue"]), float(r["close"]), str(r["content_hash"])),
        )[0]
        return chosen, row

    after = [(m, row) for m, row in moments if target <= m <= target + window]
    if after:
        moment, row = best(after, latest=False)
        return float(row["close"]), (
            f"{row['venue']} {interval} close at {row['open_time']}, price as of {moment.isoformat()}"
        )

    now = _as_utc(datetime.fromisoformat(now_iso)) if now_iso else datetime.now(UTC)
    before = [(m, row) for m, row in moments if target - window <= m < target]
    if now > target + window + SETTLEMENT_FALLBACK_GRACE and before:
        moment, row = best(before, latest=True)
        return float(row["close"]), (
            f"{row['venue']} {interval} close at {row['open_time']}, price as of {moment.isoformat()} "
            f"(latest price before {at_iso}; nothing was captured after it)"
        )
    if not moments:
        return None, f"no non-degraded {interval} close within {tolerance_minutes}m of {at_iso}"
    return None, f"no {interval} price captured at or after {at_iso} yet; waiting"


def _as_utc(moment: datetime) -> datetime:
    """Naive timestamps are UTC by this store's convention; aware ones are converted."""

    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def settle_due_predictions(
    connection: sqlite3.Connection,
    *,
    now_iso: str | None = None,
    interval: str = "1h",
    tolerance_minutes: int = 90,
    horizons: Collection[str] | None = None,
) -> dict[str, int]:
    """Resolve every prediction whose horizon has elapsed.

    This function deliberately never reads probability_up. It looks up the price
    and compares it to reference_price. Keeping the outcome computation blind to
    the call is what stops a settlement bug from flattering the score.

    `horizons` restricts which horizons this pass will touch. It defaults to
    "every horizon except the sub-hourly ones", because the default 90-minute
    tolerance against an hourly series would settle a 2-minute call using a
    price from over an hour away and record the resulting coin flip as a real
    outcome. Fast horizons must be settled by their own pass against 1m bars.
    """

    now = now_iso or utc_now_iso()
    if horizons is None:
        selected = None if interval in {"1m", "5m"} else "exclude_fast"
    else:
        selected = tuple(horizons)

    query = """
        SELECT id, instrument, reference_price, resolves_at
        FROM crypto_predictions
        WHERE settled_at IS NULL AND resolves_at <= ?
    """
    params: list[object] = [now]
    if selected == "exclude_fast":
        placeholders = ", ".join("?" for _ in FAST_HORIZONS)
        query += f" AND horizon NOT IN ({placeholders})"
        params.extend(sorted(FAST_HORIZONS))
    elif isinstance(selected, tuple):
        if not selected:
            return {"due": 0, "settled": 0, "unresolvable": 0}
        placeholders = ", ".join("?" for _ in selected)
        query += f" AND horizon IN ({placeholders})"
        params.extend(selected)
    query += " ORDER BY resolves_at ASC"
    pending = connection.execute(query, params).fetchall()

    settled = 0
    unresolvable = 0
    for row in pending:
        price, note = close_price_at(
            connection,
            row["instrument"],
            row["resolves_at"],
            interval=interval,
            tolerance_minutes=tolerance_minutes,
            now_iso=now,
        )
        if price is None:
            unresolvable += 1
            continue
        outcome_up = 1 if price > float(row["reference_price"]) else 0
        connection.execute(
            """
            UPDATE crypto_predictions
            SET settled_at = ?, settle_price = ?, outcome_up = ?, settle_note = ?
            WHERE id = ? AND settled_at IS NULL
            """,
            (utc_now_iso(), price, outcome_up, note, row["id"]),
        )
        settled += 1
    connection.commit()
    return {"due": len(pending), "settled": settled, "unresolvable": unresolvable}


def fetch_settled(
    connection: sqlite3.Connection,
    *,
    predictor: str | None = None,
    instrument: str | None = None,
    horizon: str | None = None,
) -> Sequence[sqlite3.Row]:
    clauses = ["settled_at IS NOT NULL", "outcome_up IS NOT NULL"]
    params: list[object] = []
    for column, value in (("predictor", predictor), ("instrument", instrument), ("horizon", horizon)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    sql = "SELECT * FROM crypto_predictions WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC"
    return connection.execute(sql, tuple(params)).fetchall()
