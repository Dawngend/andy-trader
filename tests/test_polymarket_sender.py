"""All sender tests are offline, including armed live sends."""
from datetime import timedelta
from decimal import Decimal
import json
import sqlite3
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from andy_trader import polymarket_sender as s

HASH = '0x' + 'a' * 64
KEY = '0x' + 'b' * 64
INTENT = dict(token_id='12345', price=Decimal('.50'), shares=Decimal('10'), reason='manual test')


class ApiError(Exception):
    def __init__(self, code, body='exchange error'):
        self.status_code, self.error_msg = code, body


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ('ANDY_SENDER_LIVE', 'POLYMARKET_PRIVATE_KEY', 'POLYMARKET_FUNDER_ADDRESS', 'POLYMARKET_SIGNATURE_TYPE'):
        monkeypatch.delenv(name, raising=False)
    s.SECRETS.clear()
    monkeypatch.setattr(s, 'read_json', lambda url: pytest.fail('unexpected network'))
    types = ModuleType('py_clob_client.clob_types')
    types.MarketOrderArgs = types.PartialCreateOrderOptions = SimpleNamespace
    types.OrderType = SimpleNamespace(FOK='FOK')
    exceptions = ModuleType('py_clob_client.exceptions')
    exceptions.PolyApiException = ApiError
    constants = ModuleType('py_clob_client.order_builder.constants')
    constants.BUY = 'BUY'
    for name, module in ((types.__name__, types), (exceptions.__name__, exceptions), (constants.__name__, constants)):
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / 'orders.db'
    limits = tmp_path / 'polymarket_sender_limits.json'
    limits.write_text(json.dumps(dict(max_usdc_per_order=5, max_usdc_per_day=10, max_orders_per_hour=2)))
    for key, value in dict(ANDY_SENDER_LIVE='on', POLYMARKET_PRIVATE_KEY=KEY,
                           POLYMARKET_FUNDER_ADDRESS='public-wallet', POLYMARKET_SIGNATURE_TYPE='0').items():
        monkeypatch.setenv(key, value)
    with s.database(path):
        pass
    calls = []
    probe_lock = threading.Lock()

    def unlocked(name):
        with probe_lock, sqlite3.connect(path, timeout=0) as c:
            c.execute('BEGIN IMMEDIATE')
        calls.append(name)

    def http(url):
        unlocked(url)
        if url == s.GEOBLOCK:
            return dict(blocked=False, country='PH', region='', ip='never-log-this-ip')
        return dict(asks=[{'price': '.70'}, {'price': '.49'}, {'price': '.60'}],
                    tick_size='.01', min_order_size='5')

    class Client:
        response = dict(success=True, status='matched', orderID=HASH)
        exception = None
        delay = 0
        order = None
        trades = []

        def derive_api_key(self):
            unlocked('derive')
            return SimpleNamespace(api_key='derived-key-123', api_secret='derived-secret-123', api_passphrase='derived-passphrase-123')

        def set_api_creds(self, creds):
            unlocked('set creds')

        def get_tick_size(self, token):
            unlocked('tick')
            return '.01'

        def get_neg_risk(self, token):
            unlocked('neg risk')
            return False

        def get_fee_rate_bps(self, token):
            unlocked('fee')
            return 0

        def create_market_order(self, args, options):
            unlocked('sign')
            self.args, self.options = args, options
            return 'signed'

        def post_order(self, signed, order_type):
            unlocked('post')
            assert signed == 'signed' and order_type == 'FOK'
            time.sleep(self.delay)
            if self.exception:
                raise self.exception
            return self.response

        def get_order(self, order_id):
            unlocked('order lookup')
            assert order_id == HASH
            if isinstance(self.order, Exception):
                raise self.order
            return self.order

        def get_trades(self):
            unlocked('trades')
            if isinstance(self.trades, Exception):
                raise self.trades
            return self.trades

    client = Client()
    kwargs = dict(database_path=path, limits_path=limits, http=http, client_factory=lambda **kw: client)
    return SimpleNamespace(path=path, limits=limits, client=client, calls=calls, kwargs=kwargs)


