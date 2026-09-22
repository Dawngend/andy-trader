from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from andy_trader.tradingview import (
    TradingViewError,
    collect_snapshot,
    main,
)


def _api_root(tmp_path: Path) -> Path:
    root = tmp_path / "TradingView-API"
    root.mkdir()
    (root / "main.js").write_text("module.exports = {};\n", encoding="utf-8")
    return root


def _payload() -> dict[str, object]:
    return {
        "source": "tradingview",
        "symbol": "BINANCE:BTCUSDT",
        "timeframe": "60",
        "fetched_at": "2026-09-22T05:00:00.000Z",
        "bars": [
            {
                "time": 1_795_000_000,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 42.0,
                "potentially_open": True,
            }
        ],
    }


def test_collects_validated_snapshot_without_forwarding_secrets(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, json.dumps(_payload()), "")

    result = collect_snapshot(
        api_root=_api_root(tmp_path),
        bars=1,
        environ={
            "PATH": "safe-path",
            "SYSTEMROOT": "safe-root",
            "TYPESAFE_API_KEY": "must-not-leak",
            "SESSION": "must-not-leak",
        },
        runner=fake_runner,
    )

    assert result["bars"][0]["close"] == 101.0
    assert seen["env"] == {"PATH": "safe-path", "SYSTEMROOT": "safe-root"}
    assert seen["command"][0] == "node"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbol": "BTCUSDT"}, "symbol must look like"),
        ({"timeframe": "2h"}, "unsupported timeframe"),
        ({"bars": 0}, "bars must be between"),
    ],
)
def test_rejects_invalid_requests(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    arguments: dict[str, object] = {"api_root": _api_root(tmp_path)}
    arguments.update(overrides)
    with pytest.raises(TradingViewError, match=message):
        collect_snapshot(**arguments)


def test_rejects_invalid_market_data(tmp_path: Path) -> None:
    payload = _payload()
    payload["bars"][0]["low"] = 103.0

    def fake_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    with pytest.raises(TradingViewError, match="low above high"):
        collect_snapshot(api_root=_api_root(tmp_path), runner=fake_runner)


def test_cli_fetches_without_an_acknowledgement_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("andy_trader.tradingview.collect_snapshot", lambda **_kwargs: _payload())

    assert main(["--api-root", str(_api_root(tmp_path)), "--bars", "1"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["source"] == "tradingview"
    assert result["bars"][0]["close"] == 101.0
