"""One bounded paper cycle using unauthenticated public market data only."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time

import requests

from .core import (Rejected, fee_rate, levels, liquidation_value, number,
                   plan_basket, settlement_value, timestamp, tokens)
from .store import Store


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = Path(__file__).resolve().parent / 'runtime'
CITIES = ('chicago', 'nyc', 'miami', 'dallas', 'seattle', 'atlanta', 'boston',
          'denver', 'phoenix', 'houston', 'los-angeles', 'san-francisco')
GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
DATA = 'https://data-api.polymarket.com'


class DataError(RuntimeError):
    pass


class PublicAPI:
    def get(self, url, params=None, missing_ok=False):
        try:
            response = requests.get(url, params=params, timeout=(4, 12))
            if missing_ok and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except requests.exceptions.SSLError as exc:
            raise DataError('TLS verification failed; check VPN/DNS connectivity. No prices accepted.') from exc
        except (requests.RequestException, ValueError) as exc:
            raise DataError(f'{type(exc).__name__}: public data request failed at {url.split("?")[0]}') from exc

    def market(self, market_id):
        return self.get(f'{GAMMA}/markets/{market_id}')

    def books(self, market):
        ids = tokens(market)
        def fetch(token):
            return token, self.get(f'{CLOB}/book', {'token_id': token})
        with ThreadPoolExecutor(max_workers=2) as pool:
            return dict(pool.map(fetch, ids.values()))

    def discover(self):
        today = datetime.now(timezone.utc).date()
        slugs = [f'highest-temperature-in-{city}-on-{day.strftime("%B").lower()}-{day.day}-{day.year}'
                 for offset in (-1, 0, 1, 2) for day in [today + timedelta(days=offset)] for city in CITIES]
        def fetch(slug):
            try:
                return self.get(f'{GAMMA}/events/slug/{slug}', missing_ok=True), None
            except DataError as exc:
                return None, str(exc)
        markets, errors, events = {}, [], 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            for event, error in pool.map(fetch, slugs):
                if error:
                    errors.append(error)
                if event:
                    events += 1
                    for market in event.get('markets', []):
                        if isinstance(market, dict) and market.get('id'):
                            markets[str(market['id'])] = market
        return list(markets.values()), errors, events


@contextmanager
def cycle_lock(data_dir):
    path = Path(data_dir) / 'cycle.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def load_watchlist(root=ROOT):
    wallets = {}
    for path in (root / 'whale_watch.json', root / 'data' / 'watch_wallets.json'):
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        rows = data if isinstance(data, list) else data.get('watched_wallets', data.get('wallets', []))
        for row in rows:
            address = row.get('address', '') if isinstance(row, dict) else str(row)
            if re.fullmatch(r'0x[0-9a-fA-F]{40}', address):
                wallets[address.lower()] = row.get('label', address[:10]) if isinstance(row, dict) else address[:10]
    return wallets


def monitor_whales(store, api, root=ROOT):
    previous = store.metadata('whales') or {}
    pending = store.metadata('whale_backfill') or {}
    if time.time() - previous.get('attempted_at', 0) < (60 if pending else 300):
        return
    wallets = load_watchlist(root)
    report = {'attempted_at': time.time(), 'watched_wallets': len(wallets), 'new_trades': 0,
              'successful_wallets': 0, 'errors': [], 'bootstrap_limit_per_wallet': 100}
    cursors = store.metadata('whale_cursors') or {}
    def fetch(item):
        address, label = item
        collected, complete = [], False
        checkpoint = pending.get(address, {'offset': 0, 'end': int(report['attempted_at']),
                                           'start': int(cursors[address]) - 60 if address in cursors else None})
        cutoff = checkpoint['start']
        offset_start = checkpoint['offset']
        # Initial scan is explicitly a bounded recent-history seed. Later scans
        # overlap the watermark and deduplicate, with four pages before flagging a gap.
        for offset in range(offset_start, offset_start + (400 if cutoff is not None else 100), 100):
            params = {'user': address, 'type': 'TRADE', 'limit': 100, 'offset': offset,
                      'sortBy': 'TIMESTAMP', 'sortDirection': 'DESC', 'end': checkpoint['end']}
            if cutoff is not None:
                params['start'] = cutoff
            try:
                rows = api.get(f'{DATA}/activity', params)
                if not isinstance(rows, list):
                    raise DataError('invalid_activity_payload')
                for row in rows:
                    if row.get('type') == 'TRADE' and row.get('transactionHash') and row.get('timestamp'):
                        collected.append({**row, 'watched_wallet': address, 'wallet_label': label})
                checkpoint = {**checkpoint, 'offset': offset + 100}
                if len(rows) < 100 or cutoff is None:
                    complete = True
                    break
            except DataError as exc:
                return address, collected, False, str(exc), checkpoint
        return address, collected, complete, None if complete else 'activity_backfill_pending', checkpoint
    with ThreadPoolExecutor(max_workers=6) as pool:
        for address, rows, complete, error, checkpoint in pool.map(fetch, wallets.items()):
            for row in rows:
                report['new_trades'] += store.save_activity(address, row)
            if complete:
                report['successful_wallets'] += 1
                cursors[address] = checkpoint['end']
                pending.pop(address, None)
            else:
                pending[address] = checkpoint
            if error:
                report['errors'].append({'wallet': address, 'error': error})
    report['healthy'] = bool(wallets) and not report['errors']
    report['last_success'] = time.time() if report['healthy'] else previous.get('last_success')
    store.metadata('whale_cursors', cursors)
    store.metadata('whale_backfill', pending)
    store.metadata('whales', report)


def run_cycle(store, api=None, root=ROOT):
    api = api or PublicAPI()
    started = time.time()
    report = {'discovered': 0, 'books_checked': 0, 'opened': 0, 'settled': 0, 'book_samples': [],
              'rejections': {}, 'errors': [], 'candidates': [], 'source': 'public_gamma_and_clob'}
    rejects = Counter()
    with cycle_lock(store.path.parent):
        snapshot = store.snapshot()
        if not snapshot['accounting_ok']:
            raise RuntimeError('Paper ledger invariant failed; stop and inspect the database.')
        # Existing positions are serviced before any discovery or entry work.
        for basket in snapshot['baskets']:
            if basket['status'] != 'open':
                continue
            plan = basket['plan']
            try:
                market = api.market(plan['market_id'])
                try:
                    payout = settlement_value(plan, market)
                except Rejected as exc:
                    if str(exc) != 'awaiting_official_resolution':
                        raise
                else:
                    report['settled'] += store.settle(plan['condition_id'], payout)
                    continue
                try:
                    value = liquidation_value(plan, api.books(market), time.time(), store.limits)
                except Rejected:
                    value = None
                store.mark(plan['condition_id'], value)
            except (DataError, Rejected) as exc:
                store.mark(plan['condition_id'], None)
                report['errors'].append(str(exc))
        try:
            markets, errors, event_count = api.discover()
            report['discovered'], report['events'] = len(markets), event_count
            report['errors'].extend(errors)
            if not markets:
                report['errors'].append('no_weather_markets_discovered')
            if markets and not errors:
                store.start_experiment(started)
            # Rotate the bounded scan so slower/later cities are not starved.
            markets.sort(key=lambda m: str(m.get('id', '')))
            offset = int(store.metadata('market_offset') or 0) % max(1, len(markets))
            markets = markets[offset:] + markets[:offset]
            traded = {b['condition_id'] for b in snapshot['baskets']}
            deadline = store.snapshot()['entry_deadline']
            for market in markets:
                if market.get('conditionId') in traded:
                    rejects['already_traded_condition'] += 1
                    continue
                if deadline is not None and time.time() >= deadline:
                    rejects['experiment_complete'] += 1
                    continue
                if report['books_checked'] >= 80:
                    rejects['scan_book_limit'] += 1
                    continue
                try:
                    # Cheap metadata checks avoid hundreds of unnecessary CLOB requests.
                    if market.get('closed') is not False or market.get('acceptingOrders') is not True:
                        raise Rejected('market_not_tradable')
                    if timestamp(market.get('endDate')) <= time.time():
                        raise Rejected('outside_market_horizon')
                    report['books_checked'] += 1
                    books = api.books(market)
                    if len(report['book_samples']) < 10:
                        asks = [levels(b, 'asks') for b in books.values()]
                        if len(asks) == 2 and all(asks):
                            report['book_samples'].append({'market': market.get('question'),
                                'ask_sum_before_costs': float(sum(a[0][0] for a in asks)),
                                'minimum_order': market.get('orderMinSize'),
                                'fee_rate': str(fee_rate(market))})
                    plan = plan_basket(market, books, time.time(), store.limits)
                    # Refetch BOTH books and metadata before recording a simulated fill.
                    fresh_market = api.market(plan['market_id'])
                    plan = plan_basket(fresh_market, api.books(fresh_market), time.time(), store.limits)
                    store.open_basket(plan)
                    report['opened'] += 1
                    report['candidates'].append(plan)
                    traded.add(plan['condition_id'])
                except Rejected as exc:
                    rejects[str(exc)] += 1
                except DataError as exc:
                    report['errors'].append(str(exc))
            store.metadata('market_offset', (offset + 80) % max(1, len(markets)))
        except (DataError, ValueError, TypeError) as exc:
            report['errors'].append(str(exc))
        try:
            monitor_whales(store, api, root)
        except (ValueError, TypeError, DataError) as exc:
            store.metadata('whales', {'healthy': False, 'attempted_at': time.time(), 'errors': [str(exc)]})
        report['rejections'] = dict(rejects)
        report['errors'] = list(dict.fromkeys(report['errors']))[:20]
        report['duration_seconds'] = round(time.time() - started, 2)
        store.record_scan(started, not report['errors'], report)
    return store.snapshot()
