"""Disconnected, manually opted-in FOK BUY sender. Importing never sends."""
import argparse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import urlencode
from urllib.request import urlopen

from .env import REPO_ROOT
from .store import default_database_path

HOST = 'https://clob.polymarket.com'
GEOBLOCK = 'https://polymarket.com/api/geoblock'
# https://docs.polymarket.com/api-reference/geoblock (2026-10-29).
# Recheck this list against the documentation before going live.
RESTRICTED_PLACES = frozenset('''IR SY CU KP UA-43 UA-14 UA-09 AR AU BY BE BI BR
CA-BC CA-ON CA-AB CA-QC CF CD ET FR DE IQ IT LB LY MM NZ NI PL RU SG SO SK SS SD
TW TH GB US UM VE YE ZW'''.split())
COUNTED = ('pending', 'sent', 'filled', 'unknown')
BUSY_TIMEOUT = 5
SECRETS = set()


class Refused(RuntimeError):
    pass


def redact(message, secrets=()):
    text = str(message)
    for secret in sorted(SECRETS.union(secrets), key=len, reverse=True):
        if len(secret) >= 8:
            text = text.replace(secret, '[REDACTED]')
    return text


def positive(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError
        return number
    except (ValueError, InvalidOperation):
        raise Refused('expected a finite positive number') from None


def utc_now():
    return datetime.now(UTC)


@contextmanager
def database(path):
    connection = None
    try:
        # Use the shared database path, but not store.connect's 30-second timeout.
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT)
        connection.row_factory = sqlite3.Row
        connection.execute(f'PRAGMA busy_timeout={int(BUSY_TIMEOUT * 1000)}')
        connection.execute('''CREATE TABLE IF NOT EXISTS polymarket_sender_orders (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, token_id TEXT,
            price TEXT, shares TEXT, usdc_cost TEXT, reason TEXT, status TEXT NOT NULL,
            order_id TEXT, error TEXT, clear_reason TEXT)''')
        connection.commit()
        yield connection
    except sqlite3.OperationalError as exc:
        if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
            raise Refused('busy') from None
        raise
    finally:
        if connection is not None:
            connection.close()


def insert(connection, intent, cost, status, error='', order_id=None):
    return connection.execute('''INSERT INTO polymarket_sender_orders
        (created_at,token_id,price,shares,usdc_cost,reason,status,order_id,error)
        VALUES (?,?,?,?,?,?,?,?,?)''',
        (utc_now().isoformat(), *(redact(intent.get(k, '')) for k in ('token_id', 'price', 'shares')),
         str(cost), redact(intent.get('reason', '')), status, redact(order_id) if order_id else None,
         redact(error))).lastrowid


def read_limits(path):
    keys = ('max_usdc_per_order', 'max_usdc_per_day', 'max_orders_per_hour')
    try:
        data = json.loads(Path(path).read_text(), parse_float=Decimal, parse_int=Decimal)
    except (OSError, ValueError):
        raise Refused('invalid limits: file') from None
    result = {}
    for key in keys:
        value = data.get(key) if isinstance(data, dict) else None
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise Refused(f'invalid limits: {key}')
        if key == 'max_orders_per_hour' and value != value.to_integral_value():
            raise Refused(f'invalid limits: {key}')
        result[key] = value
    return result


def check_caps(connection, limits, cost):
    daily, hourly = cost, 1
    now = utc_now()
    for row in connection.execute('SELECT * FROM polymarket_sender_orders'):
        if row['status'] not in COUNTED:
            continue
        created = datetime.fromisoformat(row['created_at'])
        if created > now - timedelta(hours=24):
            daily += Decimal(row['usdc_cost'])
        if created > now - timedelta(hours=1):
            hourly += 1
    for key, amount in (('max_usdc_per_order', cost), ('max_usdc_per_day', daily),
                        ('max_orders_per_hour', hourly)):
        if amount > limits[key]:
            raise Refused(f'cap exceeded: {key}')


def read_json(url):
    with urlopen(url, timeout=15) as response:
        return json.load(response, parse_float=Decimal)


def check_region(http):
    try:
        data = http(GEOBLOCK)
        if not isinstance(data, dict):
            raise ValueError
        data = {k: v for k, v in data.items() if k != 'ip'}
        country, region = data.get('country'), data.get('region')
        if data.get('blocked') is not False or not isinstance(country, str) or not country.strip():
            raise ValueError
        if not isinstance(region, str):
            raise ValueError
        country, region = country.upper(), region.upper()
        if country in RESTRICTED_PLACES or f'{country}-{region}' in RESTRICTED_PLACES:
            raise ValueError
    except Exception:
        # Never retain the response or exception: either can contain the caller's IP.
        raise Refused('region blocked or region check failed') from None


