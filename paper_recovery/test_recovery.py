"""Deterministic synthetic-book regression tests; never touch runtime data."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from .core import D, Limits, Rejected, liquidation_value, plan_basket, settlement_value
from .runner import cycle_lock, load_watchlist, monitor_whales, run_cycle
from unittest.mock import patch
from .store import Store


def fixtures(now=None):
    now = time.time() if now is None else now
    market = {'conditionId': '0x' + 'a' * 64, 'id': '123', 'question': 'Synthetic test only',
              'slug': 'synthetic-test', 'outcomes': '["Yes", "No"]', 'clobTokenIds': '["111", "222"]',
              'active': True, 'closed': False, 'acceptingOrders': True, 'enableOrderBook': True,
              'endDate': datetime.fromtimestamp(now + 86400, timezone.utc).isoformat(),
              'orderMinSize': .1, 'feesEnabled': True,
              'feeSchedule': {'rate': .05, 'exponent': 1, 'takerOnly': True}}
    books = {tid: {'asset_id': tid, 'market': market['conditionId'], 'timestamp': str(int(now * 1000)),
                   'min_order_size': '1', 'asks': [{'price': '.4', 'size': '100'}],
                   'bids': [{'price': '.38', 'size': '100'}]} for tid in ('111', '222')}
    return market, books


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.market, self.books = fixtures(self.now)

    def plan(self, limits=Limits()):
        return plan_basket(self.market, self.books, self.now, limits)

    def test_cost_includes_fees_and_buffer(self):
        p = self.plan()
        self.assertLessEqual(D(p['cost']), D('10'))
        self.assertEqual(D(p['units']), D(p['payout_if_complete']))
        self.assertGreater(D(p['expected_profit']), 0)
        self.assertGreater(sum(D(x['fee']) for x in p['legs']), 0)
        self.assertEqual(len(p['legs']), 2)

    def test_depth_order_does_not_affect_fill(self):
        p = self.plan()
        for book in self.books.values():
            book['asks'].insert(0, {'price': '.99', 'size': '1000'})
        self.assertEqual(p['cost'], self.plan()['cost'])
        self.assertEqual(p['units'], self.plan()['units'])

    def test_smaller_profitable_depth_is_not_hidden_by_worse_levels(self):
        for book in self.books.values():
            book['asks'] = [{'price': '.4', 'size': '2'}, {'price': '.9', 'size': '100'}]
        p = self.plan()
        self.assertLessEqual(D(p['units']), 2)
        self.assertGreater(D(p['expected_profit']), 0)

    def test_fees_can_remove_apparent_arbitrage(self):
        for b in self.books.values():
            b['asks'][0]['price'] = '.495'
        with self.assertRaisesRegex(Rejected, 'no_edge_after_costs'):
            self.plan()

    def test_exact_minimum_pair_still_needs_fee_budget(self):
        self.market['orderMinSize'] = 5
        with self.assertRaisesRegex(Rejected, 'minimum_leg_notional'):
            self.plan()

    def test_no_book_default_price(self):
        del self.books['111']
        with self.assertRaisesRegex(Rejected, 'missing_book'):
            self.plan()

    def test_stale_and_skewed_books_rejected(self):
        self.books['111']['timestamp'] = str(int((self.now - 120) * 1000))
        with self.assertRaisesRegex(Rejected, 'stale_book'):
            self.plan()
        self.books['111']['timestamp'] = str(int((self.now - 10) * 1000))
        with self.assertRaisesRegex(Rejected, 'book_timestamp_skew'):
            self.plan()

    def test_identity_mismatch_and_duplicate_outcomes(self):
        self.books['111']['market'] = 'other'
        with self.assertRaisesRegex(Rejected, 'book_identity_mismatch'):
            self.plan()
        self.market['outcomes'] = '["Yes", "Yes"]'
        with self.assertRaisesRegex(Rejected, 'not_binary_yes_no'):
            self.plan()

    def test_missing_or_nan_fee_data_fails_closed(self):
        del self.market['feeSchedule']
        with self.assertRaisesRegex(Rejected, 'fee_schedule_unknown'):
            self.plan()
        self.market['feeSchedule'] = {'rate': 'NaN', 'exponent': 1}
        with self.assertRaisesRegex(Rejected, 'nonfinite_number'):
            self.plan()

    def test_wide_spread_mark_is_actual_bid_not_midpoint(self):
        p = self.plan()
        for b in self.books.values():
            b['bids'][0]['price'] = '.01'
        mark = liquidation_value(p, self.books, self.now)
        self.assertLess(mark, D(p['cost']) / 10)

    def test_closed_without_confirmed_resolution_does_not_settle(self):
        p = self.plan()
        self.market.update(closed=True, outcomePrices='["1", "0"]')
        with self.assertRaisesRegex(Rejected, 'awaiting_official_resolution'):
            settlement_value(p, self.market)
        self.market['umaResolutionStatus'] = 'resolved'
        self.assertEqual(settlement_value(p, self.market), D(p['units']))
        self.market['outcomePrices'] = '[".5", ".5"]'
        self.assertEqual(settlement_value(p, self.market), D(p['units']))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.store = Store(self.path)
        self.now = time.time()
        self.store.start_experiment(self.now)
        self.market, self.books = fixtures(self.now)
        self.plan = plan_basket(self.market, self.books, self.now)

    def test_restart_does_not_refill_cash(self):
        self.store.open_basket(self.plan, self.now)
        cash = self.store.snapshot()['cash']
        restarted = Store(self.path)
        self.assertEqual(restarted.snapshot()['cash'], cash)
        self.assertLess(cash, 100)
        self.assertTrue(restarted.snapshot()['accounting_ok'])

    def test_open_and_settle_are_idempotent(self):
        self.store.open_basket(self.plan, self.now)
        with self.assertRaisesRegex(Rejected, 'already_traded_condition'):
            self.store.open_basket(self.plan, self.now)
        self.assertTrue(self.store.settle(self.plan['condition_id'], self.plan['units']))
        self.assertFalse(self.store.settle(self.plan['condition_id'], self.plan['units']))
        s = self.store.snapshot()
        self.assertAlmostEqual(s['cash'], 100 + float(self.plan['expected_profit']), places=6)
        self.assertTrue(s['accounting_ok'])
        with self.assertRaisesRegex(Rejected, 'already_traded_condition'):
            self.store.open_basket(self.plan, self.now)

    def test_drawn_capital_and_missing_mark_not_fake_profit(self):
        self.store.open_basket(self.plan, self.now)
        self.assertIsNone(self.store.snapshot()['equity'])
        self.store.mark(self.plan['condition_id'], 1, self.now - 120)
        self.assertIsNone(self.store.snapshot()['equity'])
        self.assertEqual(self.store.snapshot()['realized_pnl'], 0)

    def test_experiment_expiry_stops_entries_but_not_settlement(self):
        self.store.open_basket(self.plan, self.now)
        later = self.now + 4 * 86400
        other = {**self.plan, 'condition_id': '0x' + 'b' * 64, 'quoted_at': later}
        with self.assertRaisesRegex(Rejected, 'experiment_complete'):
            self.store.open_basket(other, later)
        self.assertTrue(self.store.settle(self.plan['condition_id'], self.plan['units'], later))

    def test_exposure_limit_is_transactional(self):
        for c in ('a', 'b'):
            self.store.open_basket({**self.plan, 'condition_id': '0x' + c * 64}, self.now)
        with self.assertRaisesRegex(Rejected, 'exposure_limit'):
            self.store.open_basket({**self.plan, 'condition_id': '0x' + 'c' * 64}, self.now)
        self.assertTrue(self.store.snapshot()['accounting_ok'])

    def test_overlapping_runner_cannot_get_lock(self):
        with cycle_lock(self.path.parent):
            with self.assertRaises((BlockingIOError, OSError)):
                with cycle_lock(self.path.parent):
                    pass

    def test_wallet_activity_deduplicates_without_losing_other_outcome(self):
        row = {'timestamp': self.now, 'transactionHash': 'tx', 'asset': '111', 'price': .1, 'size': 5, 'side': 'BUY'}
        self.assertEqual(self.store.save_activity('wallet', row), 1)
        self.assertEqual(self.store.save_activity('wallet', row), 0)
        self.assertEqual(self.store.save_activity('wallet', {**row, 'asset': '222'}), 1)
        self.assertEqual(len(self.store.snapshot()['activity']), 2)

    def test_runner_services_position_and_reports_no_entry(self):
        market, books = self.market, self.books
        class FakeAPI:
            def market(self, market_id): return deepcopy(market)
            def books(self, m): return deepcopy(books)
            def discover(self): return [deepcopy(market)], [], 1
        first = run_cycle(self.store, FakeAPI(), Path(self.temp.name))
        self.assertEqual(first['report']['opened'], 1)
        market.update(closed=True, umaResolutionStatus='resolved', outcomePrices='["1", "0"]')
        second = run_cycle(self.store, FakeAPI(), Path(self.temp.name))
        self.assertEqual(second['report']['settled'], 1)
        self.assertEqual(second['report']['opened'], 0)
        self.assertGreater(second['realized_pnl'], 0)
        self.assertTrue(second['accounting_ok'])

    def test_watchlists_merge_case_insensitively(self):
        root = Path(self.temp.name)
        (root / 'data').mkdir()
        address = '0x' + 'a' * 40
        (root / 'whale_watch.json').write_text(json.dumps({'watched_wallets': [{'address': address, 'label': 'whale'}]}))
        (root / 'data' / 'watch_wallets.json').write_text(json.dumps([address]))
        self.assertEqual(len(load_watchlist(root)), 1)

    def test_whale_backfill_resumes_frozen_window_instead_of_repeating_pages(self):
        address = '0x' + 'a' * 40
        self.store.metadata('whale_cursors', {address: self.now - 3600})
        seen = []
        class API:
            def get(self, url, params):
                seen.append(dict(params))
                if params['offset'] >= 400:
                    return []
                return [{'type': 'TRADE', 'transactionHash': str(i), 'timestamp': int(self.now),
                         'asset': '111', 'size': 1, 'price': .5} for i in range(params['offset'], params['offset'] + 100)]
        api = API()
        api.now = self.now
        with patch('paper_recovery.runner.load_watchlist', return_value={address: 'whale'}):
            monitor_whales(self.store, api)
            pending = self.store.metadata('whale_backfill')[address]
            self.assertEqual(pending['offset'], 400)
            self.assertFalse(self.store.metadata('whales')['healthy'])
            old = self.store.metadata('whales')
            self.store.metadata('whales', {**old, 'attempted_at': self.now - 61})
            monitor_whales(self.store, api)
        self.assertEqual(seen[-1]['offset'], 400)
        self.assertEqual(seen[-1]['end'], seen[0]['end'])
        self.assertEqual(self.store.metadata('whale_backfill'), {})
        self.assertTrue(self.store.metadata('whales')['healthy'])


if __name__ == '__main__':
    unittest.main()
