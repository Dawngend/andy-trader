# Andy Trader cross-check runner (local only, not part of upstream).
# Copy of the file used inside evan-kolberg/prediction-market-backtesting at commit c76e77a.
# To rerun: place it in that repo's backtests/private/ and run
#   ANDY_FEES_IN_SIGNAL=1 uv run python -m backtests.private.andy_pair_fee_compare
# Same BookBinaryPairArbitrageStrategy config as backtests/polymarket_btc_5m_pair_arbitrage.py,
# over one full hour of BTC 5m rounds, with include_taker_fees_in_signal taken from the
# ANDY_FEES_IN_SIGNAL environment variable ("1" or "0").

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os

from backtests._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

WINDOW_START = datetime.fromisoformat(os.environ.get("ANDY_WINDOW_START", "2026-05-29T14:00:00+00:00"))
WINDOW_COUNT = int(os.environ.get("ANDY_WINDOW_COUNT", "12"))
FEES_IN_SIGNAL = os.environ.get("ANDY_FEES_IN_SIGNAL", "1") == "1"
SIZE = timedelta(minutes=5)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def run() -> None:
    from prediction_market_extensions.backtesting._execution_config import (
        ExecutionModelConfig,
        StaticLatencyConfig,
    )
    from prediction_market_extensions.backtesting._experiments import (
        build_replay_experiment,
        run_experiment,
    )
    from prediction_market_extensions.backtesting._prediction_market_backtest import (
        MarketReportConfig,
    )
    from prediction_market_extensions.backtesting._prediction_market_runner import (
        MarketDataConfig,
    )
    from prediction_market_extensions.backtesting._replay_specs import BookReplay
    from prediction_market_extensions.backtesting.data_sources import Book, PMXT, Polymarket

    replays = []
    for index in range(WINDOW_COUNT):
        start = WINDOW_START + index * SIZE
        slug = f"btc-updown-5m-{int(start.timestamp())}"
        for token_index in (0, 1):
            replays.append(
                BookReplay(
                    market_slug=slug,
                    token_index=token_index,
                    start_time=_iso(start),
                    end_time=_iso(start + SIZE),
                    metadata={"sim_label": f"{slug}-{'up' if token_index == 0 else 'down'}"},
                )
            )
    tag = "fees" if FEES_IN_SIGNAL else "nofees"
    run_experiment(
        build_replay_experiment(
            name=f"andy_pair_fee_compare_{tag}",
            description="BTC 5m pair entries, taker fees in signal on/off",
            data=MarketDataConfig(
                platform=Polymarket,
                data_type=Book,
                vendor=PMXT,
                sources=("archive:r2v2.pmxt.dev", "archive:r2.pmxt.dev"),
            ),
            replays=tuple(replays),
            strategy_configs=[
                {
                    "strategy_path": "strategies:BookBinaryPairArbitrageStrategy",
                    "config_path": "strategies:BookBinaryPairArbitrageConfig",
                    "config": {
                        "instrument_ids": "__ALL_SIM_INSTRUMENT_IDS__",
                        "trade_size": Decimal("5"),
                        "min_net_edge": 0.0,
                        "max_total_cost": 1.0,
                        "max_leg_price": 0.985,
                        "max_spread": 0.080,
                        "max_expected_slippage": 0.015,
                        "min_visible_size": 5.0,
                        "max_entries_per_pair": 1,
                        "reentry_cooldown_updates": 25,
                        "pairing_mode": "sequential",
                        "hold_to_resolution": True,
                        "include_taker_fees_in_signal": FEES_IN_SIGNAL,
                    },
                }
            ],
            initial_cash=1_000.0,
            probability_window=256,
            min_book_events=25,
            min_price_range=0.0,
            execution=ExecutionModelConfig(
                queue_position=True,
                latency_model=StaticLatencyConfig(
                    base_latency_ms=75.0,
                    insert_latency_ms=10.0,
                    update_latency_ms=5.0,
                    cancel_latency_ms=5.0,
                ),
            ),
            report=MarketReportConfig(
                count_key="book_events",
                count_label="Book Events",
                pnl_label="PnL (pUSD)",
                market_key="sim_label",
                summary_report=False,
            ),
            empty_message="No sims met the book requirements.",
            return_summary_series=False,
        )
    )


if __name__ == "__main__":
    run()