def read_book(token_id, http):
    try:
        book = http(HOST + '/book?' + urlencode({'token_id': token_id}))
        asks = [positive(level['price']) for level in book['asks']]
        if not asks or any(price >= 1 for price in asks):
            raise ValueError
        return book, min(asks)
    except Exception:
        raise Refused('no book: missing or malformed asks') from None


def check_book(intent, cost, book, best, tick=None):
    price = intent['price']
    if not Decimal('.02') <= price <= Decimal('.98') or price > best + Decimal('.03'):
        raise Refused('price outside band')
    try:
        tick = positive(tick if tick is not None else book['tick_size'])
        minimum = positive(book['min_order_size'])
    except (KeyError, Refused):
        raise Refused('no book: missing tick size or minimum') from None
    if price % tick:
        raise Refused('price off tick size')
    if cost <= 0 or cost < minimum * price:
        raise Refused('amount below minimum market buy')


def credentials():
    key = os.environ.get('POLYMARKET_PRIVATE_KEY', '')
    if key:
        SECRETS.update((key, key[2:] if key.startswith('0x') else '0x' + key))
    if os.environ.get('ANDY_SENDER_LIVE') != 'on':
        raise Refused('ANDY_SENDER_LIVE must be exactly on')
    funder = os.environ.get('POLYMARKET_FUNDER_ADDRESS', '')
    signature = os.environ.get('POLYMARKET_SIGNATURE_TYPE', '')
    if not key or not funder or not signature:
        raise Refused('missing process environment credentials')
    if signature not in ('0', '1', '2'):
        raise Refused('invalid POLYMARKET_SIGNATURE_TYPE')
    return dict(key=key, funder=funder, signature_type=int(signature))


def make_client(**kwargs):
    from py_clob_client.client import ClobClient
    return ClobClient(HOST, chain_id=137, **kwargs)


def authenticated_client(factory):
    client = factory(**credentials())
    creds = client.derive_api_key()
    if creds is None:
        raise Refused('API credential derivation failed')
    for name in ('api_key', 'api_secret', 'api_passphrase'):
        value = getattr(creds, name, None)
        if isinstance(value, str):
            SECRETS.add(value)
    client.set_api_creds(creds)
    return client


def sign_order(client, intent, cost, tick, neg_risk, fee):
    # Official py-clob-client README and clob_types.py: BUY amount is USDC.
    # https://github.com/Polymarket/py-clob-client#place-a-market-order-buy-by--amount
    # This requested legacy SDK is archived; validate compatibility before live use.
    from py_clob_client.clob_types import MarketOrderArgs, PartialCreateOrderOptions, OrderType
    from py_clob_client.order_builder.constants import BUY
    args = MarketOrderArgs(token_id=intent['token_id'], amount=float(cost), side=BUY,
                           price=float(intent['price']), fee_rate_bps=fee, order_type=OrderType.FOK)
    return client.create_market_order(args, PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk))


def response_status(response):
    if isinstance(response, dict):
        if response.get('success') is True and isinstance(response.get('status'), str):
            return ('filled' if response['status'] == 'matched' else 'sent'), ''
        if response.get('success') is False and isinstance(response.get('errorMsg'), str) and response['errorMsg']:
            return 'rejected', redact(response['errorMsg'])
    return 'unknown', 'malformed post response'


def exception_status(exc):
    # PolyApiException.status_code may be None; error_msg holds the response body.
    from py_clob_client.exceptions import PolyApiException
    if isinstance(exc, PolyApiException):
        code = exc.status_code
        if type(code) is int and 400 <= code < 500 and code != 408:
            return 'rejected', redact(exc.error_msg)
    return 'unknown', redact(exc)


def save_answer(path, row_id, response=None, failure=None):
    with database(path) as connection:
        # Persist the exchange ID before interpreting any other response fields.
        order_id = response.get('orderID') if isinstance(response, dict) else None
        if isinstance(order_id, str) and order_id:
            connection.execute("UPDATE polymarket_sender_orders SET order_id=? WHERE id=? AND status IN ('pending','unknown')",
                               (redact(order_id), row_id))
            connection.commit()
        else:
            order_id = None
        status, error = failure if failure else response_status(response)
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM polymarket_sender_orders WHERE id=?', (row_id,)).fetchone()
        if row['status'] in ('pending', 'unknown'):
            connection.execute('UPDATE polymarket_sender_orders SET status=?,error=? WHERE id=? AND status=?',
                               (status, redact(error), row_id, row['status']))
        elif row['status'] == 'cleared_by_operator' and status in ('sent', 'filled'):
            intent = dict(row)
            intent['reason'] = f'late answer for cleared row {row_id}'
            insert(connection, intent, row['usdc_cost'], status, error, order_id)
        elif row['status'] == 'cleared_by_operator':
            print(redact(f'late answer for cleared row {row_id}: {status} {error}'))
        connection.commit()