def rows(setup):
    with s.database(setup.path) as c:
        return [dict(row) for row in c.execute('SELECT * FROM polymarket_sender_orders ORDER BY id')]


def send(setup, **changes):
    return s.send_buy({**INTENT, **changes}, live=True, **setup.kwargs)


def add_row(setup, status, age=0, order_id=None):
    with s.database(setup.path) as c:
        ident = s.insert(c, INTENT, '5.00', status, order_id=order_id)
        c.execute('UPDATE polymarket_sender_orders SET created_at=? WHERE id=?',
                  ((s.utc_now() - timedelta(minutes=age)).isoformat(), ident))
        c.commit()
    return ident


def test_dry_run_no_credentials_or_region(setup, monkeypatch, capsys):
    monkeypatch.setattr(s, 'credentials', lambda: pytest.fail('credentials read'))
    monkeypatch.setattr(s, 'check_region', lambda _: pytest.fail('region checked'))
    result = s.send_buy(INTENT, **setup.kwargs)
    assert result['status'] == rows(setup)[0]['status'] == 'dry_run'
    assert 'region not checked' in capsys.readouterr().out
    assert not any(call in setup.calls for call in ('derive', 'sign', 'post'))


def test_live_arguments_and_order(setup):
    assert send(setup)['status'] == 'filled'
    args, options = setup.client.args, setup.client.options
    assert (args.token_id, args.amount, args.price, args.side, args.order_type) == ('12345', 5.0, .5, 'BUY', 'FOK')
    assert (options.tick_size, options.neg_risk, args.fee_rate_bps) == ('.01', False, 0)
    assert setup.calls[-2:] == ['sign', 'post']
    assert setup.calls.count('post') == 1
    assert rows(setup)[0]['order_id'] == HASH


@pytest.mark.parametrize('problem', ['gate', 'file', 'key', 'stop', 'private', 'funder', 'signature'])
def test_missing_gate_or_configuration(setup, monkeypatch, problem):
    if problem == 'gate':
        monkeypatch.setenv('ANDY_SENDER_LIVE', 'ON')
    elif problem == 'file':
        setup.limits.unlink()
    elif problem == 'key':
        setup.limits.write_text('{}')
    elif problem == 'stop':
        setup.limits.with_name('polymarket_sender.STOP').touch()
    else:
        monkeypatch.delenv({'private': 'POLYMARKET_PRIVATE_KEY', 'funder': 'POLYMARKET_FUNDER_ADDRESS',
                           'signature': 'POLYMARKET_SIGNATURE_TYPE'}[problem])
    with pytest.raises(s.Refused):
        send(setup)
    assert rows(setup)[0]['status'] == 'refused'
    assert 'post' not in setup.calls


@pytest.mark.parametrize('key', ['max_usdc_per_order', 'max_usdc_per_day', 'max_orders_per_hour'])
@pytest.mark.parametrize('bad', ['true', '"5"', 'null', 'NaN', 'Infinity', '0', '-1'])
def test_invalid_limits(setup, key, bad):
    data = {'max_usdc_per_order': '5', 'max_usdc_per_day': '10', 'max_orders_per_hour': '2'}
    data[key] = bad
    setup.limits.write_text('{' + ','.join(json.dumps(k) + ':' + v for k, v in data.items()) + '}')
    with pytest.raises(s.Refused, match='invalid limits: ' + key):
        send(setup)
    assert 'post' not in setup.calls


def test_fractional_hour_limit(setup):
    setup.limits.write_text('{"max_usdc_per_order":5,"max_usdc_per_day":10,"max_orders_per_hour":1.1}')
    with pytest.raises(s.Refused, match='max_orders_per_hour'):
        send(setup)


@pytest.mark.parametrize('signature', ['3', '-1', '00', ' 0', '1.0', 'false'])
def test_signature(setup, monkeypatch, signature):
    monkeypatch.setenv('POLYMARKET_SIGNATURE_TYPE', signature)
    with pytest.raises(s.Refused, match='SIGNATURE_TYPE'):
        send(setup)


