import unittest
from unittest.mock import patch

from live_strategy2 import _candidate_from_market, _manage_exits, _update_position_price
from paper_trader import (
    _conservative_mark_price_for_side,
    _entry_price_for_side,
    _extract_bucket_bounds,
    _mark_price_for_side,
)


class StrategyPricingTests(unittest.TestCase):
    def test_extract_bucket_bounds_handles_middle_range(self):
        low, high = _extract_bucket_bounds(
            "Will the lowest temperature in Miami be between 84-85°F on May 26?"
        )
        self.assertEqual((low, high), (84.0, 85.0))

    def test_buy_entry_uses_best_ask(self):
        market = {"bestBid": "0.001", "bestAsk": "0.003"}
        self.assertEqual(_entry_price_for_side(market, "BUY"), 0.003)

    def test_buy_mark_uses_best_bid(self):
        market = {"bestBid": "0.005", "bestAsk": "0.007"}
        self.assertEqual(_mark_price_for_side(market, "BUY"), 0.005)

    def test_conservative_buy_mark_treats_missing_bid_as_zero(self):
        market = {"bestAsk": "0.007"}
        self.assertEqual(_conservative_mark_price_for_side(market, "BUY"), 0.0)

    @patch("live_strategy2.requests.get")
    def test_live_s2_update_position_price_uses_executable_exit_price(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "bestBid": "0.004",
            "bestAsk": "0.006",
            "outcomePrices": "[\"0.99\", \"0.01\"]",
        }
        pos = {"market_id": "123", "side": "BUY"}
        self.assertEqual(_update_position_price(pos), 0.004)

    @patch("live_strategy2.requests.get")
    def test_live_s2_missing_bid_marks_buy_position_at_zero(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "bestAsk": "0.006",
            "outcomePrices": "[\"0.99\", \"0.01\"]",
        }
        pos = {"market_id": "123", "side": "BUY"}
        self.assertEqual(_update_position_price(pos), 0.0)

    def test_live_s2_candidate_requires_executable_exit_bid(self):
        class Trader:
            def _estimate_fair_price(self, *args, **kwargs):
                return 0.01

        market = {
            "question": "Will the highest temperature in Dallas be 63°F or below on May 27?",
            "bestAsk": "0.001",
            "volume": 1000,
            "date": "2026-05-27",
            "city": "Dallas",
        }

        with patch("live_strategy2._get_forecast_temp", return_value=82):
            candidate, reason = _candidate_from_market(Trader(), market)

        self.assertIsNone(candidate)
        self.assertEqual(reason, "missing_best_bid")

    @patch("live_strategy2.requests.get")
    def test_live_s2_resolves_closed_market_even_without_executable_bid(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "closed": True,
            "bestAsk": "0.006",
            "outcomePrices": "[\"1\", \"0\"]",
        }
        state = {
            "positions": {
                "p1": {
                    "status": "dry_run_open",
                    "market_id": "123",
                    "side": "BUY",
                    "entry_price": 0.002,
                    "shares": 500.0,
                    "stake": 1.0,
                }
            },
            "closed_trades": 0,
            "daily_realized_pnl": {},
            "spent": 1.0,
        }

        stats = _manage_exits(state)

        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(state["positions"]["p1"]["status"], "closed")
        self.assertEqual(state["positions"]["p1"]["exit_price"], 1.0)
        self.assertEqual(state["spent"], 0.0)

    @patch("live_strategy2.requests.get")
    def test_live_s2_stop_loss_closes_no_bid_open_position(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "closed": False,
            "bestAsk": "0.006",
            "outcomePrices": "[\"0.99\", \"0.01\"]",
        }
        state = {
            "positions": {
                "p1": {
                    "status": "dry_run_open",
                    "market_id": "123",
                    "side": "BUY",
                    "entry_price": 0.002,
                    "shares": 500.0,
                    "stake": 1.0,
                }
            },
            "closed_trades": 0,
            "daily_realized_pnl": {},
            "spent": 1.0,
        }

        stats = _manage_exits(state)

        self.assertEqual(stats["stop_losses"], 1)
        self.assertEqual(state["positions"]["p1"]["status"], "closed")
        self.assertEqual(state["positions"]["p1"]["exit_price"], 0.0)
        self.assertEqual(state["spent"], 0.0)


if __name__ == "__main__":
    unittest.main()
