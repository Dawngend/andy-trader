from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3

import pytest

from andy_trader.bond_book import (
    ENTRY_BANDS, initialize_bond_book, open_position as bond_open_position,
)
from andy_trader.loss_rules import blocking_rule, learn_rules, load_rules
from andy_trader.price_target_book import (
    initialize_price_target_book, open_position as pt_open_position,
)
from andy_trader.store import connect
import run_loss_review as runner

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def position(pnl=5.0, **changes):
    row = dict(
        market_id="market", title="Bitcoin above 100,000?", side="Yes",
        entry_band="0.90 to 0.95", fill_price=0.92, shares=100,
        opened_at="2026-10-01T00:00:00+00:00", ends_at="2026-10-02T00:00:00Z",
        win=int(pnl >= 0) if pnl is not None else None, pnl=pnl,
    )
    row.update(changes)
    return row


def market(mid="m1", **changes):
    return {
        "id": mid,
        "question": "Bitcoin above 100,000?",
        "active": True,
        "closed": False,
        "endDate": (NOW + timedelta(days=1)).isoformat(),
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps([mid + "y", mid + "n"]),
        **changes,
    }


def test_qualifying_rule_threshold():
    # 30+ rows, loss rate >= 1.5x overall, negative pnl -> becomes a rule.
    # 29 rows with same properties -> does not become a rule.
    # Rows in PAUSED_SLICES are excluded from learning and overall loss rate calculation.
    rows = []
    # 30 rows in entry_band 0.80 to 0.90: 10 losses (-50 each), 20 wins (+5 each) -> loss rate 33.3%, pnl = -400
    for i in range(30):
        pnl = -50.0 if i < 10 else 5.0
        rows.append(position(pnl=pnl, entry_band="0.80 to 0.90", title=f"Bitcoin above {i}"))

    # 29 rows for asset Solana: 10 losses (-50 each), 19 wins (+5 each) -> loss rate 34.5%, pnl = -405
    for i in range(29):
        pnl = -50.0 if i < 10 else 5.0
        rows.append(position(pnl=pnl, title=f"Solana reach {i}", side="Yes"))

    # 41 baseline winning rows so overall loss rate is lower:
    # active total = 30 + 29 + 41 = 100 rows, 20 losses -> overall loss rate = 0.20
    # 1.5 * 0.20 = 0.30
    for i in range(41):
        rows.append(position(pnl=5.0, title=f"Ethereum above {i}"))

    # 50 rows in PAUSED_SLICES (up_down) that all lost: must NOT affect overall or be learned
    for i in range(50):
        rows.append(position(pnl=-100.0, title=f"Bitcoin Up or Down {i}"))

    rules = learn_rules(rows)
    entry_band_rules = [r for r in rules if r["tag"] == "entry_band" and r["value"] == "0.80 to 0.90"]
    asset_rules = [r for r in rules if r["tag"] == "asset" and r["value"] == "Solana"]
    paused_rules = [r for r in rules if r["tag"] == "slice" and r["value"] == "up_down"]

    assert len(entry_band_rules) == 1
    assert entry_band_rules[0]["settled"] == 30
    assert entry_band_rules[0]["pnl"] == -400.0
    assert len(asset_rules) == 0  # Only 29 rows, below the 30-row bar
    assert len(paused_rules) == 0  # Paused slice ignored


def test_only_five_worst_rules_kept():
    # Generate 7 qualifying rules with different negative pnls
    rows = []
    # 100 baseline winners to keep overall loss rate low (overall = 10% after adding candidates)
    for i in range(200):
        rows.append(position(pnl=10.0, title=f"Token{i} above 100"))

    # 7 distinct assets, each with 30 settled rows, 15 losses (loss rate 50%), but different loss amounts
    loss_amounts = [-10.0, -20.0, -30.0, -40.0, -50.0, -60.0, -70.0]
    for idx, total_loss in enumerate(loss_amounts):
        asset_name = f"Coin{idx}"
        for i in range(30):
            pnl = total_loss / 15.0 if i < 15 else 0.0
            rows.append(position(pnl=pnl, title=f"{asset_name} above {i}"))

    rules = learn_rules(rows)
    # At most 5 rules kept, and they must be the 5 worst (lowest / most negative pnl)
    assert len(rules) == 5
    pnls = [r["pnl"] for r in rules]
    assert pnls == [-70.0, -60.0, -50.0, -40.0, -30.0]