@pytest.mark.parametrize('cap', ['max_usdc_per_order', 'max_usdc_per_day', 'max_orders_per_hour'])
def test_cap_exact_boundary_and_above(setup, cap):
    limits = dict(max_usdc_per_order=100, max_usdc_per_day=100, max_orders_per_hour=100)
    limits[cap] = 1 if cap == 'max_orders_per_hour' else 5
    setup.limits.write_text(json.dumps(limits))
    assert send(setup)['status'] == 'filled'
    with pytest.raises(s.Refused, match=cap):
        send(setup, shares='10.02' if cap == 'max_usdc_per_order' else '10')


@pytest.mark.parametrize('status', ['dry_run', 'refused', 'rejected', 'cancelled', 'cleared_by_operator'])
def test_noncounted_statuses(setup, status):
    for _ in range(3):
        add_row(setup, status)
    assert send(setup)['status'] == 'filled'


@pytest.mark.parametrize('status', s.COUNTED)
def test_counted_statuses(setup, status):
    add_row(setup, status)
    add_row(setup, status)
    with pytest.raises(s.Refused, match='cap'):
        send(setup)


def test_rolling_windows(setup):
    add_row(setup, 'filled', age=1441)
    add_row(setup, 'filled', age=61)
    assert send(setup)['status'] == 'filled'


def test_two_racing_sends(setup):
    setup.limits.write_text('{"max_usdc_per_order":5,"max_usdc_per_day":5,"max_orders_per_hour":2}')
    barrier = threading.Barrier(2)
    def post(signed, order_type):
        setup.calls.append('post')
        return setup.client.response
    setup.client.post_order = post
    sign = s.sign_order
    def signer(*args):
        result = sign(*args)
        barrier.wait(timeout=5)
        return result
    setup.kwargs['signer'] = signer
    results = []
    def run():
        try:
            results.append(send(setup)['status'])
        except s.Refused:
            results.append('refused')
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == ['filled', 'refused']
    assert setup.calls.count('post') == 1


def test_busy_sends_nothing(setup, monkeypatch):
    # Lock after every preflight call, immediately before reservation.
    lock = sqlite3.connect(setup.path)
    original = s.sign_order
    def signer(*args):
        result = original(*args)
        lock.execute('BEGIN IMMEDIATE')
        return result
    setup.kwargs['signer'] = signer
    monkeypatch.setattr(s, 'BUSY_TIMEOUT', .02)
    try:
        with pytest.raises(s.Refused, match='^busy$'):
            send(setup)
    finally:
        lock.close()
    assert rows(setup) == []
    assert 'post' not in setup.calls


@pytest.mark.parametrize('response,status', [
    ({'success': True, 'status': 'matched'}, 'filled'),
    *[({'success': True, 'status': x}, 'sent') for x in ('delayed', 'live', 'unmatched')],
    ({'success': False, 'errorMsg': 'minimum size'}, 'rejected'),
    ({}, 'unknown'), (None, 'unknown'), ('not json', 'unknown')])
def test_post_mapping(setup, response, status):
    setup.client.response = response
    assert send(setup)['status'] == status
    if status == 'rejected':
        assert rows(setup)[0]['error'] == 'minimum size'


@pytest.mark.parametrize('code,status', [(400, 'rejected'), (429, 'rejected'), (500, 'unknown'),
                                       (408, 'unknown'), (None, 'unknown'), ('400', 'unknown')])
def test_http_errors(setup, code, status):
    setup.client.exception = ApiError(code, KEY + ' derived-secret-123')
    assert send(setup)['status'] == status
    assert KEY not in str(rows(setup))
    assert 'derived-secret-123' not in str(rows(setup))


