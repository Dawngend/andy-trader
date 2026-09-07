from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

import andy_trader.complete_set as complete_set
from andy_trader.complete_set import (
    CRYPTO_TAKER_FEE_RATE,
    CompleteSetError,
    PaperAccountError,
    _http_json,
    collect_current_round,
    get_or_create_paper_account,
    observe_complete_set,
    open_paper_trade,
    paper_account_summary,
    record_complete_set_observation,
    resolve_round_outcome,
    settle_due_paper_trades,
    summarize_history,
    taker_fee,
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


def test_taker_fee_matches_polymarkets_own_worked_example() -> None:
    """docs.polymarket.com/trading/fees states the Crypto-category peak fee is
    $1.75 per 100 shares at p=0.50. If this drifts from Polymarket's own
    number, every downstream net-of-fees figure is quietly wrong."""

    fee = taker_fee(100, 0.50, CRYPTO_TAKER_FEE_RATE)

    assert float(fee) == pytest.approx(1.75)


def test_taker_fee_is_symmetric_and_zero_at_the_edges() -> None:
    assert taker_fee(100, 0.30, CRYPTO_TAKER_FEE_RATE) == taker_fee(100, 0.70, CRYPTO_TAKER_FEE_RATE)
    assert float(taker_fee(100, 0.01, CRYPTO_TAKER_FEE_RATE)) == pytest.approx(0.0693, abs=1e-4)


def test_the_real_finding_a_gross_mispricing_the_fee_fully_consumes() -> None:
    """The actual shape found on this project's first night of live collection:
    combined_cost was $0.99 (looks mispriced), and Polymarket's own 7% crypto
    taker fee, charged on both legs, is enough by itself to erase it. Being
    statistically/structurally cheaper than $1 is not the same claim as being
    net-of-fees cheaper than $1, and a detector that only reports the first
    one would have told Dawn this was free money when it was not."""

    observation = observe_complete_set(
        "btc-updown-5m-real-example",
        "2026-09-06T23:00:00+00:00",
        _book((0.48, 100)),
        _book((0.51, 100)),
        target_notional=10,
    )

    assert observation.combined_cost == pytest.approx(0.99)
    assert observation.mispriced is True
    # fee = 10 * 0.07 * p * (1-p) per leg
    expected_fee = float(taker_fee(10, 0.48, CRYPTO_TAKER_FEE_RATE)) + float(
        taker_fee(10, 0.51, CRYPTO_TAKER_FEE_RATE)
    )
    assert observation.up_fee_cost + observation.down_fee_cost == pytest.approx(expected_fee)
    assert observation.net_combined_cost == pytest.approx(0.99 + expected_fee / 10)
    assert observation.net_mispriced is False


def test_a_large_enough_gap_survives_the_fee() -> None:
    """The other real shape from that same night: a handful of rounds were
    cheap enough (around $0.94-0.95) that the fee did not fully close the gap.
    The detector must be able to say yes here, not just no everywhere."""

    observation = observe_complete_set(
        "btc-updown-5m-survives",
        "2026-09-06T23:05:00+00:00",
        _book((0.50, 100)),
        _book((0.44, 100)),
        target_notional=10,
    )

    assert observation.combined_cost == pytest.approx(0.94)
    assert observation.net_mispriced is True
    assert observation.net_combined_cost < 1.0


def test_fee_is_walked_per_level_not_approximated_from_the_average_price() -> None:
    """p*(1-p) is concave, so pricing the whole fill at its average price would
    give a different (biased) number than summing each level's own fee. A fill
    spanning 0.30 for 5 shares and 0.60 for 5 shares must charge those two
    levels their own fee, not 10 shares at an average price of 0.45."""

    fill = walk_ask_book(_book((0.30, 5), (0.60, 5)), 10)

    exact = float(taker_fee(5, 0.30, CRYPTO_TAKER_FEE_RATE)) + float(
        taker_fee(5, 0.60, CRYPTO_TAKER_FEE_RATE)
    )
    approximated_from_average = float(taker_fee(10, 0.45, CRYPTO_TAKER_FEE_RATE))

    assert fill.fee_cost == pytest.approx(exact)
    assert fill.fee_cost != pytest.approx(approximated_from_average)


def test_an_unmeasurable_side_has_no_fee_either() -> None:
    """A fill that could not complete never actually executed, so it does not
    owe a taker fee on the shares it could not get -- None, not zero, since
    zero would misleadingly claim a real, costed, completed transaction."""

    observation = observe_complete_set(
        "btc-updown-5m-thin",
        "2026-09-06T23:10:00+00:00",
        _book((0.40, 5)),
        _book((0.50, 100)),
    )

    assert observation.up_fee_cost is None
    assert observation.net_combined_cost is None
    assert observation.net_mispriced is None


def test_migrating_a_pre_fee_database_adds_columns_without_touching_old_rows(
    tmp_path: Path,
) -> None:
    """The real production database already held 474 observations, recorded
    hours before the fee columns existed. Opening it again must add the new
    columns without rewriting a single previously-recorded fact."""
    import sqlite3

    database = tmp_path / "pre_fee.db"
    raw = sqlite3.connect(database)
    raw.execute(
        """
        CREATE TABLE complete_set_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            target_notional REAL NOT NULL,
            up_best_ask REAL, down_best_ask REAL,
            up_best_ask_depth_shares REAL, down_best_ask_depth_shares REAL,
            up_best_ask_depth_notional REAL, down_best_ask_depth_notional REAL,
            naive_combined_cost REAL,
            up_fill_shares REAL NOT NULL, down_fill_shares REAL NOT NULL,
            up_fill_cost REAL, down_fill_cost REAL,
            combined_cost REAL, mispriced INTEGER,
            unmeasurable_reason TEXT
        )
        """
    )
    raw.execute(
        "INSERT INTO complete_set_observations "
        "(round_id, observed_at, target_notional, up_fill_shares, down_fill_shares, "
        " combined_cost, mispriced) VALUES ('old-round', '2026-09-06T22:00:00+00:00', "
        "10.0, 10.0, 10.0, 0.99, 1)"
    )
    raw.commit()
    raw.close()

    with connect(database) as connection:
        row = connection.execute(
            "SELECT round_id, combined_cost, mispriced, net_combined_cost, net_mispriced "
            "FROM complete_set_observations WHERE round_id = 'old-round'"
        ).fetchone()

    assert row["combined_cost"] == pytest.approx(0.99)
    assert row["mispriced"] == 1
    assert row["net_combined_cost"] is None
    assert row["net_mispriced"] is None


def test_report_derives_net_mispricing_for_rows_older_than_the_fee_columns(
    tmp_path: Path,
) -> None:
    """The exact bug caught while reviewing the real overnight data: the report
    said '0 net mispriced' immediately after adding the fee columns, because
    all 474 real rows predated them and a bare COUNT over a NULL column reads
    as zero. Zero found and zero computed are different facts, and only the
    second one was true. The report must derive an approximate answer from the
    raw fields those old rows already have, not report a false zero."""
    import sqlite3

    database = tmp_path / "pre_fee_data.db"
    raw = sqlite3.connect(database)
    raw.execute(
        """
        CREATE TABLE complete_set_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id TEXT NOT NULL, observed_at TEXT NOT NULL, target_notional REAL NOT NULL,
            up_best_ask REAL, down_best_ask REAL,
            up_best_ask_depth_shares REAL, down_best_ask_depth_shares REAL,
            up_best_ask_depth_notional REAL, down_best_ask_depth_notional REAL,
            naive_combined_cost REAL,
            up_fill_shares REAL NOT NULL, down_fill_shares REAL NOT NULL,
            up_fill_cost REAL, down_fill_cost REAL,
            combined_cost REAL, mispriced INTEGER, unmeasurable_reason TEXT
        )
        """
    )
    # A round cheap enough (0.94, matching the real night's best case) to
    # survive the fee even under the approximation.
    raw.execute(
        "INSERT INTO complete_set_observations "
        "(round_id, observed_at, target_notional, up_best_ask, down_best_ask, "
        " up_fill_shares, down_fill_shares, up_fill_cost, down_fill_cost, "
        " combined_cost, mispriced) VALUES "
        "('old-cheap', '2026-09-06T22:00:00+00:00', 10.0, 0.50, 0.44, "
        " 10.0, 10.0, 5.0, 4.4, 0.94, 1)"
    )
    # A round only marginally under $1 (0.99, the real night's median case),
    # which the fee alone should erase.
    raw.execute(
        "INSERT INTO complete_set_observations "
        "(round_id, observed_at, target_notional, up_best_ask, down_best_ask, "
        " up_fill_shares, down_fill_shares, up_fill_cost, down_fill_cost, "
        " combined_cost, mispriced) VALUES "
        "('old-thin-margin', '2026-09-06T22:05:00+00:00', 10.0, 0.48, 0.51, "
        " 10.0, 10.0, 4.8, 5.1, 0.99, 1)"
    )
    raw.commit()
    raw.close()

    with connect(database) as connection:
        report = summarize_history(connection)

    assert report.mispriced_rounds == 2  # the gross, pre-fee count is unchanged
    assert report.net_mispriced_rounds == 1  # only the genuinely cheap one survives
    assert report.net_mispriced_costs
    assert report.net_mispriced_costs[0] < 1.0


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


def _event(*, closed: bool, outcomes=("Up", "Down"), prices=None) -> list:
    market: dict = {"outcomes": list(outcomes), "closed": closed}
    if prices is not None:
        market["outcomePrices"] = list(prices)
    return [{"markets": [market]}]


def _observation(round_id: str, *, up_price: float, down_price: float, target: float = 10.0):
    return observe_complete_set(
        round_id,
        "2026-09-07T00:00:00+00:00",
        _book((up_price, 100)),
        _book((down_price, 100)),
        target_notional=target,
    )


# --------------------------------------------------------------------------
# Resolution reading
# --------------------------------------------------------------------------


def test_resolve_round_outcome_reads_a_genuinely_settled_up_win() -> None:
    payload = _event(closed=True, prices=["1", "0"])
    assert resolve_round_outcome(payload) == "up"


def test_resolve_round_outcome_reads_a_genuinely_settled_down_win() -> None:
    payload = _event(closed=True, prices=["0", "1"])
    assert resolve_round_outcome(payload) == "down"


def test_resolve_round_outcome_is_none_while_still_open() -> None:
    payload = _event(closed=False, prices=["1", "0"])
    assert resolve_round_outcome(payload) is None


def test_resolve_round_outcome_is_none_when_closed_but_not_yet_priced() -> None:
    """Gamma can mark a market closed before it has published outcomePrices.
    That gap must read as unresolved, not as an accidental winner."""
    payload = _event(closed=True, prices=None)
    assert resolve_round_outcome(payload) is None


def test_resolve_round_outcome_refuses_an_ambiguous_result() -> None:
    """A genuine dispute or void round would not look like a clean 1/0 split.
    This must not guess a side from whichever price happens to be higher."""
    payload = _event(closed=True, prices=["0.5", "0.5"])
    assert resolve_round_outcome(payload) is None


# --------------------------------------------------------------------------
# Paper account lifecycle
# --------------------------------------------------------------------------


def test_a_fresh_account_starts_at_the_requested_balance(tmp_path: Path) -> None:
    with connect(tmp_path / "paper.db") as connection:
        account = get_or_create_paper_account(connection, starting_cash=50.0)
        again = get_or_create_paper_account(connection, starting_cash=999.0)

    assert account.cash == account.starting_cash == 50.0
    assert again.cash == 50.0


def test_a_gross_mispriced_but_not_net_mispriced_round_is_never_traded(tmp_path: Path) -> None:
    observation = _observation("btc-updown-5m-1", up_price=0.48, down_price=0.51)
    assert observation.mispriced is True
    assert observation.net_mispriced is False

    with connect(tmp_path / "paper.db") as connection:
        get_or_create_paper_account(connection, starting_cash=100.0)
        trade = open_paper_trade(connection, observation)
        summary = paper_account_summary(connection)

    assert trade is None
    assert summary["total_trades"] == 0
    assert summary["cash"] == 100.0


def test_a_net_mispriced_round_opens_a_position_debited_at_true_cost(tmp_path: Path) -> None:
    observation = _observation("btc-updown-5m-2", up_price=0.50, down_price=0.44)
    assert observation.net_mispriced is True

    with connect(tmp_path / "paper.db") as connection:
        get_or_create_paper_account(connection, starting_cash=100.0)
        trade = open_paper_trade(connection, observation)
        summary = paper_account_summary(connection)

    assert trade is not None
    assert trade.total_debit == pytest.approx(observation.combined_cost * 10 + trade.fee)
    assert trade.cost + trade.fee == pytest.approx(trade.total_debit)
    assert summary["cash"] == pytest.approx(100.0 - trade.total_debit)
    assert summary["open_trades"] == 1


def test_the_same_round_is_never_traded_twice(tmp_path: Path) -> None:
    observation = _observation("btc-updown-5m-3", up_price=0.50, down_price=0.44)

    with connect(tmp_path / "paper.db") as connection:
        get_or_create_paper_account(connection, starting_cash=100.0)
        first = open_paper_trade(connection, observation)
        second = open_paper_trade(connection, observation)
        summary = paper_account_summary(connection)

    assert first is not None
    assert second is None
    assert summary["total_trades"] == 1


def test_a_trade_the_account_cannot_afford_is_refused_not_partially_filled(
    tmp_path: Path,
) -> None:
    observation = _observation("btc-updown-5m-4", up_price=0.50, down_price=0.44)

    with connect(tmp_path / "paper.db") as connection:
        get_or_create_paper_account(connection, starting_cash=1.0)
        trade = open_paper_trade(connection, observation)
        summary = paper_account_summary(connection)

    assert trade is None
    assert summary["cash"] == 1.0
    assert summary["total_trades"] == 0


def test_settlement_credits_the_full_target_notional_on_a_real_win(tmp_path: Path) -> None:
    observation = _observation("btc-updown-5m-5", up_price=0.50, down_price=0.44)

    with connect(tmp_path / "paper.db") as connection:
        get_or_create_paper_account(connection, starting_cash=100.0)
        opened = open_paper_trade(connection, observation)

        def fake_http(url: str, _timeout: float) -> object:
            assert "btc-updown-5m-5" in url
            return _event(closed=True, prices=["1", "0"])

        result = settle_due_paper_trades(connection, http=fake_http)
        summary = paper_account_summary(connection)

    assert result == {"due": 1, "settled": 1, "unresolved": 0}
    assert summary["cash"] == pytest.approx(100.0 - opened.total_debit + 10.0)
    assert summary["settled_trades"] == 1
    assert summary["wins"] == 1
    assert summary["total_pnl"] == pytest.approx(10.0 - opened.total_debit)


def test_an_unresolved_round_stays_open_rather_than_being_guessed(tmp_path: Path) -> None:
    observation = _observation("btc-updown-5m-6", up_price=0.50, down_price=0.44)

    with connect(tmp_path / "paper.db") as connection:
        get_or_create_paper_account(connection, starting_cash=100.0)
        open_paper_trade(connection, observation)

        def still_open(_url: str, _timeout: float) -> object:
            return _event(closed=False, prices=None)

        result = settle_due_paper_trades(connection, http=still_open)
        summary = paper_account_summary(connection)

    assert result == {"due": 1, "settled": 0, "unresolved": 1}
    assert summary["open_trades"] == 1
    assert summary["settled_trades"] == 0


def test_settlement_is_idempotent_a_second_pass_does_not_pay_twice(tmp_path: Path) -> None:
    observation = _observation("btc-updown-5m-7", up_price=0.50, down_price=0.44)

    def resolved(_url: str, _timeout: float) -> object:
        return _event(closed=True, prices=["1", "0"])

    with connect(tmp_path / "paper.db") as connection:
        get_or_create_paper_account(connection, starting_cash=100.0)
        open_paper_trade(connection, observation)
        settle_due_paper_trades(connection, http=resolved)
        first_cash = paper_account_summary(connection)["cash"]
        second_result = settle_due_paper_trades(connection, http=resolved)
        second_cash = paper_account_summary(connection)["cash"]

    assert second_result == {"due": 0, "settled": 0, "unresolved": 0}
    assert second_cash == pytest.approx(first_cash)


def test_get_or_create_paper_account_rejects_a_non_positive_starting_balance(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "paper.db") as connection:
        with pytest.raises(PaperAccountError, match="starting_cash"):
            get_or_create_paper_account(connection, starting_cash=0.0)