def test_blocking_rule_matches():
    rules = [
        {"tag": "entry_band", "value": "0.80 to 0.90", "pnl": -300.0},
        {"tag": "slice", "value": "above_below", "pnl": -200.0},
    ]
    # Matches on tag and value
    match1 = blocking_rule(rules, {"entry_band": "0.80 to 0.90", "slice": "other"})
    assert match1 == rules[0]

    match2 = blocking_rule(rules, {"entry_band": "0.90 to 0.95", "slice": "above_below"})
    assert match2 == rules[1]

    # No match when value differs
    assert blocking_rule(rules, {"entry_band": "0.90 to 0.95", "slice": "other"}) is None

    # No match on empty tags or rules
    assert blocking_rule([], {"entry_band": "0.80 to 0.90"}) is None
    assert blocking_rule(rules, {}) is None


def test_load_rules_missing_bad_and_old_files(tmp_path):
    missing = tmp_path / "missing_rules.json"
    assert load_rules(missing) == []

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{invalid json", encoding="utf-8")
    assert load_rules(bad_json) == []

    non_dict = tmp_path / "not_dict.json"
    non_dict.write_text("[\"not\", \"a\", \"dict\"]", encoding="utf-8")
    assert load_rules(non_dict) == []

    # File older than 14 days by generated_at
    old_gen = tmp_path / "old_gen.json"
    old_time = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    old_gen.write_text(json.dumps({"generated_at": old_time, "rules": [{"tag": "asset", "value": "BTC"}]}))
    assert load_rules(old_gen) == []

    # File older than 14 days by mtime
    old_mtime = tmp_path / "old_mtime.json"
    recent_time = datetime.now(UTC).isoformat()
    old_mtime.write_text(json.dumps({"generated_at": recent_time, "rules": [{"tag": "asset", "value": "BTC"}]}))
    past_timestamp = (datetime.now(UTC) - timedelta(days=16)).timestamp()
    os.utime(old_mtime, (past_timestamp, past_timestamp))
    assert load_rules(old_mtime) == []

    # Valid fresh file returns rules
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"generated_at": recent_time, "rules": [{"tag": "asset", "value": "BTC"}]}))
    assert load_rules(valid) == [{"tag": "asset", "value": "BTC"}]

    # Unknown tags or invalid same_end_count values are filtered out
    invalid_rules_file = tmp_path / "invalid_tags.json"
    invalid_rules_file.write_text(json.dumps({
        "generated_at": recent_time,
        "rules": [
            {"tag": "unknown_tag", "value": "xyz"},
            {"tag": "same_end_count", "value": "1"},
            {"tag": "same_end_count", "value": "2"},
            {"tag": "asset", "value": "BTC"},
            {"tag": "same_end_count", "value": "3 or more"},
        ],
    }))
    assert load_rules(invalid_rules_file) == [
        {"tag": "asset", "value": "BTC"},
        {"tag": "same_end_count", "value": "3 or more"},
    ]


