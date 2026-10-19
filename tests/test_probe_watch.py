from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import threading
import time

import pytest
import run_probe_watch as watch
from andy_trader import llm_agent
from andy_trader.ablation import run_ablation
from andy_trader.portfolio import execute_paper_trade, get_or_create_state
from andy_trader.store import Candle, connect, record_observations

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def bar(instrument='BTC-USD', age=1, price=100, low=None):
    return Candle(instrument, 'binance', '1m', (NOW - timedelta(minutes=age)).isoformat(),
                  price, price, low if low is not None else price, price, 1)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, 'JOURNAL_PATH', tmp_path / 'journal.jsonl')
    monkeypatch.setattr(watch, 'LOCK_PATH', tmp_path / 'watch.lock')
    monkeypatch.setattr(watch, 'INSTRUMENTS', ('BTC-USD', 'ETH-USD'))
    monkeypatch.setattr(watch, 'fetch_binance', lambda instrument, interval, settings: [bar(instrument)])
    for key in ('LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL'):
        monkeypatch.setenv(key, 'fake')
    monkeypatch.setenv('LLM_LIST_ORDER', 'ranked')
    monkeypatch.setenv('LLM_MIN_MINUTES', '60')
    monkeypatch.setenv('LLM_DAILY_CALLS', '100')
    monkeypatch.setattr(llm_agent, 'ask_model', lambda *a, **k: reply())
    with connect(tmp_path / 'test.db') as db:
        yield db


def reply():
    return {'choices': [{'message': {'content': json.dumps({'calls': [
        {'instrument': i, 'p_up': .9, 'reason': 'test'} for i in watch.INSTRUMENTS]})}}]}


def open_position(db, book='probe'):
    execute_paper_trade(db, predictor=book, instrument='BTC-USD', target_side='long', price=100,
                        now_iso=(NOW - timedelta(minutes=2)).isoformat(), starting_cash=1000, reason='test')


def test_exit_before_slow_ai_and_entry_costs(setup, monkeypatch):
    db = setup
    open_position(db)
    monkeypatch.setattr(watch, 'fetch_binance', lambda i, *a: [bar(i, low=97)])
    # The model thread must not access SQLite; observe the journal instead.
    def slow(*a, **k):
        assert 'closed_trade' in watch.JOURNAL_PATH.read_text()
        time.sleep(.03)
        return reply()
    monkeypatch.setattr(llm_agent, 'ask_model', slow)
    watch.run_pass(db, now=NOW)
    rows = [json.loads(s) for s in watch.JOURNAL_PATH.read_text().splitlines()]
    closed = [r for r in rows if r['event'] == 'closed_trade']
    assert len(closed) == 1
    entry, exit = db.execute("SELECT * FROM paper_trades WHERE predictor='probe' AND instrument='BTC-USD' ORDER BY id").fetchall()[:2]
    assert closed[0]['entry_cost'] == pytest.approx(entry['fee_cost'] + entry['slippage_cost'])
    assert closed[0]['pnl'] == pytest.approx(exit['qty'] * (exit['price'] - entry['price']) - closed[0]['entry_cost'] - closed[0]['exit_cost'])
    watch.run_pass(db, now=NOW + timedelta(minutes=1))
    assert get_or_create_state(db, predictor='probe', instrument='BTC-USD', now_iso=NOW.isoformat()).position_qty == 0


def test_timeout_records_error_after_exit(setup, monkeypatch):
    open_position(setup)
    monkeypatch.setattr(watch, 'fetch_binance', lambda i, *a: [bar(i, low=97)])
    release = threading.Event()
    monkeypatch.setattr(llm_agent, 'ask_model', lambda *a, **k: release.wait(2))
    original = llm_agent.ask_with_timeout
    monkeypatch.setattr(llm_agent, 'ask_with_timeout', lambda context, timeout: original(context, .03))
    start = time.monotonic()
    try:
        watch.run_pass(setup, now=NOW)
        assert time.monotonic() - start < 1.8
        assert setup.execute("SELECT count(*) FROM llm_decisions WHERE status='error' AND reason LIKE '%TimeoutError%'").fetchone()[0] == 2
        assert get_or_create_state(setup, predictor='probe', instrument='BTC-USD', now_iso=NOW.isoformat()).position_qty == 0
    finally:
        release.set()