def test_timeout_and_late_answer(setup):
    setup.client.delay = .15
    setup.kwargs['post_timeout'] = .01
    result = send(setup)
    assert result['status'] == 'unknown' and result['order_id'] is None
    time.sleep(.25)
    assert len(rows(setup)) == 1
    assert rows(setup)[0]['status'] == 'filled'
    assert setup.calls.count('post') == 1


@pytest.mark.parametrize('reject', [False, True])
def test_late_answer_after_clear(setup, reject, capsys):
    setup.client.delay = .15
    setup.kwargs['post_timeout'] = .01
    if reject:
        setup.client.response = {'success': False, 'errorMsg': 'rejected'}
    result = send(setup)
    s.clear_stuck(result['id'], 'checked exchange', True, database_path=setup.path)
    time.sleep(.25)
    records = rows(setup)
    assert records[0]['status'] == 'cleared_by_operator'
    assert len(records) == (1 if reject else 2)
    if not reject:
        assert records[1]['status'] == 'filled' and records[1]['order_id'] == HASH
        assert records[1]['reason'] == f"late answer for cleared row {result['id']}"
    else:
        assert 'late answer' in capsys.readouterr().out


def test_answer_during_timeout_transition(setup, monkeypatch):
    # Force wait to report timeout after the worker has already saved its answer.
    real_event = threading.Event
    class TimeoutEvent:
        def __init__(self):
            self.event = real_event()
        def set(self):
            self.event.set()
        def wait(self, timeout):
            assert self.event.wait(3)
            return False
    monkeypatch.setattr(s.threading, 'Event', TimeoutEvent)
    # Thread itself uses Event, so preserve the native Thread implementation's event.
    real_thread = threading.Thread
    def thread(*args, **kwargs):
        monkeypatch.setattr(s.threading, 'Event', real_event)
        return real_thread(*args, **kwargs)
    monkeypatch.setattr(s.threading, 'Thread', thread)
    assert send(setup)['status'] == 'filled'
    assert len(rows(setup)) == 1


def test_id_saved_before_response_mapping(setup, monkeypatch):
    original = s.response_status
    def inspect(response):
        assert rows(setup)[0]['order_id'] == HASH
        return original(response)
    monkeypatch.setattr(s, 'response_status', inspect)
    assert send(setup)['status'] == 'filled'


@pytest.mark.parametrize('status', ['pending', 'unknown', 'sent'])
def test_clear_confirmation_and_reason(setup, status):
    ident = add_row(setup, status, age=16)
    for reason, confirm in [('checked', False), (' ', True), (None, True)]:
        with pytest.raises(s.Refused):
            s.clear_stuck(ident, reason, confirm, database_path=setup.path)
    s.clear_stuck(ident, 'checked exchange', True, database_path=setup.path)
    assert rows(setup)[0]['clear_reason'] == 'checked exchange'
    assert rows(setup)[0]['status'] == 'cleared_by_operator'


@pytest.mark.parametrize('status', ['filled', 'cancelled', 'rejected', 'refused', 'dry_run', 'cleared_by_operator'])
def test_cannot_clear_terminal(setup, status):
    ident = add_row(setup, status, age=16)
    with pytest.raises(s.Refused):
        s.clear_stuck(ident, 'checked', True, database_path=setup.path)


def test_young_pending_cannot_clear(setup):
    ident = add_row(setup, 'pending')
    with pytest.raises(s.Refused, match='15 minutes'):
        s.clear_stuck(ident, 'checked', True, database_path=setup.path)


def test_clear_frees_cap_space(setup):
    ids = [add_row(setup, 'unknown') for _ in range(2)]
    with pytest.raises(s.Refused):
        send(setup)
    s.clear_stuck(ids[0], 'checked', True, database_path=setup.path)
    assert send(setup)['status'] == 'filled'


@pytest.mark.parametrize('geo', [dict(blocked=True, country='PH', region=''),
    dict(blocked=False, country='US', region='NY'), dict(blocked=False, country='ca', region='on'),
    dict(blocked='false', country='PH', region=''), dict(country='PH', region=''),
    dict(blocked=False, country=None, region=''), dict(blocked=None, country='PH', region=''), [], None])
