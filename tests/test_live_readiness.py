from datetime import UTC, datetime, timedelta
from pathlib import Path

from andy_trader.complete_set import (
    get_or_create_paper_account,
    initialize_paper_account,
)
from andy_trader.live_readiness import evaluate_live_readiness
from andy_trader.store import connect


def _check(report, name: str):
    return next(check for check in report.checks if check.name == name)


def test_empty_database_fails_closed(tmp_path: Path) -> None:
    with connect(tmp_path / "empty.db") as connection:
        report = evaluate_live_readiness(connection, geoblock=None)

    assert report.ready is False
    assert _check(report, "execution-host eligibility").passed is False
    assert _check(report, "market coverage").passed is False
    assert _check(report, "confirmed shadow sample").passed is False
    assert _check(report, "authenticated execution adapter").passed is False


def test_a_blocked_execution_host_is_always_a_blocker(tmp_path: Path) -> None:
    with connect(tmp_path / "blocked.db") as connection:
        report = evaluate_live_readiness(
            connection,
            geoblock={"blocked": True, "country": "US", "region": "OR"},
            minimum_observation_days=1,
            minimum_confirmed_settled_trades=1,
        )

    check = _check(report, "execution-host eligibility")
    assert check.passed is False
    assert "blocked=True" in check.evidence


def test_legacy_single_snapshot_trades_do_not_count_as_confirmed(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "legacy.db") as connection:
        get_or_create_paper_account(connection)
        connection.execute(
            "INSERT INTO complete_set_paper_trades "
            "(round_id, opened_at, cost, fee, total_debit, target_notional, settled_at, outcome, payout, pnl) "
            "VALUES ('legacy', '2026-09-01T00:00:00+00:00', 9.5, 0.2, 9.7, 10, "
            "'2026-09-01T00:05:00+00:00', 'up', 10, 0.3)"
        )
        connection.commit()
        report = evaluate_live_readiness(
            connection,
            geoblock={"blocked": False, "country": "PH", "region": "NCR"},
            minimum_observation_days=1,
            minimum_confirmed_settled_trades=1,
        )

    assert _check(report, "confirmed shadow sample").passed is False
    assert "0 settled re-quoted" in _check(report, "confirmed shadow sample").evidence


def test_every_gate_must_pass_even_with_enough_data(tmp_path: Path) -> None:
    with connect(tmp_path / "ready.db") as connection:
        initialize_paper_account(connection)
        start = datetime(2026, 9, 1, tzinfo=UTC)
        for offset in range(14):
            connection.execute(
                "INSERT INTO complete_set_observations "
                "(round_id, observed_at, target_notional, up_fill_shares, down_fill_shares) "
                "VALUES (?, ?, 10, 0, 0)",
                (f"round-{offset}", (start + timedelta(days=offset)).isoformat()),
            )
        for offset in range(100):
            connection.execute(
                "INSERT INTO complete_set_paper_trades "
                "(round_id, opened_at, cost, fee, total_debit, target_notional, "
                " settled_at, outcome, payout, pnl, confirmation_net_combined_cost) "
                "VALUES (?, ?, 9.5, 0.2, 9.7, 10, ?, 'up', 10, 0.3, 0.97)",
                (
                    f"trade-{offset}",
                    (start + timedelta(minutes=offset)).isoformat(),
                    (start + timedelta(minutes=offset + 5)).isoformat(),
                ),
            )
        connection.commit()
        report = evaluate_live_readiness(
            connection,
            geoblock={"blocked": False, "country": "PH", "region": "NCR"},
            live_execution_adapter_verified=True,
            atomic_two_leg_failure_handling_verified=True,
        )

    assert report.ready is True
    assert all(check.passed for check in report.checks)
