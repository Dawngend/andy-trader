"""Sender tests use fake HTTP exclusively, including when process env is armed."""
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import sqlite3
from urllib.parse import urlsplit, parse_qs

import pytest

from andy_trader import bybit_sender as sender, dry_run_executor as executor, risk
from andy_trader.env import load_env_file
from andy_trader.order_intent import initialize_order_intents, new_intent, record_intent
from andy_trader.store import initialize_database

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_live_defaults(monkeypatch, tmp_path):
    for name in ('ANDY_SENDER_LIVE', 'ANDY_SENDER_NET', 'BYBIT_API_KEY', 'BYBIT_API_SECRET'):
        monkeypatch.delenv(name, raising=False)
    def fail(*args, **kwargs):
        pytest.fail('unmocked HTTP call')
    monkeypatch.setattr(sender, 'HTTP', fail)
    monkeypatch.setattr(sender, 'REPO_ROOT', tmp_path)


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    initialize_order_intents(connection)
    sender.initialize_live_orders(connection)
    yield connection
    connection.close()


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv('ANDY_SENDER_LIVE', 'on')
    monkeypatch.setenv('BYBIT_API_KEY', 'test-key-unique')
    monkeypatch.setenv('BYBIT_API_SECRET', 'test-secret-unique')


def intent(db, **changes):
    defaults = dict(instrument='SOL-USD', predictor='p', side='long', size=500,
                    stop=98, target=104, source_decision_id=1, now_iso=NOW.isoformat(),
                    expiry_seconds=300, leverage=sender.LEVERAGE)
    defaults.update(changes)
    ident = record_intent(db, new_intent(**defaults))
    db.execute('''INSERT OR IGNORE INTO crypto_observations
        (content_hash,instrument,venue,interval,open_time,open,high,low,close,volume,
         degraded,first_seen_at,last_seen_at,times_seen)
        VALUES (?,?,'test','1h',?,100,100,100,100,0,0,?,?,1)''',
        (defaults['instrument'], defaults['instrument'], NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
    db.commit()
    return db.execute('SELECT * FROM order_intents WHERE id=?', (ident,)).fetchone()


class Exchange:
    def __init__(self, db):
        self.db = db
        self.calls = []
        self.margin_mode = 'ISOLATED_MARGIN'
        self.price = '100'
        self.min_qty = '0.01'
        self.min_notional = '5'
        self.step = '0.01'
        self.tick = '0.1'
        self.codes = {}
        self.states = ['Filled']
        self.position_reads = 0
        self.existing_position = False
        self.naked = False
        self.empty_reads = 0
        self.transport_error = False
        self.close_error = False
        self.create_hook = None
        self.before_leverage = None

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        path = parts.path
        params = json.loads(body) if body else {k: v[0] for k,v in parse_qs(parts.query).items()}
        self.calls.append((method, url, params))
        # Verify production signing and absence of credentials in body/query.
        payload = body.decode() if body else parts.query
        expected = hmac.new(b'test-secret-unique', (headers['X-BAPI-TIMESTAMP'] + 'test-key-unique' +
                            sender.RECV_WINDOW + payload).encode(), hashlib.sha256).hexdigest()
        assert headers['X-BAPI-SIGN'] == expected
        assert 'test-key-unique' not in url + payload
        assert 'test-secret-unique' not in url + payload
        symbol = params.get('symbol', 'SOLUSDT')
        result = {}
        if path.endswith('/account/info'):
            result = {'marginMode': self.margin_mode}
        elif path.endswith('/tickers'):
            result = {'list': [{'symbol': symbol, 'lastPrice': self.price}]}
        elif path.endswith('/instruments-info'):
            result = {'list': [{'symbol': symbol, 'contractType': 'LinearPerpetual', 'settleCoin': 'USDT',
                'lotSizeFilter': {'qtyStep': self.step, 'minOrderQty': self.min_qty,
                                 'minNotionalValue': self.min_notional},
                'priceFilter': {'tickSize': self.tick}}]}
        elif path.endswith('/position/list'):
            self.position_reads += 1
            size = '1.6' if self.existing_position or self.position_reads > 1 else '0'
            result = {'list': [{'symbol': symbol, 'size': size,
                        'stopLoss': '0' if self.naked else '98', 'takeProfit': '0' if self.naked else '104'}]}
            if 1 < self.position_reads <= self.empty_reads + 1:
                result = {'list': []}
        elif path.endswith('/set-leverage') and self.before_leverage:
            self.before_leverage()
        elif path.endswith('/create'):
            assert self.db.execute('SELECT status FROM live_orders ORDER BY id DESC').fetchone()[0] in ('pending', 'sent')
            if self.create_hook:
                self.create_hook()
            if self.transport_error:
                raise OSError('test-key-unique test-secret-unique ' + expected)
            if params.get('reduceOnly') and self.close_error:
                raise OSError('close failed')
            result = {'orderLinkId': params['orderLinkId']}
        elif path.endswith('/realtime') or path.endswith('/history'):
            state = self.states[0]
            if len(self.states) > 1:
                self.states.pop(0)
            if params['orderLinkId'].endswith('-close'):
                state = 'Filled'
            result = {'list': [{'orderLinkId': params['orderLinkId'], 'orderStatus': state,
                               'cumExecQty': '0.4' if state == 'PartiallyFilledCanceled' else ('1.6' if state == 'Filled' else '0')}]}
        return {'retCode': self.codes.get(path, 0), 'result': result}

    def entries(self):
        return [p for _, u, p in self.calls if urlsplit(u).path == '/v5/order/create' and not p.get('reduceOnly')]


def send(db, row, exchange, **kwargs):
    return sender.send_live(db, row, http=exchange, clock=lambda: NOW, sleep=lambda _: None, **kwargs)


def count(db):
    return db.execute('SELECT COUNT(*) FROM live_orders').fetchone()[0]


def seed(db, ident, *, status='filled', instrument='OTHER', age=0, qty=1, stop=99):
    stamp = (NOW - timedelta(minutes=age)).isoformat()
    db.execute('''INSERT INTO live_orders
        (intent_id,order_link_id,instrument,side,qty,notional,margin,entry_price,stop,target,status,
         created_at,updated_at,exchange_response) VALUES (?,? ,?,'long',?,100,10,100,?,104,?,?,?,'{}')''',
        (ident, f'andy-{ident}', instrument, qty, stop, status, stamp, stamp))
    db.commit()


@pytest.mark.parametrize('value', [None, 'ON', 'true', ' on', 'on '])
def test_exact_live_env(db, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv('ANDY_SENDER_LIVE', value)
    assert sender.send_live(db, intent(db))['status'] == 'refused'
    assert count(db) == 0


@pytest.mark.parametrize('missing', ['BYBIT_API_KEY', 'BYBIT_API_SECRET'])
def test_missing_credentials(db, armed, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert sender.send_live(db, intent(db))['status'] == 'refused'
    assert count(db) == 0


@pytest.mark.parametrize('argv,enabled', [([], False), (['--live'], True)])
def test_cli_seam(db, armed, monkeypatch, argv, enabled):
    import andy_trader.store as store
    monkeypatch.setattr(store, 'connect', lambda _: db)
    seen = []
    monkeypatch.setattr(executor, 'run_dry_run', lambda c, live_sender: seen.append(live_sender) or
                        {'checked': 0, 'would_send': 0, 'rejected': 0})
    executor.main(argv)
    assert seen == [sender.send_live if enabled else None]


def test_live_flag_without_env_is_off(db, monkeypatch):
    import andy_trader.store as store
    row = intent(db)
    monkeypatch.setattr(store, 'connect', lambda _: db)
    monkeypatch.setattr(executor, 'utc_now_iso', lambda: NOW.isoformat())
    monkeypatch.setattr(executor, 'evaluate_paper_eligibility', lambda *a, **k: type('Gate', (), {'eligible': True})())
    executor.main(['--live'])
    assert count(db) == 0
    assert db.execute('SELECT COUNT(*) FROM order_decisions').fetchone()[0] == 1


@pytest.mark.parametrize('network,host', [(None, 'api-testnet.bybit.com'), ('mainnet', 'api.bybit.com'),
                                         ('MAINNET', 'api-testnet.bybit.com'), (' mainnet ', 'api-testnet.bybit.com')])
def test_network_and_happy_order(db, armed, monkeypatch, network, host):
    if network:
        monkeypatch.setenv('ANDY_SENDER_NET', network)
    row = intent(db)
    api = Exchange(db)
    assert send(db, row, api)['status'] == 'filled'
    assert all(urlsplit(u).netloc == host for _, u, _ in api.calls)
    leverage = next(p for _,u,p in api.calls if u.endswith('/set-leverage'))
    assert leverage['buyLeverage'] == leverage['sellLeverage'] == '10'
    order = api.entries()[0]
    assert order['positionIdx'] == 0 and float(order['stopLoss']) == 98
    saved = db.execute('SELECT * FROM live_orders').fetchone()
    assert saved['notional'] == saved['margin'] * 10 == 160
    assert row['leverage'] == sender.LEVERAGE  # the one leverage both paths agree on


def test_env_file_ignores_sender_keys(tmp_path):
    path = tmp_path / '.env'
    names = ['BYBIT_API_KEY', 'BYBIT_API_SECRET', 'ANDY_SENDER_LIVE', 'ANDY_SENDER_NET']
    path.write_text('\n'.join(f'{name}=on' for name in names) + '\nOTHER=ok')
    env = {}
    load_env_file(path, env)
    assert env == {'OTHER': 'ok'}


@pytest.mark.parametrize('path,code,success', [('/v5/account/info', 10001, False),
    ('/v5/position/set-leverage', 110026, False), ('/v5/position/set-leverage', 110043, True)])
def test_return_codes(db, armed, path, code, success):
    api = Exchange(db)
    api.codes[path] = code
    result = send(db, intent(db), api)
    assert (result['status'] == 'filled') == success
    assert len([u for _,u,_ in api.calls if urlsplit(u).path == path]) == 1
    assert count(db) == int(success)


@pytest.mark.parametrize('setting,value,reason', [('margin_mode', 'REGULAR_MARGIN', 'isolated'),
    ('price', '104', '3%'), ('price', '', 'failed'), ('min_qty', '10', 'minimum'),
    ('min_notional', '200', 'minimum')])
def test_exchange_refusals(db, armed, setting, value, reason):
    api = Exchange(db)
    setattr(api, setting, value)
    result = send(db, intent(db), api)
    assert reason in result['reason']
    assert count(db) == 0 and not api.entries()


@pytest.mark.parametrize('size,notional', [(1, 16), (50, 16), (100, 32), (250, 80), (500, 160), (1000, 160)])
def test_size_scaling(db, armed, size, notional):
    assert send(db, intent(db, size=size), Exchange(db))['status'] == 'filled'
    row = db.execute('SELECT * FROM live_orders').fetchone()
    assert row['notional'] == notional
    assert row['margin'] <= 16


def test_rounding(db, armed):
    row = intent(db, stop=98.17, target=104.19)
    api = Exchange(db)
    api.price = '101'
    assert send(db, row, api)['status'] == 'filled'
    order = api.entries()[0]
    assert order['qty'] == '1.58' and order['stopLoss'] == '98.1' and order['takeProfit'] == '104.1'


def test_btc_min_qty(db, armed):
    row = intent(db, instrument='BTC-USD', stop=98000, target=104000)
    db.execute('UPDATE crypto_observations SET close=100000')
    api = Exchange(db)
    api.price, api.step, api.min_qty = '100000', '0.001', '0.002'
    result = send(db, row, api)
    assert 'BTCUSDT minimum' in result['reason']
    assert count(db) == 0


@pytest.mark.parametrize('changes', [{'stop': 94}, {'stop': 102}, {'target': 99},
                                    {'side': 'short', 'stop': 98, 'target': 104}])
def test_bad_brackets(db, armed, changes):
    # Raw SQL permits testing malformed upstream input independently of new_intent.
    row = dict(intent(db))
    row.update(changes)
    assert send(db, row, Exchange(db))['status'] == 'refused'
    assert count(db) == 0


def test_flat_raw_sql(db, armed):
    row = intent(db)
    db.execute("UPDATE order_intents SET side='flat'")
    row = db.execute('SELECT * FROM order_intents').fetchone()
    assert send(db, row, Exchange(db))['status'] == 'refused'
    assert count(db) == 0


def test_stale_and_expired(db, armed):
    row = intent(db)
    db.execute('UPDATE crypto_observations SET open_time=?', ((NOW-timedelta(minutes=91)).isoformat(),))
    assert '90 minutes' in send(db, row, Exchange(db))['reason']
    row = dict(row, expires_at=NOW.isoformat())
    assert 'expired' in send(db, row, Exchange(db))['reason']
    assert count(db) == 0


@pytest.mark.parametrize('severity', ['soft', 'hard', None])
def test_kill_switch(db, armed, severity):
    row = intent(db)
    risk.initialize_risk(db)
    db.execute('INSERT INTO risk_kill_switch(predictor,instrument,tripped,severity) VALUES (?,?,1,?)',
               ('p', 'SOL-USD', severity))
    db.commit()
    assert 'kill switch' in send(db, row, Exchange(db))['reason']
    assert count(db) == 0


@pytest.mark.parametrize('status', sender.TERMINAL + ('pending', 'error', 'timeout', 'sent', 'error_naked', 'error_naked_closed'))
def test_every_status_counts_hourly(db, armed, status):
    row = intent(db)
    for i in range(4):
        seed(db, 100+i, status=status)
    assert 'rolling hour' in send(db, row, Exchange(db))['reason']
    assert count(db) == 4


def test_daily_risk_rounded_candidate_and_utc_boundary(db, armed):
    row = intent(db, stop=98.09)
    seed(db, 100, age=90, qty=2.5, stop=98)  # 5 + 3.2 > 8
    assert 'daily' in send(db, row, Exchange(db))['reason']
    db.execute('UPDATE live_orders SET created_at=?', ((NOW-timedelta(days=1)).isoformat(),))
    db.commit()
    assert send(db, row, Exchange(db))['status'] == 'filled'


@pytest.mark.parametrize('status', ['pending', 'sent', 'timeout', 'error', 'error_naked', 'error_naked_closed'])
def test_nonterminal_blocks(db, armed, status):
    row = intent(db)
    seed(db, 100, status=status, instrument='SOL-USD')
    assert 'non-terminal' in send(db, row, Exchange(db))['reason']
    assert count(db) == 1


def test_position_and_duplicate(db, armed):
    row = intent(db)
    api = Exchange(db)
    api.existing_position = True
    assert 'existing position' in send(db, row, api)['reason']
    assert count(db) == 0
    api.existing_position = False
    api.position_reads = 0
    assert send(db, row, api)['status'] == 'filled'
    assert 'already' in send(db, row, Exchange(db))['reason']
    assert count(db) == 1


def test_transport_pending_no_resend(db, armed, caplog):
    row = intent(db)
    api = Exchange(db)
    api.transport_error = True
    assert send(db, row, api)['status'] == 'pending'
    assert db.execute('SELECT status FROM live_orders').fetchone()[0] == 'pending'
    again = Exchange(db)
    assert send(db, row, again)['status'] == 'refused'
    assert not again.entries()
    assert 'test-key-unique' not in caplog.text and 'test-secret-unique' not in caplog.text


@pytest.mark.parametrize('bybit,local', list(sender.FINAL_STATUS.items()))
def test_final_states(db, armed, bybit, local):
    api = Exchange(db)
    api.states = [bybit]
    assert send(db, intent(db), api)['status'] == local
    if bybit == 'PartiallyFilledCanceled':
        response = db.execute('SELECT exchange_response FROM live_orders').fetchone()[0]
        assert json.loads(response)['order']['cumExecQty'] == '0.4'


@pytest.mark.parametrize('state', ['New', 'PartiallyFilled', 'Untriggered'])
def test_pending_states_timeout(db, armed, state):
    api = Exchange(db)
    api.states = [state]
    pauses = []
    result = sender.send_live(db, intent(db), http=api, clock=lambda: NOW, sleep=pauses.append)
    assert result['status'] == 'timeout'
    assert pauses == [2]*14
    assert len([u for _,u,_ in api.calls if '/order/realtime' in u]) == 16
    assert len([u for _,u,_ in api.calls if '/order/history' in u]) == 16


def test_timeout_already_finished_reconciles(db, armed):
    api = Exchange(db)
    api.states = ['New']*30 + ['Filled']
    api.codes['/v5/order/cancel'] = 110001
    assert send(db, intent(db), api)['status'] == 'filled'


@pytest.mark.parametrize('empty_reads,naked,close_error,status', [(1, False, False, 'filled'),
    (3, False, False, 'error_naked_closed'), (0, True, False, 'error_naked_closed'),
    (0, True, True, 'error_naked')])
def test_naked_checks(db, armed, empty_reads, naked, close_error, status):
    api = Exchange(db)
    api.empty_reads, api.naked, api.close_error = empty_reads, naked, close_error
    assert send(db, intent(db), api)['status'] == status
    closes = [p for _,_,p in api.calls if p.get('reduceOnly')]
    if status != 'filled':
        assert api.position_reads == 4  # one preflight, three after fill
        assert closes[0]['side'] == 'Sell'
    else:
        assert not closes


def test_lock_held(db, armed):
    row = intent(db)
    with sender.sender_lock(sender.REPO_ROOT / '.sender.lock'):
        assert 'lock held' in sender.send_live(db, row)['reason']
    assert count(db) == 0
    assert send(db, row, Exchange(db))['status'] == 'filled'


def test_atomic_gate_reread_and_commit_before_http(db, armed, monkeypatch):
    row = intent(db)
    original = sender.database_gate
    calls = []
    def gate(*args):
        calls.append(db.in_transaction)
        original(*args)
    monkeypatch.setattr(sender, 'database_gate', gate)
    api = Exchange(db)
    api.create_hook = lambda: (pytest.fail('transaction still open') if db.in_transaction else None)
    assert send(db, row, api)['status'] == 'filled'
    assert calls == [False, True]


def test_transaction_gate_failure_leaves_no_row(db, armed, monkeypatch):
    row = intent(db)
    original = sender.database_gate
    def gate(*args):
        original(*args)
        if db.in_transaction:
            raise sender.Refused('changed under transaction')
    monkeypatch.setattr(sender, 'database_gate', gate)
    api = Exchange(db)
    assert send(db, row, api)['status'] == 'refused'
    assert count(db) == 0 and not api.entries()
    assert not db.in_transaction


def test_status_and_clear(tmp_path, armed, monkeypatch, capsys):
    import andy_trader.store as store
    path = tmp_path / 'orders.db'
    db = store.connect(path)
    sender.initialize_live_orders(db)
    seed(db, 123, status='pending')
    db.close()
    monkeypatch.setattr(store, 'default_database_path', lambda: path)
    sender.main(['status'])
    assert '123 OTHER pending age=' in capsys.readouterr().out
    sender.main(['clear', '123', '--reason', 'checked Bybit test-key-unique'])
    db = store.connect(path)
    row = db.execute('SELECT * FROM live_orders').fetchone()
    assert row['status'] == 'cleared_by_dawn'
    response = json.loads(row['exchange_response'])
    assert response['cleared_at'] == row['updated_at']
    assert 'test-key-unique' not in repr(dict(row))
    db.close()
    capsys.readouterr()
    sender.main(['status'])
    assert capsys.readouterr().out == ''


def test_loop_continues_after_sender_failure(db, armed, monkeypatch):
    intent(db)
    intent(db, instrument='ETH-USD')
    monkeypatch.setattr(executor, 'evaluate_paper_eligibility', lambda *a, **k: type('Gate', (), {'eligible': True})())
    seen = []
    def failing_http(*args):
        seen.append(1)
        raise OSError('secret')
    monkeypatch.setattr(sender, 'HTTP', failing_http)
    report = executor.run_dry_run(db, now_iso=NOW.isoformat(), live_sender=lambda c,r:
                                  sender.send_live(c,r,clock=lambda: NOW))
    assert report['checked'] == len(seen) == 2
    assert count(db) == 0


def test_restart_keeps_pending_and_refuses_duplicate(tmp_path, armed):
    import andy_trader.store as store
    path = tmp_path / 'restart.db'
    db = store.connect(path)
    row = dict(intent(db))
    api = Exchange(db)
    api.transport_error = True
    assert send(db, row, api)['status'] == 'pending'
    db.close()
    reopened = store.connect(path)
    api = Exchange(reopened)
    assert send(reopened, row, api)['status'] == 'refused'
    assert not api.entries()
    assert count(reopened) == 1
    reopened.close()


def test_write_lock_covers_gate_and_insert(tmp_path, armed, monkeypatch):
    import andy_trader.store as store
    path = tmp_path / 'atomic.db'
    db = store.connect(path)
    other = sqlite3.connect(path, timeout=0)
    row = intent(db)
    original = sender.database_gate
    observed = []
    def gate(*args):
        original(*args)
        if db.in_transaction:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('UPDATE risk_kill_switch SET tripped=1')
            other.rollback()
            observed.append(True)
    monkeypatch.setattr(sender, 'database_gate', gate)
    api = Exchange(db)
    def pending_visible():
        assert other.execute('SELECT status FROM live_orders').fetchone()[0] == 'pending'
    api.create_hook = pending_visible
    assert send(db, row, api)['status'] == 'filled'
    assert observed == [True]
    db.close()
    other.close()


def test_missing_flag_stays_off_even_with_legacy_constant(db, armed, monkeypatch):
    import andy_trader.store as store
    intent(db)
    monkeypatch.setattr(store, 'connect', lambda _: db)
    monkeypatch.setattr(executor, 'ALLOW_LIVE_SEND', True)
    monkeypatch.setattr(executor, 'evaluate_paper_eligibility', lambda *a, **k: type('Gate', (), {'eligible': True})())
    monkeypatch.setattr(executor, 'utc_now_iso', lambda: NOW.isoformat())
    monkeypatch.setattr(sender, 'send_live', lambda *a, **k: pytest.fail('missing --live called sender'))
    monkeypatch.setattr(executor, 'send_order', lambda *a, **k: pytest.fail('legacy stub called'))
    executor.main([])
    assert count(db) == 0


def test_expiry_checked_again_after_exchange_preflight(db, armed):
    row = intent(db)
    api = Exchange(db)
    now = [NOW]
    api.before_leverage = lambda: now.__setitem__(0, NOW + timedelta(minutes=6))
    result = sender.send_live(db, row, http=api, clock=lambda: now[0], sleep=lambda _: None)
    assert 'expired' in result['reason']
    assert count(db) == 0 and not api.entries()


def test_kill_switch_other_pair_does_not_block(db, armed):
    row = intent(db)
    risk.initialize_risk(db)
    db.execute("INSERT INTO risk_kill_switch(predictor,instrument,tripped) VALUES ('other','SOL-USD',1)")
    db.commit()
    assert send(db, row, Exchange(db))['status'] == 'filled'


def test_entry_rejection_and_unknown_codes_are_not_retried(db, armed):
    api = Exchange(db)
    api.codes['/v5/order/create'] = 110043  # only leverage endpoint may accept this
    assert send(db, intent(db), api)['status'] == 'rejected'
    assert len(api.entries()) == 1
    assert db.execute('SELECT status FROM live_orders').fetchone()[0] == 'rejected'


def test_short_brackets_and_close_side(db, armed):
    api = Exchange(db)
    api.naked = True
    assert send(db, intent(db, side='short', stop=102, target=96), api)['status'] == 'error_naked_closed'
    assert api.entries()[0]['side'] == 'Sell'
    assert next(p for _,_,p in api.calls if p.get('reduceOnly'))['side'] == 'Buy'


def test_daily_risk_exact_limit_allowed(db, armed):
    row = intent(db)
    seed(db, 100, age=90, qty=2.4, stop=98)  # 4.8 + 3.2 = 8
    assert send(db, row, Exchange(db))['status'] == 'filled'


def test_response_redaction(db, armed, caplog, capsys):
    api = Exchange(db)
    row = intent(db)
    def http(method, url, headers, body):
        response = api(method, url, headers, body)
        response['echo'] = repr(headers) + 'test-secret-unique'
        if '/order/realtime' in url:
            response['result']['list'][0]['debug'] = response['echo']
        return response
    result = send(db, row, http)
    output = repr(result) + caplog.text + capsys.readouterr().out
    output += repr([dict(r) for r in db.execute('SELECT * FROM live_orders')])
    assert 'test-key-unique' not in output
    assert 'test-secret-unique' not in output
    assert '[REDACTED]' in output
    assert '[REDACTED]' not in repr(result)  # success result contains no request data


def test_nonfinite_intent_fails_closed(db, armed):
    row = dict(intent(db), size=float('nan'))
    assert send(db, row, Exchange(db))['status'] == 'refused'
    assert count(db) == 0


def test_rounded_target_cannot_disable_protection(db, armed):
    row = intent(db, side='short', stop=102, target=0.01)
    api = Exchange(db)
    assert send(db, row, api)['status'] == 'refused'
    assert count(db) == 0 and not api.entries()


class NoWrites(Exchange):
    """Read-only preflight is allowed; nothing may be *sent* once refused.

    Every mutating Bybit call in the sender is a POST -- set-leverage and
    order/create -- so a POST arriving here is the bug this guards.
    """

    def __call__(self, method, url, headers, body):
        if method != 'GET':
            pytest.fail(f'refused intent still sent {method} {urlsplit(url).path}')
        return super().__call__(method, url, headers, body)


@pytest.mark.parametrize('block,reason', [('kill_switch', 'kill switch'), ('hourly', 'rolling hour'),
                                          ('daily', 'daily'), ('expired', 'expired')])
def test_early_gate_refuses_before_the_leverage_change(db, armed, block, reason):
    """set-leverage changes exchange state, so the database gate runs first."""
    row = intent(db)
    if block == 'kill_switch':
        risk.initialize_risk(db)
        db.execute("INSERT INTO risk_kill_switch(predictor,instrument,tripped,severity) "
                   "VALUES ('p','SOL-USD',1,'soft')")
        db.commit()
    elif block == 'hourly':
        for i in range(sender.MAX_ORDERS_PER_HOUR):
            seed(db, 100 + i)
    elif block == 'daily':
        seed(db, 100, age=90, qty=2.5, stop=98)  # 5 + 3.2 > 8
    else:
        row = dict(row, expires_at=NOW.isoformat())
    api = NoWrites(db)
    result = send(db, row, api)
    assert result['status'] == 'refused' and reason in result['reason']
    assert not [u for _, u, _ in api.calls if u.endswith('/set-leverage')]
    assert not api.entries()


def test_gate_runs_before_set_leverage_and_again_in_the_transaction(db, armed, monkeypatch):
    """The early gate is an addition, not a replacement: both still run, and
    the first one lands before the leverage change reaches the exchange."""
    row = intent(db)
    original = sender.database_gate
    api = Exchange(db)
    gate_order = []
    def gate(*args):
        gate_order.append((len(api.calls), db.in_transaction))
        original(*args)
    monkeypatch.setattr(sender, 'database_gate', gate)
    assert send(db, row, api)['status'] == 'filled'
    leverage_at = [i for i, (_, u, _) in enumerate(api.calls) if u.endswith('/set-leverage')]
    assert len(leverage_at) == 1
    assert [in_tx for _, in_tx in gate_order] == [False, True]
    assert gate_order[0][0] <= leverage_at[0]  # gated before the leverage POST


@pytest.mark.parametrize('leverage', [1, 2, 9.5, 11, 20])
def test_refuses_any_intent_leverage_but_the_constant(db, armed, leverage):
    row = dict(intent(db), leverage=leverage)
    api = NoWrites(db)
    result = send(db, row, api)
    assert result['status'] == 'refused' and 'leverage' in result['reason']
    assert count(db) == 0 and not api.calls  # refused before any request at all


def test_accepts_exactly_the_constant_leverage(db, armed):
    assert send(db, intent(db, leverage=sender.LEVERAGE), Exchange(db))['status'] == 'filled'
    assert sender.LEVERAGE == executor.REQUIRED_LEVERAGE
