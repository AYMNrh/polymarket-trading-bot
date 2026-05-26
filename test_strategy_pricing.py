import unittest
from unittest.mock import patch

from live_strategy2 import _update_position_price
from paper_trader import (
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


if __name__ == "__main__":
    unittest.main()
