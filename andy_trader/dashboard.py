"""Local, real-time, read-only monitor for Andy Trader.

Serves one page over plain HTTP on 127.0.0.1, never the network -- this reads
your live trading database and has no reason to ever leave your machine.

**What this shows and what it does not.** Live collected prices, every
prediction as it is logged and settled, the running Brier-skill scoreboard per
predictor, the CT-07 model registry (every retrain, promoted or rejected, with
its holdout score), and CT-08's paper portfolios (simulated cash and position
per predictor, real trading costs, an append-only equity curve). Every dollar
shown is simulated -- `andy_trader.portfolio` never touches a real exchange
account, and paper trading only happens for a (predictor, instrument) pair you
have explicitly run a cycle for. Nothing trades itself into existence just by
this dashboard being open.

No third-party dependencies: stdlib http.server + sqlite3 + string templating,
matching the rest of the project.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from andy_trader.env import REPO_ROOT, load_env_file
from andy_trader.predict import DEFAULT_MAX_DATA_AGE_MINUTES, load_closes, score_all
from andy_trader.store import connect, default_database_path

DEFAULT_PORT = 8787
RECENT_PREDICTIONS_LIMIT = 40
RECENT_REGISTRY_LIMIT = 20


def _seconds_since(iso_timestamp: str, now: datetime) -> float:
    parsed = datetime.fromisoformat(iso_timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed).total_seconds()


def _collector_health(connection: sqlite3.Connection, now: datetime) -> dict[str, object]:
    """Per series source reachability and usable-bar freshness."""

    rows = connection.execute(
        """
        SELECT instrument, interval,
               MAX(last_seen_at) AS last_seen_at,
               MAX(open_time) AS latest_bar_at
        FROM crypto_observations
        WHERE degraded = 0 AND close IS NOT NULL
        GROUP BY instrument, interval
        ORDER BY instrument, interval
        """
    ).fetchall()
    series = []
    most_recent = None
    for row in rows:
        last_seen_at = row["last_seen_at"]
        age_seconds = _seconds_since(last_seen_at, now)
        latest_bar_at = row["latest_bar_at"]
        data_age_seconds = _seconds_since(latest_bar_at, now)
        interval_seconds = {"1h": 3600, "4h": 14_400, "1d": 86_400}.get(
            row["interval"], 0
        )
        # Bar open_time naturally trails the wall clock by up to one complete
        # interval. Twenty extra minutes covers the 15-minute scheduler plus a
        # small completion margin without masking a genuinely stale reference.
        data_stale = data_age_seconds > interval_seconds + 20 * 60
        series.append(
            {
                "instrument": row["instrument"],
                "interval": row["interval"],
                "last_seen_at": last_seen_at,
                "age_seconds": age_seconds,
                "stale": age_seconds > 20 * 60,  # scheduled cycle runs every 15 min
                "latest_bar_at": latest_bar_at,
                "data_age_seconds": data_age_seconds,
                "data_stale": data_stale,
            }
        )
        if most_recent is None or age_seconds < most_recent:
            most_recent = age_seconds
    return {
        "series": series,
        "freshest_age_seconds": most_recent,
        "healthy": bool(series) and all(
            not row["stale"] and not row["data_stale"] for row in series
        ),
    }


def _pending_overdue(connection: sqlite3.Connection, now_iso: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM crypto_predictions WHERE settled_at IS NULL AND resolves_at <= ?",
        (now_iso,),
    ).fetchone()
    return int(row["n"]) if row else 0


def _recent_predictions(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT predictor, instrument, horizon, probability_up, reference_price,
               created_at, resolves_at, settled_at, settle_price, outcome_up
        FROM crypto_predictions
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (RECENT_PREDICTIONS_LIMIT,),
    ).fetchall()
    return [dict(row) for row in rows]


def _latest_prices(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT instrument, interval, close, open_time
        FROM (
            SELECT instrument, interval, close, open_time, venue, times_seen,
                   ROW_NUMBER() OVER (
                       PARTITION BY instrument, interval
                       ORDER BY open_time DESC, times_seen DESC, venue ASC
                   ) AS rank
            FROM crypto_observations
            WHERE degraded = 0 AND close IS NOT NULL
        )
        WHERE rank = 1
        ORDER BY instrument, interval
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _scoreboard(connection: sqlite3.Connection) -> tuple[dict[str, object], dict[str, int]]:
    excluded_stale: dict[str, int] = {}
    reports = score_all(
        connection,
        minimum=1,
        maximum_data_age_minutes=DEFAULT_MAX_DATA_AGE_MINUTES,
        excluded_stale=excluded_stale,
    )
    out: dict[str, object] = {}
    for predictor, report in reports.items():
        if isinstance(report, dict):  # CalibrationError case
            out[predictor] = report
        else:
            out[predictor] = report.as_dict()
    return out, excluded_stale


def _registry(connection: sqlite3.Connection) -> list[dict[str, object]]:
    try:
        rows = connection.execute(
            """
            SELECT model_id, trained_at, instrument, interval, horizon,
                   holdout_bars, holdout_brier_skill, base_rate_brier_skill,
                   promoted, promotion_reason
            FROM model_registry
            ORDER BY trained_at DESC, id DESC
            LIMIT ?
            """,
            (RECENT_REGISTRY_LIMIT,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # table does not exist yet: no model has ever been trained
    return [dict(row) for row in rows]


def _portfolio_summary(connection: sqlite3.Connection) -> dict[str, object] | None:
    """Aggregate P&L across every coin, if andy_trader.portfolio.portfolio_summary exists yet.

    Deliberately tolerant of it not existing: this dashboard should never go
    down just because a dependent module hasn't landed yet.
    """

    try:
        from andy_trader.portfolio import portfolio_summary
    except ImportError:
        return None
    try:
        summary = portfolio_summary(connection)
    except Exception:  # noqa: BLE001 - a summary glitch must never take the whole page down
        return None
    return summary.__dict__ if hasattr(summary, "__dict__") else dict(summary)


def _portfolios(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """One summary row per (predictor, instrument) pair that has ever paper-traded."""

    try:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(paper_portfolio_state)")
        }
        if not columns:
            return []
        # Keep the monitor genuinely read-only even while an older CT-08
        # database is waiting for portfolio.initialize_portfolio to perform
        # its explicit migration. The only pre-migration bankroll was the
        # original fixed $10,000 default.
        starting_cash_sql = (
            "starting_cash" if "starting_cash" in columns else "10000.0 AS starting_cash"
        )
        rows = connection.execute(
            f"SELECT predictor, instrument, {starting_cash_sql}, cash, position_qty, "
            "avg_entry_price, updated_at FROM paper_portfolio_state "
            "ORDER BY predictor, instrument"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # no paper trade has ever run yet

    summaries: list[dict[str, object]] = []
    for row in rows:
        predictor, instrument = row["predictor"], row["instrument"]
        curve = [
            dict(point)
            for point in reversed(
                connection.execute(
                    "SELECT * FROM paper_equity_curve "
                    "WHERE predictor = ? AND instrument = ? "
                    "ORDER BY recorded_at DESC LIMIT 200",
                    (predictor, instrument),
                ).fetchall()
            )
        ]
        trades = connection.execute(
            "SELECT id FROM paper_trades WHERE predictor = ? AND instrument = ? LIMIT 1000",
            (predictor, instrument),
        ).fetchall()
        latest_equity = curve[-1]["equity"] if curve else float(row["cash"])
        starting_equity = float(row["starting_cash"])
        risk_state = _risk_state(connection, predictor=predictor, instrument=instrument)
        price_series = [
            {"time": open_time, "close": close}
            for open_time, close in load_closes(connection, instrument, interval="1h", limit=200)
        ]
        summaries.append(
            {
                "predictor": predictor,
                "instrument": instrument,
                "cash": float(row["cash"]),
                "position_qty": float(row["position_qty"]),
                "avg_entry_price": row["avg_entry_price"],
                "equity": latest_equity,
                "starting_equity": starting_equity,
                "return_pct": (latest_equity / starting_equity - 1.0) * 100.0 if starting_equity else 0.0,
                "trade_count": len(trades),
                "equity_curve": [point["equity"] for point in curve[-100:]],
                "equity_curve_full": [
                    {"time": point["recorded_at"], "equity": point["equity"]} for point in curve
                ],
                "price_series": price_series,
                "updated_at": row["updated_at"],
                "risk": risk_state,
            }
        )
    return summaries


def _risk_state(connection: sqlite3.Connection, *, predictor: str, instrument: str) -> dict[str, object]:
    """CT-10 kill-switch status for one portfolio. Read-only; never trips or clears anything."""

    try:
        row = connection.execute(
            "SELECT tripped, severity, tripped_at, tripped_reason FROM risk_kill_switch "
            "WHERE predictor = ? AND instrument = ?",
            (predictor, instrument),
        ).fetchone()
    except sqlite3.OperationalError:
        return {"tripped": False, "severity": None, "reason": None}
    if row is None or not row["tripped"]:
        return {"tripped": False, "severity": None, "reason": None}
    return {"tripped": True, "severity": row["severity"], "reason": row["tripped_reason"]}


def _json_safe(value: object) -> object:
    """Recursively replace non-finite floats with None before serializing.

    Python's json.dumps happily emits the non-standard tokens Infinity,
    -Infinity, and NaN. A real predictor can legitimately produce one --
    CT-02's calibration harness deliberately reports -inf skill for a
    degenerate sample rather than a flattering 0.0 (see calibration.py) --
    and a brand-new predictor with only a couple of settled calls hits that
    exact case in practice, not just in theory. Standard JSON has no token
    for these values, so a strict client-side JSON.parse rejects the entire
    payload the instant one appears anywhere in it. The frontend already
    knows how to display a degenerate reading (it checks the `degenerate`
    flag independently of the number itself); it just needs a value it can
    actually receive.
    """

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def build_dashboard_state(connection: sqlite3.Connection) -> dict[str, object]:
    """Pure, read-only snapshot of everything the dashboard displays.

    Deliberately separate from the HTTP layer so it can be tested and reasoned
    about without spinning up a server.
    """

    now = datetime.now(UTC)
    now_iso = now.isoformat()
    scoreboard, score_exclusions = _scoreboard(connection)
    return {
        "generated_at": now_iso,
        "collector_health": _collector_health(connection, now),
        "pending_overdue": _pending_overdue(connection, now_iso),
        "latest_prices": _latest_prices(connection),
        "recent_predictions": _recent_predictions(connection),
        "scoreboard": scoreboard,
        "score_exclusions": score_exclusions,
        "registry": _registry(connection),
        "portfolios": _portfolios(connection),
        "portfolio_summary": _portfolio_summary(connection),
    }


_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Andy Trader Monitor</title>
<style>
  :root { color-scheme: dark; }
  body { background:#0b0f14; color:#d8e1e8; font:14px/1.4 -apple-system,Segoe UI,sans-serif; margin:0; padding:24px; }
  h1 { font-size:18px; margin:0 0 4px; }
  .badge { display:inline-block; background:#7a3b00; color:#ffd699; padding:3px 10px; border-radius:4px; font-weight:700; font-size:12px; letter-spacing:.04em; }
  .sub { color:#8fa3b0; font-size:12px; margin-bottom:20px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; align-items:start; }
  .card { background:#11161d; border:1px solid #1e2833; border-radius:8px; padding:14px 16px; }
  .card h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:#8fa3b0; margin:0 0 10px; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th, td { text-align:left; padding:4px 6px; border-bottom:1px solid #1a232c; white-space:nowrap; }
  th { color:#6d8494; font-weight:600; }
  .ok { color:#4ade80; } .bad { color:#f87171; } .warn { color:#facc15; } .muted { color:#5c7080; }
  .pill { padding:1px 7px; border-radius:10px; font-size:11px; font-weight:600; }
  .pill.promoted { background:#123a1f; color:#4ade80; }
  .pill.rejected { background:#3a1212; color:#f87171; }
  #err { color:#f87171; display:none; margin-bottom:12px; }
  .updated { font-size:11px; color:#5c7080; }
  .bigchart-card { display:flex; flex-direction:column; gap:6px; }
  .bigchart-head { display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px 20px; }
  .bigchart-title { font-size:12px; color:#8fa3b0; text-transform:uppercase; letter-spacing:.05em; }
  .bigchart-value { font-size:26px; font-weight:700; }
  .bigchart-change { font-size:14px; font-weight:600; }
  .bigchart-row { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
  .bigchart-svg-wrap { position:relative; }
  .bigchart-svg-wrap svg { width:100%; height:auto; display:block; }
  .bigchart-empty { color:#5c7080; font-size:12px; padding:40px 0; text-align:center; }
  .summary-strip { display:flex; gap:24px; flex-wrap:wrap; background:#11161d; border:1px solid #1e2833; border-radius:8px; padding:16px 20px; margin-bottom:20px; }
  .summary-item { display:flex; flex-direction:column; gap:2px; }
  .summary-label { font-size:11px; color:#8fa3b0; text-transform:uppercase; letter-spacing:.05em; }
  .summary-value { font-size:22px; font-weight:700; }
  .summary-sub { font-size:12px; color:#8fa3b0; }
</style>
</head>
<body>
  <h1>Andy Trader &mdash; Live Monitor <span class="badge">PAPER &middot; SIMULATED CAPITAL &middot; REAL MARKET</span></h1>
  <div class="sub">Real prices, real predictions, real settlement, simulated cash and positions only &mdash; nothing here ever touches a real exchange account.</div>
  <div id="err"></div>
  <div id="summary"></div>
  <div id="bigcharts"></div>
  <div class="grid">
    <div class="card" style="grid-column:1/-1;">
      <h2>Paper Portfolios (CT-08)</h2>
      <table id="portfolios"><thead><tr><th>Predictor</th><th>Instrument</th><th>Side</th><th>Cash</th><th>Position</th><th>Equity</th><th>Return</th><th>Trades</th><th>Curve</th><th>Risk (CT-10)</th></tr></thead><tbody></tbody></table>
    </div>
    <div class="card">
      <h2>Collector Health</h2>
      <table id="health"><thead><tr><th>Instrument</th><th>Interval</th><th>Last Success</th><th>Source Age</th><th>Latest Bar</th><th>Bar Age</th></tr></thead><tbody></tbody></table>
    </div>
    <div class="card">
      <h2>Latest Prices</h2>
      <table id="prices"><thead><tr><th>Instrument</th><th>Interval</th><th>Close</th><th>Open Time</th></tr></thead><tbody></tbody></table>
    </div>
    <div class="card">
      <h2>Scoreboard (Brier skill vs. base rate)</h2>
      <p class="updated" id="score-quality"></p>
      <table id="scoreboard"><thead><tr><th>Predictor</th><th>N</th><th>Skill</th><th>Hit Rate</th></tr></thead><tbody></tbody></table>
    </div>
    <div class="card">
      <h2>Model Registry (CT-07)</h2>
      <table id="registry"><thead><tr><th>Model</th><th>Trained</th><th>Holdout Skill</th><th>vs Base</th><th>Verdict</th></tr></thead><tbody></tbody></table>
    </div>
    <div class="card" style="grid-column:1/-1;">
      <h2>Recent Predictions</h2>
      <table id="predictions"><thead><tr><th>Predictor</th><th>Instrument</th><th>Horizon</th><th>P(up)</th><th>Created</th><th>Status</th></tr></thead><tbody></tbody></table>
    </div>
  </div>
  <p class="updated" id="updated"></p>
<script>
function fmtAge(s) {
  if (s == null) return "-";
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s/60) + "m";
  return (s/3600).toFixed(1) + "h";
}
function fmtTime(iso) {
  if (!iso) return "-";
  return iso.replace("T"," ").slice(0,19) + "Z";
}
function td(v) { const e = document.createElement("td"); e.textContent = v; return e; }
function row(cells) { const r = document.createElement("tr"); cells.forEach(c => r.appendChild(c)); return r; }

function sparkline(values) {
  if (!values || values.length < 2) return document.createTextNode("-");
  const w = 120, h = 28, pad = 2;
  const min = Math.min(...values), max = Math.max(...values);
  const span = (max - min) || 1;
  const step = (w - 2*pad) / (values.length - 1);
  const points = values.map((v, i) => {
    const x = pad + i * step;
    const y = h - pad - ((v - min) / span) * (h - 2*pad);
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  const rising = values[values.length - 1] >= values[0];
  const svgns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgns, "svg");
  svg.setAttribute("width", w); svg.setAttribute("height", h);
  const poly = document.createElementNS(svgns, "polyline");
  poly.setAttribute("points", points);
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", rising ? "#4ade80" : "#f87171");
  poly.setAttribute("stroke-width", "1.5");
  svg.appendChild(poly);
  return svg;
}

const SVGNS = "http://www.w3.org/2000/svg";

function bigLineChart(values, opts) {
  // opts: { referenceValue?, valuePrefix?, valueSuffix? }
  const w = 640, h = 200, padL = 8, padR = 8, padT = 12, padB = 12;
  const wrap = document.createElement("div");
  wrap.className = "bigchart-svg-wrap";
  if (!values || values.length < 2) {
    const empty = document.createElement("div");
    empty.className = "bigchart-empty";
    empty.textContent = "not enough data points yet";
    wrap.appendChild(empty);
    return wrap;
  }
  const ref = opts.referenceValue != null ? opts.referenceValue : values[0];
  const min = Math.min(...values, ref), max = Math.max(...values, ref);
  const span = (max - min) || Math.abs(ref) * 0.01 || 1;
  const x = i => padL + (i / (values.length - 1)) * (w - padL - padR);
  const y = v => padT + (1 - (v - min) / span) * (h - padT - padB);
  const rising = values[values.length - 1] >= ref;
  const color = rising ? "#4ade80" : "#f87171";

  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");

  const gradId = "grad-" + Math.random().toString(36).slice(2);
  const defs = document.createElementNS(SVGNS, "defs");
  const grad = document.createElementNS(SVGNS, "linearGradient");
  grad.setAttribute("id", gradId);
  grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0");
  grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
  const stop1 = document.createElementNS(SVGNS, "stop");
  stop1.setAttribute("offset", "0%"); stop1.setAttribute("stop-color", color); stop1.setAttribute("stop-opacity", "0.35");
  const stop2 = document.createElementNS(SVGNS, "stop");
  stop2.setAttribute("offset", "100%"); stop2.setAttribute("stop-color", color); stop2.setAttribute("stop-opacity", "0");
  grad.appendChild(stop1); grad.appendChild(stop2);
  defs.appendChild(grad);
  svg.appendChild(defs);

  // Reference line (starting equity, or the window's opening price).
  const refLine = document.createElementNS(SVGNS, "line");
  refLine.setAttribute("x1", padL); refLine.setAttribute("x2", w - padR);
  refLine.setAttribute("y1", y(ref)); refLine.setAttribute("y2", y(ref));
  refLine.setAttribute("stroke", "#3a4756"); refLine.setAttribute("stroke-dasharray", "4,4");
  svg.appendChild(refLine);

  const linePoints = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const areaPoints = `${padL.toFixed(1)},${(h - padB).toFixed(1)} ` + linePoints +
    ` ${x(values.length - 1).toFixed(1)},${(h - padB).toFixed(1)}`;

  const area = document.createElementNS(SVGNS, "polygon");
  area.setAttribute("points", areaPoints);
  area.setAttribute("fill", `url(#${gradId})`);
  svg.appendChild(area);

  const poly = document.createElementNS(SVGNS, "polyline");
  poly.setAttribute("points", linePoints);
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", color);
  poly.setAttribute("stroke-width", "2");
  poly.setAttribute("stroke-linejoin", "round");
  svg.appendChild(poly);

  // A dot on the last point makes "where are we right now" unambiguous.
  const dot = document.createElementNS(SVGNS, "circle");
  dot.setAttribute("cx", x(values.length - 1)); dot.setAttribute("cy", y(values[values.length - 1]));
  dot.setAttribute("r", "3.5"); dot.setAttribute("fill", color);
  svg.appendChild(dot);

  const maxLabel = document.createElementNS(SVGNS, "text");
  maxLabel.setAttribute("x", padL); maxLabel.setAttribute("y", padT + 4);
  maxLabel.setAttribute("fill", "#5c7080"); maxLabel.setAttribute("font-size", "10");
  maxLabel.textContent = (opts.valuePrefix || "") + max.toFixed(2) + (opts.valueSuffix || "");
  svg.appendChild(maxLabel);

  const minLabel = document.createElementNS(SVGNS, "text");
  minLabel.setAttribute("x", padL); minLabel.setAttribute("y", h - 2);
  minLabel.setAttribute("fill", "#5c7080"); minLabel.setAttribute("font-size", "10");
  minLabel.textContent = (opts.valuePrefix || "") + min.toFixed(2) + (opts.valueSuffix || "");
  svg.appendChild(minLabel);

  wrap.appendChild(svg);
  return wrap;
}

function bigChartCard(title, values, opts) {
  const card = document.createElement("div");
  card.className = "card bigchart-card";
  const head = document.createElement("div");
  head.className = "bigchart-head";
  const titleEl = document.createElement("div");
  titleEl.className = "bigchart-title";
  titleEl.textContent = title;
  head.appendChild(titleEl);

  if (values && values.length >= 2) {
    const ref = opts.referenceValue != null ? opts.referenceValue : values[0];
    const current = values[values.length - 1];
    const changePct = ref ? ((current / ref - 1) * 100) : 0;
    const valueEl = document.createElement("div");
    valueEl.className = "bigchart-value";
    valueEl.textContent = (opts.valuePrefix || "") + current.toFixed(2) + (opts.valueSuffix || "");
    valueEl.style.color = changePct >= 0 ? "#4ade80" : "#f87171";
    const changeEl = document.createElement("span");
    changeEl.className = "bigchart-change";
    changeEl.style.color = changePct >= 0 ? "#4ade80" : "#f87171";
    changeEl.textContent = (changePct >= 0 ? "+" : "") + changePct.toFixed(2) + "% " + (changePct >= 0 ? "▲" : "▼");
    valueEl.appendChild(document.createTextNode(" "));
    valueEl.appendChild(changeEl);
    head.appendChild(valueEl);
  }
  card.appendChild(head);
  card.appendChild(bigLineChart(values, opts));
  return card;
}

function summaryItem(label, valueText, valueColor, subText) {
  const item = document.createElement("div");
  item.className = "summary-item";
  const lab = document.createElement("div");
  lab.className = "summary-label"; lab.textContent = label;
  const val = document.createElement("div");
  val.className = "summary-value"; val.textContent = valueText;
  if (valueColor) val.style.color = valueColor;
  item.appendChild(lab); item.appendChild(val);
  if (subText) {
    const sub = document.createElement("div");
    sub.className = "summary-sub"; sub.textContent = subText;
    item.appendChild(sub);
  }
  return item;
}

function renderSummary(summary) {
  const container = document.getElementById("summary");
  container.innerHTML = "";
  if (!summary || summary.total_starting_cash === 0) {
    return;  // nothing paper-traded yet -- the per-coin section already explains that
  }
  const strip = document.createElement("div");
  strip.className = "summary-strip";
  const upColor = "#4ade80", downColor = "#f87171";
  const pnlColor = summary.total_return_pct >= 0 ? upColor : downColor;

  strip.appendChild(summaryItem(
    "Total Equity (all coins)",
    "$" + summary.total_equity.toFixed(2),
    pnlColor,
    "started at $" + summary.total_starting_cash.toFixed(2)
  ));
  strip.appendChild(summaryItem(
    "Total Return",
    (summary.total_return_pct >= 0 ? "+" : "") + summary.total_return_pct.toFixed(2) + "%",
    pnlColor,
    (summary.total_equity - summary.total_starting_cash >= 0 ? "+$" : "-$") +
      Math.abs(summary.total_equity - summary.total_starting_cash).toFixed(2)
  ));
  strip.appendChild(summaryItem("Winners / Losers", summary.winners + " / " + summary.losers, null, summary.total_trade_count + " trades total"));
  if (summary.best) {
    strip.appendChild(summaryItem("Best Coin", summary.best[1], upColor, summary.best[0] + " · " + (summary.best[2] >= 0 ? "+" : "") + summary.best[2].toFixed(2) + "%"));
  }
  if (summary.worst) {
    strip.appendChild(summaryItem("Worst Coin", summary.worst[1], downColor, summary.worst[0] + " · " + (summary.worst[2] >= 0 ? "+" : "") + summary.worst[2].toFixed(2) + "%"));
  }
  container.appendChild(strip);
}

function renderBigCharts(portfolios) {
  const container = document.getElementById("bigcharts");
  container.innerHTML = "";
  portfolios.forEach(p => {
    const row = document.createElement("div");
    row.className = "bigchart-row";
    row.style.marginBottom = "20px";

    const equityValues = (p.equity_curve_full || []).map(pt => pt.equity);
    row.appendChild(
      bigChartCard(
        `Equity — ${p.predictor} on ${p.instrument} (PAPER)`,
        equityValues,
        { referenceValue: p.starting_equity, valuePrefix: "$" }
      )
    );

    const priceValues = (p.price_series || []).map(pt => pt.close);
    row.appendChild(
      bigChartCard(
        `${p.instrument} Price (real market)`,
        priceValues,
        { valuePrefix: "$" }
      )
    );

    container.appendChild(row);
  });
}

async function refresh() {
  try {
    const res = await fetch("/api/state", {cache: "no-store"});
    if (!res.ok) throw new Error("HTTP " + res.status);
    const s = await res.json();
    document.getElementById("err").style.display = "none";

    renderSummary(s.portfolio_summary);
    renderBigCharts(s.portfolios);

    const pfBody = document.querySelector("#portfolios tbody");
    pfBody.innerHTML = "";
    s.portfolios.forEach(p => {
      const sideCell = td(p.position_qty > 0 ? "LONG" : p.position_qty < 0 ? "SHORT" : "flat");
      sideCell.className = p.position_qty > 0 ? "ok" : p.position_qty < 0 ? "warn" : "muted";
      const retCell = td(p.return_pct.toFixed(2) + "%");
      retCell.className = p.return_pct > 0 ? "ok" : (p.return_pct < 0 ? "bad" : "muted");
      const curveCell = document.createElement("td");
      curveCell.appendChild(sparkline(p.equity_curve));
      const riskCell = document.createElement("td");
      if (p.risk && p.risk.tripped) {
        const pill = document.createElement("span");
        pill.className = "pill " + (p.risk.severity === "hard" ? "rejected" : "promoted");
        pill.style.background = p.risk.severity === "hard" ? "#3a1212" : "#3a2a12";
        pill.style.color = p.risk.severity === "hard" ? "#f87171" : "#facc15";
        pill.textContent = p.risk.severity === "hard" ? "HARD HALT" : "SOFT TRIPPED";
        pill.title = p.risk.reason || "";
        riskCell.appendChild(pill);
      } else {
        const ok = document.createElement("span");
        ok.className = "muted";
        ok.textContent = "ok";
        riskCell.appendChild(ok);
      }
      pfBody.appendChild(row([
        td(p.predictor), td(p.instrument), sideCell,
        td("$" + p.cash.toFixed(2)), td(p.position_qty.toFixed(6)),
        td("$" + p.equity.toFixed(2)), retCell, td(p.trade_count), curveCell, riskCell,
      ]));
    });
    if (s.portfolios.length === 0) {
      pfBody.appendChild(row([td("no paper trading has run yet -- run: python -m andy_trader.portfolio --predictor <name> --instrument <inst>"), td(""), td(""), td(""), td(""), td(""), td(""), td(""), td(""), td("")]));
    }

    const healthBody = document.querySelector("#health tbody");
    healthBody.innerHTML = "";
    s.collector_health.series.forEach(row_ => {
      const ageCell = td(fmtAge(row_.age_seconds));
      ageCell.className = row_.stale ? "bad" : "ok";
      const dataAgeCell = td(fmtAge(row_.data_age_seconds));
      dataAgeCell.className = row_.data_stale ? "bad" : "ok";
      healthBody.appendChild(row([
        td(row_.instrument), td(row_.interval), td(fmtTime(row_.last_seen_at)), ageCell,
        td(fmtTime(row_.latest_bar_at)), dataAgeCell,
      ]));
    });

    const pricesBody = document.querySelector("#prices tbody");
    pricesBody.innerHTML = "";
    s.latest_prices.forEach(p => {
      pricesBody.appendChild(row([td(p.instrument), td(p.interval), td(p.close), td(fmtTime(p.open_time))]));
    });

    const sbBody = document.querySelector("#scoreboard tbody");
    sbBody.innerHTML = "";
    const excluded = Object.values(s.score_exclusions).reduce((a, b) => a + b, 0);
    document.getElementById("score-quality").textContent = excluded
      ? excluded + " stale-reference calls excluded by the 90m quality gate"
      : "No stale-reference calls excluded";
    Object.keys(s.scoreboard).sort().forEach(name => {
      const r = s.scoreboard[name];
      if (r.error) { sbBody.appendChild(row([td(name), td("-"), td(r.error), td("-")])); return; }
      const skillCell = td(r.degenerate ? "degenerate" : r.brier_skill_score.toFixed(4));
      skillCell.className = r.degenerate ? "muted" : (r.brier_skill_score > 0 ? "ok" : "bad");
      sbBody.appendChild(row([td(name), td(r.count), skillCell, td((r.hit_rate*100).toFixed(1)+"%")]));
    });

    const regBody = document.querySelector("#registry tbody");
    regBody.innerHTML = "";
    s.registry.forEach(m => {
      const verdict = document.createElement("td");
      const pill = document.createElement("span");
      pill.className = "pill " + (m.promoted ? "promoted" : "rejected");
      pill.textContent = m.promoted ? "PROMOTED" : "REJECTED";
      verdict.appendChild(pill);
      regBody.appendChild(row([
        td(m.model_id), td(fmtTime(m.trained_at)),
        td(m.holdout_brier_skill.toFixed(4)), td(m.base_rate_brier_skill.toFixed(4)),
        verdict,
      ]));
    });
    if (s.registry.length === 0) {
      regBody.appendChild(row([td("no model has been trained yet"), td(""), td(""), td(""), td("")]));
    }

    const predBody = document.querySelector("#predictions tbody");
    predBody.innerHTML = "";
    s.recent_predictions.forEach(p => {
      let status;
      if (p.settled_at) {
        const correct = (p.outcome_up === 1) === (p.probability_up >= 0.5);
        status = document.createElement("span");
        status.textContent = p.outcome_up === 1 ? "settled: UP" : "settled: DOWN";
        status.className = correct ? "ok" : "bad";
      } else {
        status = document.createElement("span");
        status.textContent = "pending";
        status.className = "warn";
      }
      const statusCell = document.createElement("td");
      statusCell.appendChild(status);
      predBody.appendChild(row([
        td(p.predictor), td(p.instrument), td(p.horizon),
        td(p.probability_up.toFixed(3)), td(fmtTime(p.created_at)), statusCell,
      ]));
    });

    document.getElementById("updated").textContent = "updated " + fmtTime(s.generated_at);
  } catch (e) {
    const err = document.getElementById("err");
    err.textContent = "Could not reach the dashboard server: " + e.message;
    err.style.display = "block";
  }
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    database_path: Path = None  # set by run()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep stdout quiet; this is a local monitor, not a service

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path == "":
            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            try:
                with connect(self.database_path) as connection:
                    state = build_dashboard_state(connection)
                body = json.dumps(_json_safe(state)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # noqa: BLE001 - surface any failure to the page
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def run(database_path: Path, port: int = DEFAULT_PORT) -> None:
    _Handler.database_path = database_path
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"Andy Trader monitor: http://127.0.0.1:{port}  (database: {database_path})")
    print("Local only -- not reachable from the network. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", help="Override database path")
    args = parser.parse_args(argv)

    load_env_file(REPO_ROOT / ".env")
    db_path = Path(args.database) if args.database else default_database_path()
    run(db_path, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
