from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from andy_trader.loss_review import (
    TABLES, build_report, group_by_end, loss_clusters, read_positions, row_tags, summarize, utc_time,
)
import run_loss_review as runner


def position(pnl=5.0, **changes):
    row = dict(market_id="market", title="Bitcoin Up or Down", side="Yes",
               entry_band="0.90 to 0.95", fill_price=0.95, shares=100,
               opened_at="2026-10-01T00:00:00+00:00", ends_at="2026-10-01T00:30:00Z",
               win=int(pnl >= 0) if pnl is not None else None, pnl=pnl)
    row.update(changes)
    return row


def create_database(path, books):
    with sqlite3.connect(path) as connection:
        for table, rows in books.items():
            connection.execute(f"""CREATE TABLE {table} (
                market_id TEXT, title TEXT, side TEXT, entry_band TEXT, fill_price REAL,
                shares REAL, opened_at TEXT, ends_at TEXT, win INTEGER, pnl REAL
            )""")
            for row in rows:
                connection.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   tuple(row.values()))


def test_overall_and_settled_filter(tmp_path):
    database = tmp_path / "paper.db"
    create_database(database, {TABLES[0]: [position(5), position(-95), position(None)],
                               TABLES[1]: [position(15), position(-85)]})
    rows, counts = read_positions(database)
    assert counts == dict.fromkeys(TABLES, 2)
    assert summarize(rows) == dict(settled=4, wins=2, losses=2, loss_rate=0.5,
                                   pnl=-160, average_loss=-90, average_win=10)
    report = build_report(rows, counts)
    assert "| 4 | 2 | 2 | 50.0% | -$160.00 | -$90.00 | $10.00 |" in report
    assert "Run time:" in report
    assert "bond_book_positions: 2" in report
    assert "price_target_book_positions: 2" in report


def test_flagged_tags_and_minimum_sample(tmp_path):
    database = tmp_path / "paper.db"
    rows = [position(-95, title="XRP Up or Down") for _ in range(20)]
    rows += [position(5, title="Bitcoin above $100") for _ in range(100)]
    rows += [position(-95, title="ZCash Up or Down") for _ in range(19)]
    create_database(database, {TABLES[0]: rows})
    report = build_report(*read_positions(database))
    tags = report.split("## By tag")[1].split("## Clusters")[0]
    assert "| asset | XRP | 20 | 20 | 100.0% | -$1,900.00 | FLAGGED |" in tags
    assert "| asset | Bitcoin | 100 | 0 | 0.0% | $500.00 |  |" in tags
    assert "ZCash" not in tags
    assert "Skip asset XRP: 20 settled" in report
    assert "would have been $0 if skipped." in report
    assert "In-sample hindsight: a forward paper test is needed before any rule is trusted." in report


def test_clusters_across_books(tmp_path):
    database = tmp_path / "paper.db"
    create_database(database, {
        TABLES[0]: [position(-95), position(-95, title="Alone", ends_at="2026-10-02T00:00:00Z")],
        TABLES[1]: [position(-90, title="XRP Up or Down"), position(5, title="ZCash above $100")],
    })
    rows, counts = read_positions(database)
    ends = group_by_end(rows)
    clusters = loss_clusters(ends)
    assert len(clusters) == 1
    assert clusters[0][2]["pnl"] == -180
    assert clusters[0][2]["losses"] == 2
    end_key = utc_time(rows[0]["ends_at"]).replace(microsecond=0)
    assert row_tags(rows[0], len(ends[end_key]))["same_end_count"] == "3 or more"
    report = build_report(rows, counts).split("## Clusters")[1]
    assert "Bitcoin Up or Down; XRP Up or Down; ZCash above $100" in report
    assert "| 3 | 2 | -$180.00 |" in report
    assert "Alone" not in report


def test_only_ten_worst_clusters():
    rows = []
    for day in range(1, 13):
        rows.extend([position(-day, ends_at=f"2026-10-{day:02d}T00:00:00Z")] * 2)
    clusters = loss_clusters(group_by_end(rows))
    assert len(clusters) == 10
    assert [stats["pnl"] for _, _, stats in clusters] == list(range(-24, -4, 2))


@pytest.mark.parametrize("table", [None, *TABLES])
def test_missing_tables(tmp_path, table):
    database = tmp_path / "paper.db"
    create_database(database, {table: [position()]} if table else {})
    rows, counts = read_positions(database)
    assert len(rows) == (1 if table else 0)
    assert counts == {name: int(name == table) for name in TABLES}
    report = build_report(rows, counts)
    assert "No tag stands out yet." in report
    assert "No clusters with two or more losses." in report


@pytest.mark.parametrize("hours, expected", [
    (0.5, "under 1h"), (1, "1h to 6h"), (6, "1h to 6h"),
    (6.1, "6h to 24h"), (24, "6h to 24h"), (25, "over 24h"),
])
def test_time_buckets(hours, expected):
    end = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(hours=hours)
    assert row_tags(position(ends_at=end.isoformat()), 1)["time_to_end"] == expected