def test_region_refusal(setup, geo):
    setup.kwargs['http'] = lambda url: geo
    with pytest.raises(s.Refused, match='region'):
        send(setup)
    assert 'post' not in setup.calls


def test_region_failure_drops_ip(setup, capsys):
    def http(url):
        raise TimeoutError('sensitive-ip')
    setup.kwargs['http'] = http
    with pytest.raises(s.Refused):
        send(setup)
    assert 'sensitive-ip' not in str(rows(setup)) + capsys.readouterr().out


@pytest.mark.parametrize('price,shares,reason', [('.501', '10', 'tick'), ('.5', '4.99', 'minimum'),
    ('.01', '10', 'band'), ('.99', '10', 'band'), ('.53', '10', 'band')])
def test_price_and_minimum_rules(setup, price, shares, reason):
    with pytest.raises(s.Refused, match=reason):
        send(setup, price=price, shares=shares)


def test_round_down(setup):
    result = send(setup, shares='10.019')
    assert result['usdc_cost'] == '5.00' and setup.client.args.amount == 5.0


@pytest.mark.parametrize('book', [{}, {'asks': []}, {'asks': [{'price': 'NaN'}]}, {'asks': [{'price': 'garbage'}]}])
def test_no_book_dry_run(setup, book, capsys):
    setup.kwargs['http'] = lambda _: book
    with pytest.raises(s.Refused, match='no book'):
        s.send_buy(INTENT, **setup.kwargs)
    assert rows(setup)[0]['status'] == 'refused'
    assert 'no book' in capsys.readouterr().out


def test_redact_exact_secrets_only(setup, capsys):
    send(setup)
    text = f'{KEY} {KEY[2:]} derived-key-123 derived-secret-123 derived-passphrase-123 {HASH} .50 12345'
    clean = s.redact(text, ['0', '123'])
    assert KEY not in clean and KEY[2:] not in clean and 'derived-' not in clean
    assert HASH in clean and '.50 12345' in clean
    assert rows(setup)[0]['order_id'] == HASH


@pytest.mark.parametrize('order,trades,expected', [
    (None, [], 'unknown'), ({}, [], 'unknown'),
    ({'id': HASH, 'status': 'CANCELED', 'size_matched': '0'}, [], 'cancelled'),
    ({'id': HASH, 'status': 'CANCELED', 'size_matched': '2'}, [], 'filled'),
    ({'id': HASH, 'status': 'MATCHED'}, [], 'filled'),
    (None, [{'taker_order_id': HASH, 'status': 'CONFIRMED'}], 'filled'),
    (None, [{'maker_orders': [{'order_id': HASH, 'matched_amount': '1'}], 'status': 'MATCHED'}], 'filled'),
    (TimeoutError(), [{'taker_order_id': HASH, 'status': 'CONFIRMED'}], 'unknown'),
    ({'id': HASH, 'status': 'MATCHED'}, TimeoutError(), 'unknown'),
    ({'id': HASH, 'status': 'MATCHED'}, {}, 'unknown'),
    ('malformed', [], 'unknown')])
def test_reconcile_positive_evidence(setup, order, trades, expected):
    add_row(setup, 'unknown', order_id=HASH)
    setup.client.order, setup.client.trades = order, trades
    s.reconcile(database_path=setup.path, client_factory=setup.kwargs['client_factory'])
    assert rows(setup)[0]['status'] == expected
    assert 'order lookup' in setup.calls and 'trades' in setup.calls
    assert 'post' not in setup.calls


def test_reconcile_skips_missing_id_and_cleared(setup):
    for status in ('pending', 'unknown', 'cleared_by_operator'):
        add_row(setup, status, order_id=HASH if status == 'cleared_by_operator' else None)
    s.reconcile(database_path=setup.path, client_factory=setup.kwargs['client_factory'])
    assert [r['status'] for r in rows(setup)] == ['pending', 'unknown', 'cleared_by_operator']
    assert 'order lookup' not in setup.calls


