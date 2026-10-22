"""Opt-in Bybit V5 sender. The CLI executor supplies this seam only with --live."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from andy_trader import risk
from andy_trader.collector import _BYBIT_SYMBOLS
from andy_trader.env import REPO_ROOT

LEVERAGE = 10
MAX_MARGIN_USDT = 16
MAX_DAILY_RISK_USDT = 8
MAX_ORDERS_PER_HOUR = 4
CRYPTO_MAX_DATA_AGE_MINUTES = 90
RECV_WINDOW = '5000'
# https://bybit-exchange.github.io/docs/v5/error
LEVERAGE_NOT_MODIFIED = 110043
ORDER_NOT_FOUND = 110001  # Not proof of cancellation: always reconcile again.
TERMINAL = ('filled', 'cancelled', 'rejected', 'cleared_by_dawn')
FINAL_STATUS = {'Filled': 'filled', 'Cancelled': 'cancelled',
                'Deactivated': 'cancelled', 'PartiallyFilledCanceled': 'cancelled',
                'Rejected': 'rejected'}
LOG = logging.getLogger(__name__)


class Refused(ValueError):
    pass


def redact(value, secrets=()):
    text = str(value)
    values = (*secrets, os.environ.get('BYBIT_API_KEY', ''),
              os.environ.get('BYBIT_API_SECRET', ''))
    for secret in sorted((str(s) for s in values if s), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    return re.sub(r'\b[0-9a-fA-F]{64}\b', '[REDACTED]', text)


def utc_now():
    return datetime.now(UTC)


def parsed_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise Refused('non-finite number')
    return result


def positive(value):
    result = number(value)
    if result <= 0:
        raise Refused('expected a positive number')
    return result


def rounded(value, step):
    return (value / positive(step)).to_integral_value(rounding=ROUND_DOWN) * positive(step)


def initialize_live_orders(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS live_orders (
        id INTEGER PRIMARY KEY, intent_id INTEGER NOT NULL UNIQUE,
        order_link_id TEXT NOT NULL, instrument TEXT NOT NULL, side TEXT NOT NULL,
        qty REAL NOT NULL, notional REAL NOT NULL, margin REAL NOT NULL,
        entry_price REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        exchange_response TEXT NOT NULL)''')
    connection.commit()


@contextmanager
def sender_lock(path):
    # Keep the inode/file in place. OS releases the held byte/flock on process exit.
    handle = open(path, 'a+b')
    locked = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise Refused('sender lock held') from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def urllib_http(method, url, headers, body):
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=15) as response:
        return json.load(response)


# Runtime default is replaceable by tests; no requests/session dependency.
HTTP = urllib_http


def database_gate(connection, intent, qty, price, stop, now):
    if parsed_time(intent['expires_at']) <= now:
        raise Refused('intent expired')
    # fetch_kill_switch_state initializes tables and commits. Never call it inside
    # BEGIN IMMEDIATE; re-read the same pair directly without releasing that lock.
    if connection.in_transaction:
        state = connection.execute('SELECT tripped FROM risk_kill_switch '
                                   'WHERE predictor=? AND instrument=?',
                                   (intent['predictor'], intent['instrument'])).fetchone()
    else:
        state = risk.fetch_kill_switch_state(connection, predictor=intent['predictor'],
                                            instrument=intent['instrument'])
    if state and state['tripped']:
        raise Refused('kill switch tripped for this predictor/instrument')
    rows = connection.execute('SELECT * FROM live_orders').fetchall()
    hourly = 0
    daily = Decimal(0)
    day_start = parsed_time(risk._day_start_iso(now.isoformat()))
    for row in rows:
        if row['intent_id'] == intent['id']:
            raise Refused('intent already sent or pending')
        if row['instrument'] == intent['instrument'] and row['status'] not in TERMINAL:
            raise Refused('non-terminal live order on instrument')
        created = parsed_time(row['created_at'])
        if created > now - timedelta(hours=1):
            hourly += 1
        if day_start <= created < day_start + timedelta(days=1):
            daily += number(row['qty']) * abs(number(row['entry_price']) - number(row['stop']))
    if hourly + 1 > MAX_ORDERS_PER_HOUR:
        raise Refused('four live orders per rolling hour limit')
    if daily + qty * abs(price - stop) > MAX_DAILY_RISK_USDT:
        raise Refused('daily worst-case risk exceeds 8 USDT including candidate')


def update_order(connection, intent_id, status, response, now, secrets=()):
    # cumExecQty is retained in exchange_response; original qty remains the risk
    # reservation, including on partial cancellation, so risk never shrinks.
    connection.execute('UPDATE live_orders SET status=?, updated_at=?, exchange_response=? '
                       'WHERE intent_id=?',
                       (status, now.isoformat(), redact(json.dumps(response), secrets), intent_id))
    connection.commit()


def confirm_order(call, symbol, link, sleep):
    # https://bybit-exchange.github.io/docs/v5/order/open-order
    # https://bybit-exchange.github.io/docs/v5/order/order-list
    def poll():
        latest = {}
        for path in ('/v5/order/realtime', '/v5/order/history'):
            data = call('GET', path, {'category': 'linear', 'symbol': symbol, 'orderLinkId': link})
            for row in data['result']['list']:
                if row.get('orderLinkId') == link:
                    latest = row
                    if row.get('orderStatus') in FINAL_STATUS:
                        return FINAL_STATUS[row['orderStatus']], row
        return None, latest

    latest = {}
    for attempt in range(15):
        status, latest = poll()
        if status:
            return status, latest
        if attempt < 14:
            sleep(2)
    # https://bybit-exchange.github.io/docs/v5/order/cancel-order
    call('POST', '/v5/order/cancel', {'category': 'linear', 'symbol': symbol,
                                    'orderLinkId': link}, allowed=(ORDER_NOT_FOUND,))
    status, latest = poll()
    return status or 'timeout', latest


def protect_position(call, symbol, side, qty, link, sleep):
    # https://bybit-exchange.github.io/docs/v5/position
    last = []
    for attempt in range(3):
        last = call('GET', '/v5/position/list', {'category': 'linear', 'symbol': symbol})['result']['list']
        positions = [p for p in last if p.get('symbol') == symbol and number(p.get('size', 0)) > 0]
        if positions and all(number(p.get('stopLoss') or 0) > 0 and
                             number(p.get('takeProfit') or 0) > 0 for p in positions):
            return None, last
        if attempt < 2:
            sleep(2)
    # Empty responses are not evidence that a confirmed fill is protected.
    # reduceOnly cannot open a new position, even if it has already exited.
    close_qty = sum((number(p['size']) for p in positions), Decimal(0)) or qty
    try:
        response = call('POST', '/v5/order/create', {
            'category': 'linear', 'symbol': symbol, 'side': 'Sell' if side == 'Buy' else 'Buy',
            'orderType': 'Market', 'qty': str(close_qty), 'positionIdx': 0,
            'reduceOnly': True, 'orderLinkId': link + '-close'})
        close_status, close_response = confirm_order(call, symbol, link + '-close', sleep)
        return ('error_naked_closed' if close_status == 'filled' else 'error_naked'), {
            'close_ack': response, 'close_confirmation': close_response}
    except Exception:
        return 'error_naked', {'error': 'protective close failed; check Bybit'}


def send_live(connection, intent_row, *, http=None, sleep=time.sleep, clock=utc_now, lock_path=None):
    """Explicit live seam used only by --live. Also requires the exact process env.

    Never propagates an exception. Entry transport failures retain pending; no
    entry request is retried. Refusals/errors are redacted in the logging journal.
    """
    try:
        with sender_lock(lock_path or REPO_ROOT / '.sender.lock'):
            return _send_locked(connection, intent_row, http=http, sleep=sleep, clock=clock)
    except Exception:
        reason = 'sender lock held or unavailable'
        try:
            LOG.warning('Bybit sender refused: %s', reason)
        except Exception:
            pass
        return {'status': 'refused', 'reason': reason}


def _send_locked(connection, intent_row, *, http, sleep, clock):
    signatures = []
    inserted = False
    acknowledged = False
    intent_id = None
    try:
        if os.environ.get('ANDY_SENDER_LIVE') != 'on':
            raise Refused('dry run: ANDY_SENDER_LIVE must be exactly on')
        key = os.environ.get('BYBIT_API_KEY', '')
        secret = os.environ.get('BYBIT_API_SECRET', '')
        if not key or not secret:
            raise Refused('missing process environment credentials')
        signatures.extend((key, secret))
        base = ('https://api.bybit.com' if os.environ.get('ANDY_SENDER_NET') == 'mainnet'
                else 'https://api-testnet.bybit.com')
        transport = http if http is not None else HTTP

        def call(method, path, params, allowed=()):
            # https://bybit-exchange.github.io/docs/v5/guide (HMAC, headers)
            payload = urlencode(params) if method == 'GET' else json.dumps(params, separators=(',', ':'))
            timestamp = str(int(clock().timestamp() * 1000))
            signature = hmac.new(secret.encode(), (timestamp + key + RECV_WINDOW + payload).encode(),
                                 hashlib.sha256).hexdigest()
            signatures.append(signature)
            headers = {'X-BAPI-API-KEY': key, 'X-BAPI-SIGN': signature,
                       'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': RECV_WINDOW,
                       'Content-Type': 'application/json'}
            data = transport(method, base + path + ('?' + payload if method == 'GET' else ''),
                             headers, None if method == 'GET' else payload.encode())
            code = data.get('retCode')
            if type(code) is not int or code not in (0, *allowed):
                raise Refused(f'Bybit request refused at {path}: retCode {redact(code, signatures)}')
            return data

        intent = dict(intent_row)
        intent_id = int(intent['id'])
        symbol = _BYBIT_SYMBOLS.get(intent['instrument'])
        if not symbol or intent['side'] not in ('long', 'short'):
            raise Refused('unsupported instrument or side; flat cannot be sent')
        if parsed_time(intent['expires_at']) <= clock():
            raise Refused('intent expired')
        # One leverage rule for both paths: an intent must ask for exactly the
        # leverage this sender uses. dry_run_executor accepts the same value.
        if number(intent['leverage']) != LEVERAGE:
            raise Refused(f'intent leverage must be exactly {LEVERAGE}x')
        size, stop, target = positive(intent['size']), positive(intent['stop']), positive(intent['target'])
        initialize_live_orders(connection)
        # https://bybit-exchange.github.io/docs/v5/account/account-info
        account = call('GET', '/v5/account/info', {})
        if account['result'].get('marginMode') != 'ISOLATED_MARGIN':
            raise Refused('Dawn must switch the Bybit account to isolated margin herself')
        # https://bybit-exchange.github.io/docs/v5/market/tickers
        query = {'category': 'linear', 'symbol': symbol}
        ticker = call('GET', '/v5/market/tickers', query)['result']['list']
        price = positive(next(t['lastPrice'] for t in ticker if t['symbol'] == symbol))
        stored = connection.execute('SELECT close, open_time FROM crypto_observations '
            'WHERE instrument=? AND degraded=0 AND close IS NOT NULL ORDER BY open_time DESC LIMIT 1',
            (intent['instrument'],)).fetchone()
        if stored is None or clock() - parsed_time(stored['open_time']) > timedelta(minutes=CRYPTO_MAX_DATA_AGE_MINUTES):
            raise Refused('stored close missing or older than 90 minutes')
        if abs(price / positive(stored['close']) - 1) > Decimal('0.03'):
            raise Refused('ticker differs from stored close by more than 3%')
        if not (stop < price < target if intent['side'] == 'long' else target < price < stop):
            raise Refused('inverted stop/target')
        if abs(price - stop) / price > Decimal('0.05'):
            raise Refused('stop exceeds 5% distance')
        # https://bybit-exchange.github.io/docs/v5/market/instrument
        instruments = call('GET', '/v5/market/instruments-info', query)['result']['list']
        spec = next(i for i in instruments if i['symbol'] == symbol)
        if spec.get('contractType') != 'LinearPerpetual' or spec.get('settleCoin') != 'USDT':
            raise Refused('only USDT linear perpetuals supported')
        lot = spec['lotSizeFilter']
        tick = spec['priceFilter']['tickSize']
        stop, target = rounded(stop, tick), rounded(target, tick)
        positive(stop)
        positive(target)
        if not (stop < price < target if intent['side'] == 'long' else target < price < stop):
            raise Refused('inverted stop/target or rounding crosses entry')
        if abs(price - stop) / price > Decimal('0.05'):
            raise Refused('stop exceeds 5% distance')
        margin = Decimal(MAX_MARGIN_USDT) * min(Decimal(1), max(Decimal('0.1'), size / 500))
        qty = rounded(margin * LEVERAGE / price, lot['qtyStep'])
        notional = qty * price
        margin = notional / LEVERAGE
        if qty <= 0 or qty < positive(lot['minOrderQty']) or notional < number(lot['minNotionalValue']):
            raise Refused(f'{symbol} minimum order exceeds sized allowance / 16 USDT margin cap; never resized')
        if margin > MAX_MARGIN_USDT:
            raise Refused('margin exceeds 16 USDT cap')
        positions = call('GET', '/v5/position/list', query)['result']['list']
        if any(number(p['size']) != 0 for p in positions):
            raise Refused('existing position on symbol')
        # Gate before set-leverage: that call changes exchange state, so a
        # tripped, rate-limited or expired intent must never reach it.
        database_gate(connection, intent, qty, price, stop, clock())
        # https://bybit-exchange.github.io/docs/v5/position/leverage
        call('POST', '/v5/position/set-leverage', {**query, 'buyLeverage': str(LEVERAGE),
             'sellLeverage': str(LEVERAGE)}, allowed=(LEVERAGE_NOT_MODIFIED,))
        link = f'andy-{intent_id}'
        side = 'Buy' if intent['side'] == 'long' else 'Sell'
        # https://bybit-exchange.github.io/docs/v5/order/create-order
        body = {**query, 'side': side, 'orderType': 'Market', 'qty': str(qty),
                'positionIdx': 0, 'orderLinkId': link, 'stopLoss': str(stop),
                'takeProfit': str(target), 'tpslMode': 'Full',
                'slOrderType': 'Market', 'tpOrderType': 'Market'}
        # Initialize/preflight may have committed caller's implicit writes.
        # Finish any remaining implicit transaction before taking our write lock.
        connection.commit()
        connection.execute('BEGIN IMMEDIATE')
        try:
            now = clock()
            database_gate(connection, intent, qty, price, stop, now)
            connection.execute('''INSERT INTO live_orders
                (intent_id, order_link_id, instrument, side, qty, notional, margin,
                 entry_price, stop, target, status, created_at, updated_at, exchange_response)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, '{}')''',
                (intent_id, link, intent['instrument'], intent['side'], float(qty), float(notional),
                 float(margin), float(price), float(stop), float(target), now.isoformat(), now.isoformat()))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        inserted = True
        try:
            response = call('POST', '/v5/order/create', body)
        except Refused as exc:
            update_order(connection, intent_id, 'rejected', {'error': str(exc)}, clock(), signatures)
            LOG.warning('Bybit sender rejected: %s', redact(exc, signatures))
            return {'status': 'rejected', 'reason': redact(exc, signatures)}
        acknowledged = True
        update_order(connection, intent_id, 'sent', response, clock(), signatures)
        status, response = confirm_order(call, symbol, link, sleep)
        # Check partial fills too; their remaining position needs protection.
        if status == 'filled' or number(response.get('cumExecQty') or 0) > 0:
            naked_status, protection = protect_position(call, symbol, side, qty, link, sleep)
            response = {'order': response, 'protection': protection}
            status = naked_status or status
        update_order(connection, intent_id, status, response, clock(), signatures)
        return {'status': status}
    except Exception as exc:
        # Unknown exception messages may contain request reprs or encoded secrets.
        # Only our controlled Refused messages are logged verbatim.
        reason = redact(str(exc) if isinstance(exc, Refused) else 'sender operation failed', signatures)
        status = 'pending' if inserted and not acknowledged else ('error' if acknowledged else 'refused')
        try:
            if inserted:
                update_order(connection, intent_id, status, {'error': reason}, clock(), signatures)
            LOG.warning('Bybit sender %s: %s', status, reason)
        except Exception:
            pass
        return {'status': status, 'reason': reason}


def main(argv=None):
    from andy_trader.store import connect, default_database_path
    parser = argparse.ArgumentParser(description='Inspect or manually clear local live-order blocks. Check Bybit first.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    clear = commands.add_parser('clear')
    clear.add_argument('intent_id', type=int)
    clear.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    connection = connect(Path(default_database_path()))
    try:
        with sender_lock(REPO_ROOT / '.sender.lock'):
            initialize_live_orders(connection)
            rows = connection.execute('SELECT * FROM live_orders ORDER BY id').fetchall()
            if args.command == 'status':
                for row in rows:
                    if row['status'] not in TERMINAL:
                        age = utc_now() - parsed_time(row['created_at'])
                        print(redact(f"{row['intent_id']} {row['instrument']} {row['status']} age={age}"))
            else:
                if not args.reason.strip():
                    parser.error('--reason must not be blank')
                row = next((r for r in rows if r['intent_id'] == args.intent_id), None)
                if row is None or row['status'] in TERMINAL:
                    parser.error('no non-terminal order for that intent')
                now = utc_now()
                update_order(connection, args.intent_id, 'cleared_by_dawn', {
                    'previous': json.loads(row['exchange_response']),
                    'reason': redact(args.reason), 'cleared_at': now.isoformat()}, now)
                print(f'Cleared intent {args.intent_id}; local block only.')
    finally:
        connection.close()


if __name__ == '__main__':
    main()