def test_stale_price_skips_exit_and_mark(setup, monkeypatch):
    open_position(setup)
    monkeypatch.setattr(watch, 'fetch_binance', lambda i, *a: [bar(i, age=5, low=90)])
    watch.run_pass(setup, now=NOW)
    assert get_or_create_state(setup, predictor='probe', instrument='BTC-USD', now_iso=NOW.isoformat()).position_qty > 0
    assert setup.execute('SELECT count(*) FROM paper_equity_curve').fetchone()[0] == 0
    assert 'stale_price' in watch.JOURNAL_PATH.read_text()


def test_batch_once_each_book_and_risk_without_new_batch(setup, monkeypatch):
    watch.run_pass(setup, now=NOW)
    assert setup.execute('SELECT count(*) FROM probe_batch_claims').fetchone()[0] == 2
    assert setup.execute('SELECT count(*) FROM paper_trades').fetchone()[0] == 4
    calls = []
    original = watch.check_and_enforce
    def risk(*args, **kwargs):
        calls.append(kwargs['predictor'])
        return original(*args, **kwargs)
    monkeypatch.setattr(watch, 'check_and_enforce', risk)
    watch.run_pass(setup, now=NOW + timedelta(minutes=1))
    assert calls == ['probe', 'probe', 'probe_x10', 'probe_x10']
    assert setup.execute('SELECT count(*) FROM paper_trades').fetchone()[0] == 4


def test_locks(setup, monkeypatch):
    token = watch.acquire_lock()
    assert token
    monkeypatch.setattr(watch, 'run_pass', lambda *a: pytest.fail('fresh lock must skip'))
    assert watch.main([]) == 0
    os.utime(watch.LOCK_PATH, (time.time() - 181,) * 2)
    replacement = watch.acquire_lock()
    assert replacement and replacement != token
    assert watch.LOCK_PATH.read_text() == replacement


def test_cycle_has_no_probe_calls():
    source = Path('run_cycle.py').read_text()
    assert 'probe_trade_once' not in source
    assert 'probe_x10_trade_once' not in source
    assert 'build_predictors' not in source


def test_minute_moves_and_ablation(setup):
    record_observations(setup, [bar(age=m + 1, price=130 - m) for m in range(31)])
    context = llm_agent.render_context(setup, watch.INSTRUMENTS, NOW.isoformat())
    assert '5m 4.0000%; 15m 13.0435%; 30m 30.0000%' in context
    assert 'ETH-USD: no fresh 1m bars' in context
    old = llm_agent.render_context(setup, watch.INSTRUMENTS, (NOW + timedelta(minutes=4)).isoformat())
    assert '5m 4.0000%' not in old
    from andy_trader.llm_ledger import initialize_ledger, record_decision
    initialize_ledger(setup)
    record_decision(setup, batch_id='ablation-source', instrument='BTC-USD', p_up=.9, status='ok', rendered_context=context)
    seen = []
    run_ablation(setup, ask=lambda text: (seen.append(text), reply())[1])
    assert any('## SECTION: minute_moves' not in text and '## SECTION: prices' in text for text in seen)


def test_batch_selection_ignores_newest(setup):
    from andy_trader.llm_ledger import record_decision
    from andy_trader.probe_book import probe_trade_once
    watch.run_pass(setup, now=NOW)
    record_decision(setup, batch_id='sell', instrument='BTC-USD', p_up=.1, status='ok', created_at=NOW.isoformat())
    record_decision(setup, batch_id='newest-buy', instrument='BTC-USD', p_up=.9, status='ok', created_at=NOW.isoformat())
    attempt = probe_trade_once(setup, instrument='BTC-USD', interval='1m', batch_id='sell', now=NOW)
    assert attempt.trade.side == 'flat'