def post_once(path, row_id, client, signed, timeout):
    done = threading.Event()

    def worker():
        try:
            try:
                response = client.post_order(signed, 'FOK')
            except Exception as exc:
                save_answer(path, row_id, failure=exception_status(exc))
            else:
                save_answer(path, row_id, response)
        except Exception as exc:
            print(redact(f'answer could not be saved: {exc}'))
        finally:
            done.set()

    # The SDK has no per-call timeout. A daemon may finish after our wait expires.
    threading.Thread(target=worker, daemon=True).start()
    if not done.wait(min(timeout, 30)):
        with database(path) as connection:
            connection.execute("UPDATE polymarket_sender_orders SET status='unknown',error='post timeout' WHERE id=? AND status='pending'", (row_id,))
            connection.commit()
    with database(path) as connection:
        return dict(connection.execute('SELECT * FROM polymarket_sender_orders WHERE id=?', (row_id,)).fetchone())


def send_buy(intent, *, live=False, database_path=None, limits_path=None,
             http=None, client_factory=None, signer=None, post_timeout=30):
    path = database_path or default_database_path()
    limits_path = Path(limits_path or REPO_ROOT / 'polymarket_sender_limits.json')
    http = http or read_json
    intent, cost = dict(intent), Decimal(0)
    error = None
    try:
        if live:
            credentials()
            limits = read_limits(limits_path)
            if limits_path.with_name('polymarket_sender.STOP').exists():
                raise Refused('stop file exists')
            check_region(http)
        if not isinstance(intent.get('token_id'), str) or not intent['token_id'].strip():
            raise Refused('missing token id')
        if not isinstance(intent.get('reason'), str) or not intent['reason'].strip():
            raise Refused('missing reason')
        intent['price'], intent['shares'] = positive(intent['price']), positive(intent['shares'])
        cost = (intent['price'] * intent['shares']).quantize(Decimal('.01'), rounding=ROUND_DOWN)
        book, best = read_book(intent['token_id'], http)
        check_book(intent, cost, book, best)
        if live:
            client = authenticated_client(client_factory or make_client)
            tick = client.get_tick_size(intent['token_id'])
            neg_risk = client.get_neg_risk(intent['token_id'])
            fee = client.get_fee_rate_bps(intent['token_id'])
            check_book(intent, cost, book, best, tick)
            signed = (signer or sign_order)(client, intent, cost, tick, neg_risk, fee)
    except Exception as exc:
        error = redact(exc)
    with database(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        if live and error is None:
            try:
                check_caps(connection, limits, cost)
            except Refused as exc:
                error = str(exc)
        status = 'refused' if error else ('pending' if live else 'dry_run')
        row_id = insert(connection, intent, cost, status, error or '')
        connection.commit()
    if error:
        print(redact(f'refused: {error}'))
        raise Refused(error)
    if not live:
        print(redact(f'dry_run BUY FOK {intent} amount_usdc={cost}; region not checked'))
        return {'id': row_id, 'status': status, 'usdc_cost': str(cost)}
    return post_once(path, row_id, client, signed, post_timeout)


def clear_stuck(row_id, reason, confirm_not_filled=False, *, database_path=None):
    if not isinstance(reason, str) or not reason.strip() or not confirm_not_filled:
        raise Refused('non-blank reason and --confirm-not-filled required')
    with database(database_path or default_database_path()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM polymarket_sender_orders WHERE id=?', (row_id,)).fetchone()
        if row is None or row['status'] not in ('pending', 'unknown', 'sent'):
            raise Refused('row is not stuck')
        if row['status'] == 'pending' and utc_now() - datetime.fromisoformat(row['created_at']) <= timedelta(minutes=15):
            raise Refused('pending row must be more than 15 minutes old')
        connection.execute("UPDATE polymarket_sender_orders SET status='cleared_by_operator',clear_reason=? WHERE id=? AND status=?",
                           (redact(reason), row_id, row['status']))
        connection.commit()


def settlement(order_id, order, trades):
    if not isinstance(trades, list) or not all(isinstance(t, dict) for t in trades):
        return None
    if order is not None and not isinstance(order, dict):
        return None
    if order and (not isinstance(order.get('id'), str) or not isinstance(order.get('status'), str)):
        return None
    for trade in trades:
        if not isinstance(trade.get('status'), str):
            return None
        makers = trade.get('maker_orders', [])
        if not isinstance(makers, list) or not all(isinstance(m, dict) for m in makers):
            return None
    for trade in trades:
        if trade.get('status', '').upper() in ('MATCHED', 'MINED', 'CONFIRMED', 'RETRYING'):
            if trade.get('taker_order_id') == order_id:
                return 'filled'
            for maker in trade.get('maker_orders', []):
                if maker.get('order_id') == order_id and positive(maker['matched_amount']) > 0:
                    return 'filled'
    if not order or order.get('id') != order_id:
        return None
    status = order.get('status', '').upper()
    matched = Decimal(str(order.get('size_matched', 'NaN')))
    if status in ('MATCHED', 'FILLED') or (matched.is_finite() and matched > 0):
        return 'filled'
    if status in ('CANCELED', 'CANCELLED', 'KILLED') and matched == 0:
        return 'cancelled'
    return None


def reconcile(*, database_path=None, client_factory=None):
    path = database_path or default_database_path()
    client = authenticated_client(client_factory or make_client)
    with database(path) as connection:
        rows = connection.execute("SELECT * FROM polymarket_sender_orders WHERE status IN ('sent','unknown') AND order_id IS NOT NULL").fetchall()
    for row in rows:
        failed = False
        try:
            order = client.get_order(row['order_id'])
        except Exception:
            failed, order = True, None
        try:
            trades = client.get_trades()
        except Exception:
            failed, trades = True, None
        if failed:
            continue
        try:
            status = settlement(row['order_id'], order, trades)
        except Exception:
            continue
        if status:
            with database(path) as connection:
                connection.execute('UPDATE polymarket_sender_orders SET status=? WHERE id=? AND status=?',
                                   (status, row['id'], row['status']))
                connection.commit()


def show_status(*, database_path=None, limits_path=None):
    with database(database_path or default_database_path()) as connection:
        rows = connection.execute('SELECT * FROM polymarket_sender_orders ORDER BY id').fetchall()
    counts = {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
        if row['status'] in ('pending', 'unknown', 'sent'):
            age = utc_now() - datetime.fromisoformat(row['created_at'])
            print(redact(f"{row['id']} {row['status']} age={age} {row['order_id'] or 'no order id'}"))
    print(redact(f'ledger: {counts}'))
    try:
        print(redact(f"limits: {read_limits(limits_path or REPO_ROOT / 'polymarket_sender_limits.json')}"))
    except Refused as exc:
        print(redact(f'limits: invalid ({str(exc).split(": ")[-1]})'))


def demo_intent(http):
    markets = http(HOST + '/sampling-markets')['data']
    for market in markets:
        if market.get('active') is True and market.get('closed') is False and market.get('accepting_orders') is True:
            token = market['tokens'][0]['token_id']
            book, best = read_book(token, http)
            return dict(token_id=token, price=best, shares=positive(book['min_order_size']) + 1,
                        reason='public open market demo')
    raise Refused('no open demo market')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Disconnected manual Polymarket FOK BUY sender')
    actions = parser.add_mutually_exclusive_group()
    for action in ('status', 'dry-run-demo', 'live', 'reconcile'):
        actions.add_argument('--' + action, action='store_true')
    actions.add_argument('--clear-stuck', type=int)
    parser.add_argument('--token-id')
    parser.add_argument('--price', type=decimal_argument)
    parser.add_argument('--shares', type=decimal_argument)
    parser.add_argument('--reason')
    parser.add_argument('--confirm-not-filled', action='store_true')
    parser.add_argument('--limits-path', type=Path)
    parser.add_argument('--database-path', type=Path)
    args = parser.parse_args(argv)
    db = dict(database_path=args.database_path)
    try:
        if args.live:
            if any(value is None for value in (args.token_id, args.price, args.shares, args.reason)):
                parser.error('--live requires --token-id --price --shares --reason')
            result = send_buy(dict(token_id=args.token_id, price=args.price, shares=args.shares, reason=args.reason),
                              live=True, limits_path=args.limits_path, **db)
            print(redact(result))
        elif args.dry_run_demo:
            send_buy(demo_intent(read_json), limits_path=args.limits_path, **db)
        elif args.clear_stuck is not None:
            clear_stuck(args.clear_stuck, args.reason, args.confirm_not_filled, **db)
        elif args.reconcile:
            reconcile(**db)
        elif args.status:
            show_status(limits_path=args.limits_path, **db)
        else:
            parser.print_help()
    except Exception as exc:
        print(redact(f'refused: {exc}'))
        return 1
    return 0


def decimal_argument(value):
    try:
        return positive(value)
    except Refused as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