def test_reconcile_clear_during_lookup(setup):
    ident = add_row(setup, 'unknown', order_id=HASH)
    def lookup(order_id):
        s.clear_stuck(ident, 'checked', True, database_path=setup.path)
        return {'id': HASH, 'status': 'MATCHED'}
    setup.client.get_order = lookup
    s.reconcile(database_path=setup.path, client_factory=setup.kwargs['client_factory'])
    assert rows(setup)[0]['status'] == 'cleared_by_operator'


def test_reconcile_gate_and_ignores_stop(setup, monkeypatch):
    setup.limits.with_name('polymarket_sender.STOP').touch()
    s.reconcile(database_path=setup.path, client_factory=setup.kwargs['client_factory'])
    monkeypatch.delenv('ANDY_SENDER_LIVE')
    with pytest.raises(s.Refused):
        s.reconcile(database_path=setup.path, client_factory=setup.kwargs['client_factory'])


def test_status_invalid_limits_and_no_id(setup, capsys):
    add_row(setup, 'unknown')
    setup.limits.unlink()
    s.show_status(database_path=setup.path, limits_path=setup.limits)
    output = capsys.readouterr().out
    assert 'age=' in output and 'no order id' in output and 'limits: invalid (file)' in output


def test_cli_exclusion_and_no_action(monkeypatch):
    monkeypatch.setattr(s, 'send_buy', lambda *a, **kw: pytest.fail('sent'))
    with pytest.raises(SystemExit) as exc:
        s.main(['--dry-run-demo', '--live'])
    assert exc.value.code == 2
    assert s.main([]) == 0


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '0', '-1'])
def test_cli_rejects_bad_decimal(value):
    with pytest.raises(SystemExit):
        s.main(['--live', '--price', value])


def process_send(path, limits, barrier, results):
    client = SimpleNamespace(
        derive_api_key=lambda: SimpleNamespace(api_key='derived-key', api_secret='derived-secret', api_passphrase='derived-pass'),
        set_api_creds=lambda creds: None,
        get_tick_size=lambda token: '.01', get_neg_risk=lambda token: False,
        get_fee_rate_bps=lambda token: 0,
        post_order=lambda signed, kind: {'success': True, 'status': 'matched', 'orderID': HASH})
    def http(url):
        if url == s.GEOBLOCK:
            return dict(blocked=False, country='PH', region='')
        return dict(asks=[{'price': '.50'}], tick_size='.01', min_order_size='5')
    def sign(*args):
        barrier.wait(10)
        return 'signed'
    try:
        result = s.send_buy(INTENT, live=True, database_path=path, limits_path=limits,
                            http=http, client_factory=lambda **kw: client, signer=sign)
        results.put(result['status'])
    except s.Refused:
        results.put('refused')


def test_two_processes_cannot_spend_same_cap(setup):
    import multiprocessing
    setup.limits.write_text('{"max_usdc_per_order":5,"max_usdc_per_day":5,"max_orders_per_hour":2}')
    context = multiprocessing.get_context('spawn')
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=process_send, args=(setup.path, setup.limits, barrier, results)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        if process.is_alive():
            process.terminate()
            pytest.fail('sender process hung')
        assert process.exitcode == 0
    assert sorted(results.get(timeout=2) for _ in processes) == ['filled', 'refused']
    assert sum(row['status'] == 'filled' for row in rows(setup)) == 1


def test_unknown_timeout_keeps_cap_and_never_retries(setup):
    release = threading.Event()
    entered = threading.Event()
    def post(signed, kind):
        setup.calls.append('post')
        entered.set()
        release.wait(5)
        return {'success': True, 'status': 'matched', 'orderID': HASH}
    setup.client.post_order = post
    setup.kwargs['post_timeout'] = .01
    setup.limits.write_text('{"max_usdc_per_order":5,"max_usdc_per_day":5,"max_orders_per_hour":2}')
    try:
        assert send(setup)['status'] == 'unknown'
        assert entered.is_set()
        with pytest.raises(s.Refused, match='cap'):
            send(setup)
        s.reconcile(database_path=setup.path, client_factory=setup.kwargs['client_factory'])
        assert rows(setup)[0]['status'] == 'unknown'
        assert setup.calls.count('post') == 1
    finally:
        release.set()
        time.sleep(.1)