@pytest.mark.parametrize("hour", range(24))
def test_utc_open_hour(hour):
    opened = datetime(2026, 10, 1, hour, tzinfo=UTC)
    tags = row_tags(position(opened_at=opened.isoformat()), 2)
    start = hour // 4 * 4
    assert tags["open_hour"] == f"{start:02d}-{start + 3:02d}"
    assert tags["same_end_count"] == "2"


def test_tags_convert_timezone_and_reuse_slice():
    tags = row_tags(position(title="ZCash above $100", opened_at="2026-10-01T08:00:00+08:00"), 1)
    assert tags == dict(slice="above_below", entry_band="0.90 to 0.95", asset="ZCash",
                        time_to_end="under 1h", open_hour="00-03", same_end_count="1")


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_and_database_unchanged(tmp_path, capsys, dry_run):
    database = tmp_path / "paper #1.db"
    output = tmp_path / "research" / "loss_review.md"
    create_database(database, {TABLES[0]: [position(-95)]})
    before = database.read_bytes(), database.stat().st_mtime_ns
    args = ["--database", str(database), "--output", str(output)]
    assert runner.main(args + (["--dry-run"] if dry_run else [])) == 0
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before
    if dry_run:
        assert not output.parent.exists()
        assert "# Loss Review Report" in capsys.readouterr().out
    else:
        assert "# Loss Review Report" in output.read_text(encoding="utf-8")


def test_default_output_and_dry_run_preserves_existing_file(tmp_path, monkeypatch):
    database = tmp_path / "paper.db"
    output = tmp_path / "research" / "loss_review.md"
    rules_file = tmp_path / "loss_rules.json"
    create_database(database, {})
    monkeypatch.setattr(runner, "default_database_path", lambda: database)
    monkeypatch.setattr(runner, "DEFAULT_OUTPUT_PATH", output)
    monkeypatch.setattr(runner, "DEFAULT_RULES_PATH", rules_file)
    assert runner.main([]) == 0
    before = output.read_bytes(), output.stat().st_mtime_ns
    before_rules = rules_file.read_bytes(), rules_file.stat().st_mtime_ns
    assert runner.main(["--dry-run"]) == 0
    assert (output.read_bytes(), output.stat().st_mtime_ns) == before
    assert (rules_file.read_bytes(), rules_file.stat().st_mtime_ns) == before_rules


@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_database_exit_one(tmp_path, capsys, dry_run):
    database = tmp_path / "missing.db"
    output = tmp_path / "out.md"
    args = ["--database", str(database), "--output", str(output)]
    assert runner.main(args + (["--dry-run"] if dry_run else [])) == 1
    assert "Database file not found" in capsys.readouterr().err
    assert not database.exists()
    assert not output.exists()


def test_output_cannot_overwrite_database(tmp_path, capsys):
    database = tmp_path / "paper.db"
    create_database(database, {})
    before = database.read_bytes()
    assert runner.main(["--database", str(database), "--output", str(database)]) == 1
    assert "Output must not overwrite the database" in capsys.readouterr().err
    assert database.read_bytes() == before


def test_group_by_end_treats_equivalent_timestamps_as_same_moment():
    rows = [
        position(ends_at="2026-10-01T00:30:00Z"),
        position(ends_at="2026-10-01T00:30:00+00:00"),
        position(ends_at="2026-10-01T00:30:00.123456Z"),
        position(ends_at="2026-10-01T00:30:00.000+00:00"),
    ]
    ends = group_by_end(rows)
    assert len(ends) == 1
    key = utc_time("2026-10-01T00:30:00Z").replace(microsecond=0)
    assert len(ends[key]) == 4


def test_null_fields_handled_gracefully(tmp_path):
    database = tmp_path / "nulls.db"
    row_with_nulls = position(
        title=None,
        entry_band=None,
        opened_at=None,
        ends_at=None,
        pnl=-50.0,
    )
    create_database(database, {TABLES[0]: [row_with_nulls]})
    rows, counts = read_positions(database)
    assert len(rows) == 1
    tags = row_tags(rows[0], 1)
    assert tags["asset"] == "unknown"
    assert tags["entry_band"] == "unknown"
    assert tags["open_hour"] == "unknown"
    assert tags["time_to_end"] == "unknown"
    assert tags["same_end_count"] == "unknown"
    report = build_report(rows, counts)
    assert "# Loss Review Report" in report


def test_win_loss_decided_by_pnl_not_win_column():
    rows = [
        dict(win=1, pnl=-10.0),
        dict(win=0, pnl=10.0),
        dict(win=0, pnl=0.0),
    ]
    summary = summarize(rows)
    assert summary["losses"] == 1
    assert summary["wins"] == 2
    assert summary["loss_rate"] == 1 / 3
    assert summary["pnl"] == 0.0


def test_summarize_ignores_null_pnl():
    rows = [
        dict(win=1, pnl=-10.0),
        dict(win=0, pnl=10.0),
        dict(win=1, pnl=None),
    ]
    summary = summarize(rows)
    assert summary["settled"] == 2
    assert summary["losses"] == 1
    assert summary["wins"] == 1
    assert summary["loss_rate"] == 0.5
    assert summary["pnl"] == 0.0

