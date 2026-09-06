from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

import andy_trader.complete_set as complete_set
from andy_trader.complete_set import (
    CompleteSetError,
    _http_json,
    collect_current_round,
    observe_complete_set,
    record_complete_set_observation,
    summarize_history,
    walk_ask_book,
)
from andy_trader.store import connect


def _book(*levels: tuple[float, float]) -> dict[str, object]:
    return {
        "asks": [{"price": str(price), "size": str(size)} for price, size in levels],
        "bids": [],
    }


def test_pure_observation_prices_complementary_books_without_network() -> None:
    observation = observe_complete_set(
        "btc-updown-5m-1788652800",
        "2026-09-06T00:00:01+00:00",
        _book((0.48, 100)),
        _book((0.49, 100)),
        target_notional=10,
    )

    assert observation.up_best_ask == pytest.approx(0.48)
    assert observation.down_best_ask == pytest.approx(0.49)
    assert observation.up_best_ask_depth_shares == pytest.approx(100)
    assert observation.up_best_ask_depth_notional == pytest.approx(48)
    assert observation.naive_combined_cost == pytest.approx(0.97)
    assert observation.combined_cost == pytest.approx(0.97)
    assert observation.mispriced is True
    assert observation.unmeasurable_reason is None


def test_depth_walk_exposes_a_naive_quote_that_cannot_fill_at_that_price() -> None:
    up_fill = walk_ask_book(_book((0.55, 7), (0.48, 3)), 10)
    assert up_fill.best_ask == pytest.approx(0.48)
    assert up_fill.best_ask_depth_shares == pytest.approx(3)
    assert up_fill.best_ask_depth_notional == pytest.approx(1.44)
    assert up_fill.fill_cost == pytest.approx(5.29)
    assert up_fill.complete is True

    observation = observe_complete_set(
        "btc-updown-5m-1788652800",
        "2026-09-06T00:00:02+00:00",
        _book((0.55, 7), (0.48, 3)),
        _book((0.50, 10)),
        target_notional=10,
    )
    assert observation.naive_combined_cost == pytest.approx(0.98)
    assert observation.combined_cost == pytest.approx(1.029)
    assert observation.mispriced is False


def test_no_ask_on_one_side_is_unmeasurable_not_not_mispriced() -> None:
    observation = observe_complete_set(
        "btc-updown-5m-1788652800",
        "2026-09-06T00:00:03+00:00",
        _book(),
        _book((0.52, 100)),
    )

    assert observation.up_best_ask is None
    assert observation.naive_combined_cost is None
    assert observation.combined_cost is None
    assert observation.mispriced is None
    assert observation.unmeasurable_reason == "Up filled 0/10 shares"


def test_insufficient_depth_is_unmeasurable_even_with_two_best_quotes() -> None:
    observation = observe_complete_set(
        "btc-updown-5m-1788652800",
        "2026-09-06T00:00:04+00:00",
        _book((0.40, 5)),
        _book((0.50, 100)),
    )

    assert observation.naive_combined_cost == pytest.approx(0.90)
    assert observation.up_fill_shares == pytest.approx(5)
    assert observation.combined_cost is None
    assert observation.mispriced is None
    assert "Up filled 5/10 shares" in observation.unmeasurable_reason


def test_collector_resolves_current_round_tokens_and_uses_full_books() -> None:
    calls: list[str] = []

    def fake_http(url: str, timeout: float) -> object:
        calls.append(url)
        assert timeout == 3.0
        if "gamma-api" in url:
            return [
                {
                    "markets": [
                        {
                            "outcomes": '["Up", "Down"]',
                            "clobTokenIds": '["up-token", "down-token"]',
                        }
                    ]
                }
            ]
        if "up-token" in url:
            return _book((0.48, 3), (0.55, 7))
        if "down-token" in url:
            return _book((0.50, 10))
        raise AssertionError(url)

    observation = collect_current_round(
        now=1788652919,
        timeout_seconds=3.0,
        target_notional=10,
        http=fake_http,
    )

    assert observation.round_id == "btc-updown-5m-1788652800"
    assert observation.combined_cost == pytest.approx(1.029)
    assert len(calls) == 3
    assert "slug=btc-updown-5m-1788652800" in calls[0]


def test_clob_no_order_book_response_becomes_an_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://clob.polymarket.com/book?token_id=missing"

    def no_book(*_args: object, **_kwargs: object) -> object:
        raise HTTPError(
            url,
            404,
            "Not Found",
            {},
            BytesIO(b'{"error":"No orderbook exists for the requested token id"}'),
        )

    monkeypatch.setattr(complete_set, "urlopen", no_book)

    assert _http_json(url, 3.0) == {"asks": [], "bids": []}


def test_collector_rejects_invalid_target_before_network() -> None:
    def unexpected_http(_url: str, _timeout: float) -> object:
        raise AssertionError("invalid configuration must fail before HTTP")

    with pytest.raises(CompleteSetError, match="target_notional must be positive"):
        collect_current_round(target_notional=0, http=unexpected_http)


def test_complete_set_table_is_strictly_append_only_for_the_same_round(tmp_path: Path) -> None:
    first = observe_complete_set(
        "same-round",
        "2026-09-06T00:00:01+00:00",
        _book((0.48, 100)),
        _book((0.49, 100)),
    )
    second = observe_complete_set(
        "same-round",
        "2026-09-06T00:00:02+00:00",
        _book((0.55, 100)),
        _book((0.50, 100)),
    )

    with connect(tmp_path / "observations.db") as connection:
        first_id = record_complete_set_observation(connection, first)
        second_id = record_complete_set_observation(connection, second)
        rows = connection.execute(
            "SELECT id, observed_at, combined_cost FROM complete_set_observations ORDER BY id"
        ).fetchall()
        report = summarize_history(connection)

    assert first_id != second_id
    assert len(rows) == 2
    assert rows[0]["observed_at"] == first.observed_at
    assert rows[0]["combined_cost"] == pytest.approx(0.97)
    assert rows[1]["observed_at"] == second.observed_at
    assert rows[1]["combined_cost"] == pytest.approx(1.05)
    assert report.observations == 2
    assert report.rounds_observed == 1
    assert report.two_sided_rounds == 1
    assert report.depth_measurable_rounds == 1
    assert report.naive_mispriced_rounds == 1
    assert report.mispriced_rounds == 1
    assert report.mispriced_costs == pytest.approx((0.97,))
