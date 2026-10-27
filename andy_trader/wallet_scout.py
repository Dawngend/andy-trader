"""Read-only public Polymarket account profiles."""

from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from uuid import uuid4

from andy_trader.complete_set import _http_json
from andy_trader.store import connect

API = "https://data-api.polymarket.com"
REQUEST_DELAY = 0.3
MAX_FAILURES = 3
PAGE_SIZE = 50
MAX_CLOSED_ROWS = 10_000
PERIODS = ("DAY", "WEEK", "MONTH", "ALL")
MARKET_RULES = (
    ("up_down", r"\bup\s*(?:or|/)\s*down\b|\bup/down\b"),
    ("price_target", r"\b(?:dip|reach|hit)\b"),
    ("above_below", r"\b(?:above|below)\b"),
    ("sports", r"\bversus\b|\bvs\.?(?=\s|$)"),
)
PRICE_BANDS = (("under_0.50", .50), ("0.50_to_0.80", .80),
               ("0.80_to_0.95", .95), ("0.95_to_0.99", .99),
               ("0.99_and_above", math.inf))
INVENTORY_SHARE = .20
SPLIT_MERGE_SHARE = .10
BOT_BUYS_PER_HOUR = 20
MIN_POSITIONS = 200
BOND_COST_SHARE = .60
BOND_WIN_RATE = .90
HIGH_ENTRY = .80
LONGSHOT_ENTRY = .30
STRATEGY_SHARE = .40
RECENT_HIGH_ENTRY = .95


class PublicReader:
    def __init__(self):
        self.failures = []

    def get(self, path, **params):
        url = f"{API}/{path}?{urlencode(params)}"
        while len(self.failures) < MAX_FAILURES:
            time.sleep(REQUEST_DELAY)
            try:
                result = _http_json(url, 30)
                rows = result.get("data") if isinstance(result, dict) else result
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError("Expected a list of public data rows")
                return rows
            except Exception as exc:
                self.failures.append(f"{path}: {exc}")
        raise RuntimeError("; ".join(self.failures))


def fetch_leaderboard(top, period):
    # The v2 leaderboard pages by cursor only (no offset), so ask for everything in one request.
    rows = PublicReader().get("v2/leaderboard", timePeriod=period, orderBy="PNL", limit=top)
    return [{"address": row["user_id"], "user_name": row.get("user_name", ""),
             "rank": int(row["rank"]), "leaderboard_pnl": float(row["pnl"]),
             "leaderboard_volume": float(row["volume"])} for row in rows[:top]]


