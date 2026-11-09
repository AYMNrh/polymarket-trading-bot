"""Transactional paper ledger. Cash amounts are integer micro-dollars."""
from contextlib import contextmanager
from decimal import ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .core import Limits, Rejected, number


def micros(value):
    return int((number(value) * 1000000).to_integral_value(rounding=ROUND_HALF_UP))


class Store:
    def __init__(self, path, limits=Limits()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = limits
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS account(
                    id INTEGER PRIMARY KEY CHECK(id=1), cash INTEGER NOT NULL CHECK(cash>=0),
                    initial INTEGER NOT NULL, started REAL, limits_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS baskets(
                    condition_id TEXT PRIMARY KEY, plan TEXT NOT NULL, cost INTEGER NOT NULL,
                    opened REAL NOT NULL, closed REAL, proceeds INTEGER, mark INTEGER,
                    marked REAL, reason TEXT, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scans(
                    id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL NOT NULL,
                    success INTEGER NOT NULL, report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS activity(
                    id TEXT PRIMARY KEY, wallet TEXT NOT NULL, timestamp REAL NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO account VALUES(1,?,?,NULL,?)',
                       (micros(limits.bankroll), micros(limits.bankroll), json.dumps(limits.__dict__)))
            configured = json.loads(db.execute('SELECT limits_json FROM account').fetchone()[0])
            if configured != limits.__dict__:
                raise ValueError('Existing experiment has different limits; use a separate data directory.')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def open_basket(self, plan, now=None):
        now = time.time() if now is None else now
        cost = micros(plan['cost'])
        if cost <= 0 or cost > micros(self.limits.basket_cap):
            raise Rejected('basket_budget')
        if now - plan['quoted_at'] > self.limits.book_max_age or plan['quoted_at'] > now + 5:
            raise Rejected('stale_plan')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            account = db.execute('SELECT * FROM account').fetchone()
            started = account['started']
            if started is None:
                raise Rejected('experiment_not_started')
            if now >= started + self.limits.duration_days * 86400:
                raise Rejected('experiment_complete')
            if db.execute('SELECT 1 FROM baskets WHERE condition_id=?', (plan['condition_id'],)).fetchone():
                raise Rejected('already_traded_condition')
            count, exposure = db.execute("SELECT count(*),coalesce(sum(cost),0) FROM baskets WHERE status='open'").fetchone()
            if count >= self.limits.max_open or exposure + cost > micros(self.limits.exposure_cap):
                raise Rejected('exposure_limit')
            if account['cash'] < cost:
                raise Rejected('insufficient_cash')
            db.execute('UPDATE account SET cash=cash-? WHERE id=1', (cost,))
            db.execute("INSERT INTO baskets(condition_id,plan,cost,opened,status) VALUES(?,?,?,?,'open')",
                       (plan['condition_id'], json.dumps(plan), cost, now))

    def settle(self, condition, proceeds, now=None):
        amount = micros(proceeds)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM baskets WHERE condition_id=? AND status='open'", (condition,)).fetchone()
            if row is None:
                return False
            if amount != micros(json.loads(row['plan'])['units']):
                raise Rejected('incomplete_set_payout')
            db.execute("UPDATE baskets SET status='closed',closed=?,proceeds=?,reason='official_resolution' WHERE condition_id=?",
                       (time.time() if now is None else now, amount, condition))
            db.execute('UPDATE account SET cash=cash+? WHERE id=1', (amount,))
            return True

    def mark(self, condition, value, now=None):
        with self.connection() as db:
            db.execute("UPDATE baskets SET mark=?,marked=? WHERE condition_id=? AND status='open'",
                       (None if value is None else micros(value), time.time() if now is None else now, condition))

    def start_experiment(self, now):
        with self.connection() as db:
            db.execute('UPDATE account SET started=coalesce(started,?) WHERE id=1', (now,))

    def record_scan(self, started, success, report):
        with self.connection() as db:
            db.execute('INSERT INTO scans(started,finished,success,report) VALUES(?,?,?,?)',
                       (started, time.time(), bool(success), json.dumps(report)))

    def metadata(self, key, value=None):
        with self.connection() as db:
            if value is not None:
                db.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (key, json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_activity(self, wallet, row):
        identity = [wallet.lower()] + [row.get(k) for k in
                    ('transactionHash', 'asset', 'conditionId', 'side', 'timestamp', 'size', 'price')]
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.connection() as db:
            cursor = db.execute('INSERT OR IGNORE INTO activity VALUES(?,?,?,?)',
                                (digest, wallet.lower(), float(row['timestamp']), json.dumps(row)))
            return cursor.rowcount

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            account = dict(db.execute('SELECT * FROM account').fetchone())
            baskets = [dict(r) for r in db.execute('SELECT * FROM baskets ORDER BY opened DESC')]
            scan = db.execute('SELECT * FROM scans ORDER BY id DESC LIMIT 1').fetchone()
            success = db.execute('SELECT max(finished) FROM scans WHERE success=1').fetchone()[0]
            activity = [json.loads(r[0]) for r in db.execute('SELECT payload FROM activity ORDER BY timestamp DESC LIMIT 100')]
            reviews = [json.loads(r[0]) for r in db.execute('SELECT report FROM scans ORDER BY id DESC LIMIT 50')]
        realized = sum(r['proceeds'] - r['cost'] for r in baskets if r['status'] == 'closed')
        open_cost = sum(r['cost'] for r in baskets if r['status'] == 'open')
        invariant = account['cash'] + open_cost == account['initial'] + realized
        for b in baskets:
            b['plan'] = json.loads(b['plan'])
            b['mark_fresh'] = b['marked'] is not None and now - b['marked'] <= self.limits.book_max_age
            for k in ('cost', 'proceeds', 'mark'):
                b[k] = None if b[k] is None else b[k] / 1000000
        open_baskets = [b for b in baskets if b['status'] == 'open']
        all_priced = all(b['mark'] is not None and b['mark_fresh'] for b in open_baskets)
        equity = account['cash'] / 1000000 + sum(b['mark'] for b in open_baskets) if all_priced else None
        report = json.loads(scan['report']) if scan else {}
        trading_healthy = bool(scan and scan['success'] and now - scan['finished'] < 180 and invariant)
        whales = self.metadata('whales') or {}
        whales_healthy = bool(whales.get('healthy') and now - (whales.get('last_success') or 0) < 600)
        healthy = trading_healthy and whales_healthy
        return {
            'mode': 'PAPER ONLY', 'strategy': 'binary_complete_set',
            'cash': account['cash'] / 1000000, 'starting_cash': account['initial'] / 1000000,
            'open_cost': open_cost / 1000000, 'equity': equity, 'realized_pnl': realized / 1000000,
            'accounting_ok': invariant, 'healthy': healthy, 'trading_healthy': trading_healthy,
            'status': 'healthy' if healthy else ('degraded_whale_monitoring' if trading_healthy else
                      ('blocked' if scan and not scan['success'] else 'stale_or_not_started')),
            'experiment_started': account['started'],
            'entry_deadline': account['started'] + self.limits.duration_days * 86400 if account['started'] else None,
            'parameters': json.loads(account['limits_json']), 'baskets': baskets, 'activity': activity,
            'last_scan': scan['finished'] if scan else None, 'last_success': success,
            'report': report, 'whales': {**whales, 'fresh_and_healthy': whales_healthy},
            'review': {'scans_sampled': len(reviews), 'closed': sum(b['status'] == 'closed' for b in baskets),
                       'parameter_changes_enabled': False, 'realized_pnl': realized / 1000000},
        }