def test_open_position_blocks_and_records_skip(tmp_path):
    db_path = tmp_path / "book.db"
    rules_path = tmp_path / "loss_rules.json"
    rules_data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rules": [
            {"tag": "entry_band", "value": "0.80 to 0.90", "settled": 40, "loss_rate": 0.25, "pnl": -200.0, "reason": "Bad band."},
        ],
    }
    rules_path.write_text(json.dumps(rules_data), encoding="utf-8")

    with connect(db_path) as conn:
        initialize_bond_book(conn)

        m = market("m1", question="Bitcoin above 100,000?")
        # Price 0.85 -> entry_band "0.80 to 0.90", matching rule
        bond_open_position(conn, "e1", m, "Yes", 0.85, 0.60, NOW, rules_path=rules_path)

        pos = conn.execute("SELECT * FROM bond_book_positions").fetchall()
        assert len(pos) == 0

        skips = dict(conn.execute("SELECT reason, count FROM bond_book_skips"))
        assert skips.get("loss_rule") == 1

        # Price 0.92 -> entry_band "0.90 to 0.95", does not match rule
        bond_open_position(conn, "e2", market("m2"), "Yes", 0.92, 0.60, NOW, rules_path=rules_path)

        pos = conn.execute("SELECT * FROM bond_book_positions").fetchall()
        assert len(pos) == 1
        assert pos[0]["market_id"] == "m2"

        # Skip count for loss_rule should remain 1
        skips = dict(conn.execute("SELECT reason, count FROM bond_book_skips"))
        assert skips.get("loss_rule") == 1


def test_price_target_book_blocks_and_records_skip(tmp_path):
    db_path = tmp_path / "pt_book.db"
    rules_path = tmp_path / "loss_rules.json"
    rules_data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rules": [
            {"tag": "asset", "value": "Solana", "settled": 40, "loss_rate": 0.3, "pnl": -250.0, "reason": "Solana drops."},
        ],
    }
    rules_path.write_text(json.dumps(rules_data), encoding="utf-8")

    with connect(db_path) as conn:
        initialize_price_target_book(conn)

        sol_market = market("sol_m", question="Solana reach 500?")
        pt_open_position(conn, "e_sol", sol_market, "No", 0.88, 0.50, NOW, rules_path=rules_path)

        pos = conn.execute("SELECT * FROM price_target_book_positions").fetchall()
        assert len(pos) == 0

        skips = dict(conn.execute("SELECT reason, count FROM price_target_book_skips"))
        assert skips.get("loss_rule") == 1


