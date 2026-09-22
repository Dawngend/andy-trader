"""Validated TradingView OHLCV adapter for Andy Trader.

Dawn received authorization to use and complete this public API integration in
a workshop.  The adapter stays credential-free and validates every bar before
the collector can append it to Andy Trader's evidence store.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Mapping, Sequence

from .env import REPO_ROOT, load_env_file


BRIDGE_PATH = Path(__file__).with_name("_tradingview_bridge.js")
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_.-]+:[A-Z0-9_.-]+$")
ALLOWED_TIMEFRAMES = {
    "1", "3", "5", "15", "30", "45", "60", "120", "180", "240", "D", "W", "M"
}
SAFE_ENVIRONMENT_KEYS = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


class TradingViewError(RuntimeError):
    """Raised when the TradingView adapter cannot return a valid snapshot."""


def _minimal_subprocess_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Pass Node only operating-system basics, never account or API credentials."""

    allowed = {key.upper() for key in SAFE_ENVIRONMENT_KEYS}
    return {key: value for key, value in environ.items() if key.upper() in allowed}


def _validate_bar(raw: object, index: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise TradingViewError(f"bar {index} is not an object")

    required = ("time", "open", "high", "low", "close", "volume")
    missing = [name for name in required if name not in raw]
    if missing:
        raise TradingViewError(f"bar {index} is missing: {', '.join(missing)}")

    values: dict[str, float] = {}
    for name in required:
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TradingViewError(f"bar {index} field {name!r} is not numeric")
        number = float(value)
        if not math.isfinite(number):
            raise TradingViewError(f"bar {index} field {name!r} is not finite")
        values[name] = number

    if values["time"] <= 0:
        raise TradingViewError(f"bar {index} has an invalid timestamp")
    if values["low"] > values["high"]:
        raise TradingViewError(f"bar {index} has low above high")
    if not values["low"] <= values["open"] <= values["high"]:
        raise TradingViewError(f"bar {index} open is outside its range")
    if not values["low"] <= values["close"] <= values["high"]:
        raise TradingViewError(f"bar {index} close is outside its range")
    if values["volume"] < 0:
        raise TradingViewError(f"bar {index} has negative volume")

    return {
        "time": int(values["time"]),
        "open": values["open"],
        "high": values["high"],
        "low": values["low"],
        "close": values["close"],
        "volume": values["volume"],
        "potentially_open": bool(raw.get("potentially_open", index == 0)),
    }


def collect_snapshot(
    *,
    api_root: Path,
    symbol: str = "BINANCE:BTCUSDT",
    timeframe: str = "60",
    bars: int = 100,
    timeout_seconds: float = 15.0,
    node_executable: str = "node",
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Fetch and validate one TradingView OHLCV snapshot."""

    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise TradingViewError("symbol must look like EXCHANGE:TICKER using uppercase ASCII")
    if timeframe not in ALLOWED_TIMEFRAMES:
        allowed = ", ".join(sorted(ALLOWED_TIMEFRAMES))
        raise TradingViewError(f"unsupported timeframe {timeframe!r}; choose one of {allowed}")
    if not 1 <= bars <= 500:
        raise TradingViewError("bars must be between 1 and 500")
    if not 1.0 <= timeout_seconds <= 60.0:
        raise TradingViewError("timeout_seconds must be between 1 and 60")

    resolved_root = api_root.expanduser().resolve()
    if not (resolved_root / "main.js").is_file():
        raise TradingViewError(f"TradingView-API main.js not found under {resolved_root}")
    if not BRIDGE_PATH.is_file():
        raise TradingViewError(f"Node bridge is missing: {BRIDGE_PATH}")

    command = [
        node_executable,
        str(BRIDGE_PATH),
        str(resolved_root),
        symbol,
        timeframe,
        str(bars),
        str(int(timeout_seconds * 1000)),
    ]
    child_environment = _minimal_subprocess_environment(os.environ if environ is None else environ)
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 2.0,
            check=False,
            env=child_environment,
        )
    except FileNotFoundError as exc:
        raise TradingViewError(f"Node executable not found: {node_executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TradingViewError("TradingView request exceeded its deadline") from exc

    stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0:
        detail = completed.stderr.strip() or (stdout_lines[-1] if stdout_lines else "no error detail")
        raise TradingViewError(
            f"TradingView bridge exited {completed.returncode}: {detail}"
        )
    if not stdout_lines:
        raise TradingViewError("TradingView bridge returned no JSON")

    try:
        payload = json.loads(stdout_lines[-1])
    except json.JSONDecodeError as exc:
        raise TradingViewError("TradingView bridge returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("source") != "tradingview":
        raise TradingViewError("TradingView bridge returned an unexpected payload")
    if payload.get("symbol") != symbol or payload.get("timeframe") != timeframe:
        raise TradingViewError("TradingView bridge returned the wrong market")

    raw_bars = payload.get("bars")
    if not isinstance(raw_bars, list) or not raw_bars:
        raise TradingViewError("TradingView bridge returned no bars")
    payload["bars"] = [_validate_bar(raw, index) for index, raw in enumerate(raw_bars)]
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-root", type=Path, help="path to the cloned TradingView-API repo")
    parser.add_argument("--symbol", default="BINANCE:BTCUSDT")
    parser.add_argument("--timeframe", default="60")
    parser.add_argument("--bars", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--node", help="Node executable; defaults to TRADINGVIEW_NODE or node")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env_file(REPO_ROOT / ".env")
    api_root = args.api_root or (
        Path(os.environ["TRADINGVIEW_API_ROOT"])
        if os.environ.get("TRADINGVIEW_API_ROOT")
        else None
    )
    if api_root is None:
        parser.error("set TRADINGVIEW_API_ROOT or pass --api-root")
    node_executable = args.node or os.environ.get("TRADINGVIEW_NODE", "node")

    try:
        payload = collect_snapshot(
            api_root=api_root,
            symbol=args.symbol,
            timeframe=args.timeframe,
            bars=args.bars,
            timeout_seconds=args.timeout_seconds,
            node_executable=node_executable,
        )
    except TradingViewError as exc:
        print(f"TradingView error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