def test_daily_cap_and_pass_budget(setup, monkeypatch):
    monkeypatch.setenv('LLM_DAILY_CALLS', '0')
    monkeypatch.setattr(llm_agent, 'ask_model', lambda *a, **k: pytest.fail('cap'))
    watch.run_pass(setup, now=NOW)
    assert setup.execute('SELECT count(*) FROM paper_trades').fetchone()[0] == 0
    assert setup.execute("SELECT count(*) FROM llm_decisions WHERE status='capped'").fetchone()[0] == 2
    real_clock = time.monotonic
    calls = []
    def clock():
        calls.append(1)
        return real_clock() - 21 if len(calls) == 1 else real_clock()
    monkeypatch.setattr(watch.time, "monotonic", clock)
    watch.run_pass(setup, now=NOW)
    assert setup.execute('SELECT count(*) FROM llm_decisions').fetchone()[0] == 2


@pytest.mark.parametrize('reason', ['bracket stop', 'bracket target', 'liquidated', 'probability_up=0.5', 'RISK INTERLOCK forced exit'])
def test_journals_every_exit_kind(setup, reason):
    open_position(setup)
    execute_paper_trade(setup, predictor='probe', instrument='BTC-USD', target_side='flat', price=99,
                        now_iso=NOW.isoformat(), reason=reason)
    watch.journal_closes(setup, 0)
    lines = [json.loads(line) for line in watch.JOURNAL_PATH.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]['reason'] == reason
    assert lines[0]['entry_cost'] > 0


def test_risk_bracket_mark_order_all_books(setup, monkeypatch):
    events = []
    for name in ('check_and_enforce', 'check_bracket', 'mark_to_market'):
        original = getattr(watch, name)
        def track(*a, _name=name, _original=original, **k):
            events.append(_name)
            return _original(*a, **k)
        monkeypatch.setattr(watch, name, track)
    monkeypatch.delenv('LLM_MODEL')
    watch.run_pass(setup, now=NOW)
    assert events == ['check_and_enforce', 'check_bracket', 'mark_to_market'] * 4


def test_fetches_parallel_with_eight_second_timeout(setup, monkeypatch):
    barrier = threading.Barrier(2)
    def fetch(instrument, interval, settings):
        assert interval == '1m'
        assert settings.timeout_seconds == 8 and settings.retries == 1
        barrier.wait(timeout=1)
        return [bar(instrument)]
    monkeypatch.setattr(watch, 'fetch_binance', fetch)
    watch.run_pass(setup, now=NOW)
    assert setup.execute('SELECT count(*) FROM paper_trades').fetchone()[0] == 4


def test_stop_on_later_pass_does_not_reopen_old_batch(setup, monkeypatch):
    watch.run_pass(setup, now=NOW)
    monkeypatch.setattr(watch, 'fetch_binance', lambda i, *a: [bar(i, age=-1, low=97)])
    watch.run_pass(setup, now=NOW + timedelta(minutes=2))
    for book in ('probe', 'probe_x10'):
        assert get_or_create_state(setup, predictor=book, instrument='BTC-USD', now_iso=NOW.isoformat()).position_qty == 0
    count = setup.execute('SELECT count(*) FROM paper_trades').fetchone()[0]
    watch.run_pass(setup, now=NOW + timedelta(minutes=3))
    assert setup.execute('SELECT count(*) FROM paper_trades').fetchone()[0] == count
    assert setup.execute('SELECT count(*) FROM probe_batch_claims').fetchone()[0] == 2


def test_existing_claim_blocks_repeated_batch(setup, monkeypatch):
    watch.run_pass(setup, now=NOW)
    batch = setup.execute('SELECT batch_id FROM probe_batch_claims LIMIT 1').fetchone()[0]
    monkeypatch.setenv('LLM_MIN_MINUTES', '0')
    def same_batch(self, connection, now_iso):
        self.batch_id = batch
        return {i: .9 for i in watch.INSTRUMENTS}
    monkeypatch.setattr(llm_agent.LlmPredictor, '_load_batch', same_batch)
    monkeypatch.setattr(watch, 'BOOKS', tuple((book, lev, lambda *a, **k: pytest.fail('claimed batch')) for book, lev, _ in watch.BOOKS))
    watch.run_pass(setup, now=NOW + timedelta(minutes=1))
    assert setup.execute('SELECT count(*) FROM probe_batch_claims').fetchone()[0] == 2