def test_same_end_count_rule_blocks_third_open_position(tmp_path):
    db_path = tmp_path / "same_end.db"
    rules_path = tmp_path / "loss_rules.json"
    rules_data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rules": [
            {"tag": "same_end_count", "value": "3 or more", "settled": 35, "loss_rate": 0.4, "pnl": -350.0, "reason": "Clusters lose."},
        ],
    }
    rules_path.write_text(json.dumps(rules_data), encoding="utf-8")

    end_time = (NOW + timedelta(days=2)).isoformat()
    with connect(db_path) as conn:
        initialize_bond_book(conn)

        # 1st position with end_time -> allowed
        m1 = market("m1", question="Bitcoin above 100,000?", endDate=end_time)
        bond_open_position(conn, "e1", m1, "Yes", 0.92, 0.60, NOW, rules_path=rules_path)

        # 2nd position with same end_time -> allowed
        m2 = market("m2", question="Ethereum above 4,000?", endDate=end_time)
        bond_open_position(conn, "e2", m2, "Yes", 0.92, 0.60, NOW, rules_path=rules_path)

        pos = conn.execute("SELECT * FROM bond_book_positions").fetchall()
        assert len(pos) == 2

        # 3rd position with same end_time (even with different format like +00:00) -> BLOCKED
        end_time_alt = (NOW + timedelta(days=2)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        m3 = market("m3", question="Solana above 300?", endDate=end_time_alt)
        bond_open_position(conn, "e3", m3, "Yes", 0.92, 0.60, NOW, rules_path=rules_path)

        pos = conn.execute("SELECT * FROM bond_book_positions").fetchall()
        assert len(pos) == 2  # Not inserted

        skips = dict(conn.execute("SELECT reason, count FROM bond_book_skips"))
        assert skips.get("loss_rule") == 1


def test_dry_run_does_not_write_rules_file(tmp_path, capsys):
    database = tmp_path / "paper.db"
    output = tmp_path / "loss_review.md"
    rules_output = tmp_path / "loss_rules.json"

    # Create empty database
    with sqlite3.connect(database) as conn:
        initialize_bond_book(conn)

    # Dry run
    args = ["--database", str(database), "--output", str(output),
            "--rules-output", str(rules_output), "--dry-run"]
    assert runner.main(args) == 0
    assert not output.exists()
    assert not rules_output.exists()
    assert "No rules active." in capsys.readouterr().out

    # Non dry run
    args_normal = ["--database", str(database), "--output", str(output),
                   "--rules-output", str(rules_output)]
    assert runner.main(args_normal) == 0
    assert output.exists()
    assert rules_output.exists()

    content = json.loads(rules_output.read_text(encoding="utf-8"))
    assert "generated_at" in content
    assert "rules" in content


def test_open_position_gracefully_handles_malformed_dates(tmp_path):
    db_path = tmp_path / "malformed.db"
    rules_path = tmp_path / "loss_rules.json"
    rules_data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rules": [
            {"tag": "same_end_count", "value": "3 or more", "settled": 35, "loss_rate": 0.4, "pnl": -350.0, "reason": "Clusters lose."},
        ],
    }
    rules_path.write_text(json.dumps(rules_data), encoding="utf-8")

    with connect(db_path) as conn:
        initialize_bond_book(conn)
        # Row with malformed ends_at already in DB
        conn.execute("""
            INSERT INTO bond_book_positions
            (market_id, event_id, title, side, entry_band, fill_price, fee, shares, opened_at, ends_at)
            VALUES ('m_bad', 'e_bad', 'Bad Date Question', 'Yes', '0.90 to 0.95', 0.92, 0.60, 100, 'bad_date', 'not-a-date')
        """)
        conn.commit()
        # Opening a market with malformed endDate does not raise and does not crash
        bad_market = market("m_new", endDate="invalid_iso")
        bond_open_position(conn, "e_new", bad_market, "Yes", 0.92, 0.60, NOW, rules_path=rules_path)
        pos = conn.execute("SELECT * FROM bond_book_positions WHERE market_id = 'm_new'").fetchall()
        assert len(pos) == 1


def test_same_end_count_rule_blocks_across_books(tmp_path):
    db_path = tmp_path / "cross_books.db"
    rules_path = tmp_path / "loss_rules.json"
    rules_data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rules": [
            {"tag": "same_end_count", "value": "3 or more", "settled": 35, "loss_rate": 0.4, "pnl": -350.0, "reason": "Clusters lose."},
        ],
    }
    rules_path.write_text(json.dumps(rules_data), encoding="utf-8")

    end_time = (NOW + timedelta(days=2)).isoformat()
    with connect(db_path) as conn:
        initialize_bond_book(conn)
        initialize_price_target_book(conn)

        # 2 open positions in bond_book
        m1 = market("m1", question="Bitcoin above 100,000?", endDate=end_time)
        bond_open_position(conn, "e1", m1, "Yes", 0.92, 0.60, NOW, rules_path=rules_path)
        m2 = market("m2", question="Ethereum above 4,000?", endDate=end_time)
        bond_open_position(conn, "e2", m2, "Yes", 0.92, 0.60, NOW, rules_path=rules_path)

        # 1 position in price_target_book sharing same end_time -> should be blocked!
        m3 = market("m3", question="Solana dip to 100?", endDate=end_time)
        pt_open_position(conn, "e3", m3, "No", 0.92, 0.60, NOW, rules_path=rules_path)

        pos_pt = conn.execute("SELECT * FROM price_target_book_positions").fetchall()
        assert len(pos_pt) == 0

        skips_pt = dict(conn.execute("SELECT reason, count FROM price_target_book_skips"))
        assert skips_pt.get("loss_rule") == 1