def test_worker_busy_keeps_reservation(setup, monkeypatch, capsys):
    release = threading.Event()
    answered = threading.Event()
    def post(signed, kind):
        release.wait(3)
        answered.set()
        return setup.client.response
    setup.client.post_order = post
    setup.kwargs['post_timeout'] = .01
    monkeypatch.setattr(s, 'BUSY_TIMEOUT', .02)
    assert send(setup)['status'] == 'unknown'
    with sqlite3.connect(setup.path) as lock:
        lock.execute('BEGIN IMMEDIATE')
        release.set()
        assert answered.wait(1)
        time.sleep(.15)
    assert rows(setup)[0]['status'] == 'unknown'
    assert 'busy' in capsys.readouterr().out


def test_late_exception_for_cleared_row(setup, capsys):
    setup.client.delay = .15
    setup.client.exception = ApiError(400, 'derived-secret-123')
    setup.kwargs['post_timeout'] = .01
    result = send(setup)
    s.clear_stuck(result['id'], 'checked', True, database_path=setup.path)
    time.sleep(.25)
    assert len(rows(setup)) == 1 and rows(setup)[0]['status'] == 'cleared_by_operator'
    output = capsys.readouterr().out
    assert 'late answer' in output and 'derived-secret-123' not in output


def test_demo_uses_public_open_token(setup, monkeypatch):
    reader = setup.kwargs['http']
    def http(url):
        if url.endswith('/sampling-markets'):
            return {'data': [dict(active=True, closed=False, accepting_orders=True,
                                  tokens=[{'token_id': '12345'}])]}
        return reader(url)
    monkeypatch.setattr(s, 'read_json', http)
    assert s.main(['--dry-run-demo', '--database-path', str(setup.path)]) == 0
    assert rows(setup)[0]['status'] == 'dry_run'
    assert s.GEOBLOCK not in setup.calls and 'post' not in setup.calls


def test_cli_live_preserves_decimal(monkeypatch):
    calls = []
    def send(intent, **kwargs):
        calls.append((intent, kwargs))
        return {'status': 'filled'}
    monkeypatch.setattr(s, 'send_buy', send)
    assert s.main(['--live', '--token-id', '1', '--price', '.123456789123456789',
                   '--shares', '10.123456789123456789', '--reason', 'manual']) == 0
    assert len(calls) == 1 and calls[0][1]['live'] is True
    assert calls[0][0]['price'] == Decimal('.123456789123456789')
    assert calls[0][0]['shares'] == Decimal('10.123456789123456789')


def test_pending_answer_saved_without_duplicate(setup):
    ident = add_row(setup, 'pending')
    s.save_answer(setup.path, ident, setup.client.response)
    assert len(rows(setup)) == 1 and rows(setup)[0]['status'] == 'filled'


def test_malformed_lookup_does_not_free_cap(setup):
    add_row(setup, 'unknown', order_id=HASH)
    setup.client.order = {'error': 'unexpected shape'}
    setup.client.trades = [{'taker_order_id': HASH, 'status': 'CONFIRMED'}]
    s.reconcile(database_path=setup.path, client_factory=setup.kwargs['client_factory'])
    assert rows(setup)[0]['status'] == 'unknown'


def test_all_secrets_removed_from_ledger_and_output(setup, capsys):
    secret_text = f'{KEY} {KEY[2:]} derived-key-123 derived-secret-123 derived-passphrase-123'
    setup.client.response = {'success': False, 'errorMsg': secret_text, 'orderID': HASH}
    result = send(setup, reason=secret_text)
    print(s.redact(result))
    output = str(rows(setup)) + capsys.readouterr().out
    for secret in secret_text.split():
        assert secret not in output
    assert HASH in output