def fetch_closed_positions(reader, address, max_pages):
    rows = []
    for page in range(min(max_pages, MAX_CLOSED_ROWS // PAGE_SIZE)):
        batch = reader.get("closed-positions", user=address, limit=PAGE_SIZE,
                           offset=page * PAGE_SIZE, sortBy="TIMESTAMP", sortDirection="DESC")
        if not batch:
            break
        rows.extend(batch)
    return rows


def divide(value, total):
    return value / total if total else None


def market_type(title):
    for name, pattern in MARKET_RULES:
        if re.search(pattern, title, re.IGNORECASE):
            return name
    return "other"


def price_band(price):
    return next(name for name, upper in PRICE_BANDS if price < upper)


def summarize(rows):
    return {"count": len(rows), "pnl": sum(row["pnl"] for row in rows),
            "cost": sum(row["cost"] for row in rows),
            "win_rate": divide(sum(row["pnl"] > 0 for row in rows), len(rows))}


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def strategy_label(profile):
    if "inventory_heavy" in profile["flags"] or profile["maker_rebate_count"]:
        return "market_maker"
    if (profile["high_entry_cost_share"] or 0) >= BOND_COST_SHARE and (profile["win_rate"] or 0) >= BOND_WIN_RATE:
        return "bond"
    if (profile["longshot_share"] or 0) >= STRATEGY_SHARE:
        return "longshot"
    if (profile["price_target_high_share"] or 0) >= STRATEGY_SHARE:
        return "price_target_no"
    return "mixed"


def profile_account(account, closed, activity):
    rows = [{"price": float(row["avgPrice"]),
             "cost": float(row["avgPrice"]) * float(row["totalBought"]),
             "pnl": float(row["realizedPnl"]), "timestamp": int(row["timestamp"]),
             "market": market_type(row["title"])} for row in closed]
    summary = summarize(rows)
    count = len(rows)
    times = [row["timestamp"] for row in rows]
    first, last = (min(times), max(times)) if times else (None, None)
    midpoint = (first + last) / 2 if times else 0
    wins = [row["pnl"] for row in rows if row["pnl"] > 0]
    losses = [row["pnl"] for row in rows if row["pnl"] < 0]
    average_win = divide(sum(wins), len(wins))
    average_loss = divide(sum(losses), len(losses))
    buys = [row for row in activity if row["type"] == "TRADE" and row.get("side") == "BUY"]
    activity_times = [int(row["timestamp"]) for row in activity]
    activity_first = min(activity_times) if activity_times else None
    activity_last = max(activity_times) if activity_times else None
    hours = (activity_last - activity_first) / 3600 if activity_times else 0
    profile = dict(account, sample_size=count, first_timestamp=first, last_timestamp=last,
                   pnl=summary["pnl"], cost=summary["cost"], win_rate=summary["win_rate"],
                   return_on_cost=divide(summary["pnl"], summary["cost"]),
                   average_win=average_win, average_loss=average_loss,
                   loss_to_win_ratio=divide(abs(average_loss), average_win) if average_loss is not None else None,
                   worst_loss=min(losses) if losses else 0,
                   median_position_size=percentile([row["cost"] for row in rows], .5),
                   p90_position_size=percentile([row["cost"] for row in rows], .9),
                   activity_count=len(activity), activity_first_timestamp=activity_first,
                   activity_last_timestamp=activity_last, buy_count=len(buys),
                   high_price_buy_share=divide(sum(float(row["price"]) >= RECENT_HIGH_ENTRY for row in buys), len(buys)),
                   buys_per_hour=divide(len(buys), hours),
                   first_half_pnl=sum(row["pnl"] for row in rows if row["timestamp"] < midpoint),
                   second_half_pnl=sum(row["pnl"] for row in rows if row["timestamp"] >= midpoint),
                   half_price_share=divide(sum(row["price"] == .5 for row in rows), count),
                   split_merge_share=divide(sum(row["type"] in ("SPLIT", "MERGE") for row in activity), len(activity)),
                   maker_rebate_count=sum(row["type"] == "MAKER_REBATE" for row in activity),
                   high_entry_cost_share=divide(sum(row["cost"] for row in rows if row["price"] >= HIGH_ENTRY), summary["cost"]),
                   longshot_share=divide(sum(row["price"] < LONGSHOT_ENTRY for row in rows), count),
                   price_target_high_share=divide(sum(row["market"] == "price_target" and row["price"] >= HIGH_ENTRY for row in rows), count))
    profile["price_bands"] = {name: summarize([row for row in rows if price_band(row["price"]) == name]) for name, _ in PRICE_BANDS}
    profile["market_types"] = {name: summarize([row for row in rows if row["market"] == name]) for name in [rule[0] for rule in MARKET_RULES] + ["other"]}
    profile["flags"] = profile_flags(profile)
    profile["label"] = strategy_label(profile)
    return profile


def profile_flags(profile):
    rules = {
        "inventory_heavy": (profile["half_price_share"] or 0) >= INVENTORY_SHARE or (profile["split_merge_share"] or 0) >= SPLIT_MERGE_SHARE,
        "bot_like": (profile["buys_per_hour"] or 0) > BOT_BUYS_PER_HOUR,
        "thin_sample": profile["sample_size"] < MIN_POSITIONS,
        "one_sided_period": (profile["first_half_pnl"] > 0) != (profile["second_half_pnl"] > 0),
    }
    return [name for name, enabled in rules.items() if enabled]


def create_tables(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS wallet_profiles (
        run_id TEXT, run_time TEXT, period TEXT, address TEXT, user_name TEXT, rank INTEGER,
        leaderboard_pnl REAL, leaderboard_volume REAL, sample_size INTEGER,
        first_timestamp INTEGER, last_timestamp INTEGER, pnl REAL, cost REAL,
        win_rate REAL, return_on_cost REAL, average_win REAL, average_loss REAL,
        loss_to_win_ratio REAL, worst_loss REAL, median_position_size REAL, p90_position_size REAL,
        activity_count INTEGER, activity_first_timestamp INTEGER, activity_last_timestamp INTEGER,
        buy_count INTEGER, high_price_buy_share REAL, buys_per_hour REAL,
        first_half_pnl REAL, second_half_pnl REAL, half_price_share REAL, split_merge_share REAL,
        maker_rebate_count INTEGER, high_entry_cost_share REAL, longshot_share REAL,
        price_target_high_share REAL, price_bands TEXT, market_types TEXT, flags TEXT, label TEXT,
        PRIMARY KEY (run_id, address))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS wallet_scout_failures (
        run_id TEXT, address TEXT, reason TEXT)""")


def save_profile(connection, profile):
    values = {key: json.dumps(value) if isinstance(value, (list, dict)) else value
              for key, value in profile.items()}
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO wallet_profiles ({columns}) VALUES ({placeholders})", tuple(values.values()))


def display_number(value, percent=False):
    if value is None:
        return "N/A"
    return f"{value:.1%}" if percent else f"{value:,.2f}"


def public_name(profile):
    name = re.sub(r"0x[0-9a-fA-F]{40}", lambda m: m[0][:8] + "..." + m[0][-4:], profile["user_name"])
    name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    name = name.replace("|", "&#124;").replace("\n", " ").replace("\r", " ")
    address = profile["address"]
    return f"{name} ({address[:8]}...{address[-4:]})"


def timestamp_text(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else "N/A"


def write_report(path, profiles, run_id, run_time, period, failure_count):
    lines = ["# Wallet Scout Report", "", f"Run: {run_id} | UTC: {run_time} | Leaderboard: {period}", "",
             "| User Name | Label | Flags | Sample Size | Win Rate | Return On Cost | Loss To Win Ratio | Worst Loss | First Half PnL | Second Half PnL |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for profile in sorted(profiles, key=lambda row: row["rank"]):
        cells = [public_name(profile), profile["label"], ", ".join(profile["flags"]) or "none", str(profile["sample_size"])]
        cells += [display_number(profile[key], key in ("win_rate", "return_on_cost")) for key in
                  ("win_rate", "return_on_cost", "loss_to_win_ratio", "worst_loss", "first_half_pnl", "second_half_pnl")]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "How to read this: compare returns with loss size and the flags. Loss to win ratio is how many average wins one average loss wipes out. N/A means the sample cannot define the number. Labels are rule-based descriptions, not recommendations.", "",
              "Sample-window caveat: closed positions are only the latest rows, so lifetime pnl can be much larger than the sample's. The leaderboard period does not filter these rows. Halves divide elapsed sample time at its midpoint; ties go in the second half. Activity is the latest 500 rows, with its own window.", "",
              f"Accounts with recorded failures: {failure_count}. Exhausted or invalid accounts are omitted; see wallet_scout_failures.", "", "Sample windows (UTC):"]
    for profile in sorted(profiles, key=lambda row: row["rank"]):
        lines.append(f"- {public_name(profile)}: closed {timestamp_text(profile['first_timestamp'])} to {timestamp_text(profile['last_timestamp'])}; activity {timestamp_text(profile['activity_first_timestamp'])} to {timestamp_text(profile['activity_last_timestamp'])}.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_scout(database, report, top=50, period="MONTH", max_pages=40):
    if top < 1 or max_pages < 1 or period not in PERIODS:
        raise ValueError("top and max_pages must be positive; period must be DAY, WEEK, MONTH or ALL")
    run_id, run_time = uuid4().hex, datetime.now(UTC).isoformat()
    profiles = []
    failures = 0
    connection = connect(Path(database))
    try:
        create_tables(connection)
        connection.commit()
        accounts = fetch_leaderboard(top, period)
        for account in accounts:
            reader = PublicReader()
            try:
                closed = fetch_closed_positions(reader, account["address"], max_pages)
                activity = reader.get("activity", user=account["address"], limit=500)
                profile = profile_account(account, closed, activity)
            except Exception as exc:
                reason = "; ".join(reader.failures) if reader.failures else str(exc)
                connection.execute("INSERT INTO wallet_scout_failures VALUES (?, ?, ?)", (run_id, account["address"], reason))
                failures += 1
            else:
                profile.update(run_id=run_id, run_time=run_time, period=period)
                save_profile(connection, profile)
                profiles.append(profile)
                if reader.failures:
                    connection.execute("INSERT INTO wallet_scout_failures VALUES (?, ?, ?)", (run_id, account["address"], "; ".join(reader.failures)))
                    failures += 1
            connection.commit()
        write_report(report, profiles, run_id, run_time, period, failures)
    finally:
        connection.close()
    return run_id
