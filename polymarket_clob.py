"""
Polymarket API client — fetches market data, prices, and order book info.
Uses Gamma API (gamma-api.polymarket.com) for market discovery and pricing.
CLOB API (clob.polymarket.com) used only for /markets listing (non-book endpoints).

The CLOB /book endpoint is deprecated/returns 404 as of May 2026.
Gamma API provides bestBid/bestAsk via its /markets endpoint.
"""
import json
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketClobClient:
    def __init__(self, endpoint: str = "https://clob.polymarket.com"):
        self.endpoint = endpoint.rstrip("/")
        self._session = requests.Session()

    def _get(self, path: str, params: dict = None, base_url: str = None) -> dict | list | None:
        url = f"{base_url or self.endpoint}{path}"
        try:
            r = self._session.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("API error [%s]: %s", url, e)
            return None

    # ============ GAMMA API (working) ============

    def get_markets(self, limit: int = 100, next_cursor: str = "") -> dict | None:
        """List available markets via CLOB API."""
        params = {"limit": limit}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return self._get("/markets", params, base_url=CLOB_BASE)

    def get_gamma_markets(self, tag: str = "", limit: int = 50, offset: int = 0,
                          closed: bool = False) -> list[dict]:
        """Get markets from Gamma API with optional tag filter."""
        params = {
            "limit": min(limit, 100),
            "offset": offset,
            "closed": str(closed).lower(),
        }
        if tag:
            params["tag"] = tag
        data = self._get("/markets", params, base_url=GAMMA_BASE)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return []

    def get_weather_markets(self) -> list[dict]:
        """
        Get markets tagged with 'weather' from Gamma API.
        Returns enriched market data including bestBid/bestAsk/spread/lastTradePrice.
        """
        markets = []
        offset = 0
        max_batches = 5  # Up to 500 markets

        for batch in range(max_batches):
            batch_markets = self.get_gamma_markets(tag="weather", limit=100, offset=offset)
            if not batch_markets:
                break
            markets.extend(batch_markets)
            if len(batch_markets) < 100:
                break
            offset += 100

        logger.info("Gamma: found %d weather-tagged markets", len(markets))
        return markets

    def get_market_detail(self, market_id: str) -> dict | None:
        """Get detail for a single market by Gamma ID."""
        return self._get(f"/markets/{market_id}", base_url=GAMMA_BASE)

    def get_market_prices(self, condition_id: str) -> dict | None:
        """Get prices via Gamma API using condition ID."""
        params = {"condition_id": condition_id, "limit": 1}
        data = self._get("/markets", params, base_url=GAMMA_BASE)
        if data and isinstance(data, list) and len(data) > 0:
            m = data[0]
            return {
                "best_bid": m.get("bestBid"),
                "best_ask": m.get("bestAsk"),
                "last_trade_price": m.get("lastTradePrice"),
                "spread": m.get("spread"),
                "outcome_prices": m.get("outcomePrices"),
                "volume": m.get("volumeNum"),
            }
        return None

    # ============ ORDER BOOK (via Gamma) ============

    def get_order_book(self, token_id: str) -> dict | None:
        """
        Get order book data for a token.
        CLOB /book endpoint is dead (returns 404).
        We get bestBid/bestAsk from Gamma API instead.

        Returns a synthetic book with the top level only:
        {
          "bids": [{"price": str(best_bid), "size": "1"}],
          "asks": [{"price": str(best_ask), "size": "1"}]
        }
        For real multi-level book, this is a limitation.
        """
        logger.warning("CLOB /book endpoint is deprecated. Using Gamma bestBid/bestAsk instead.")
        return None

    def get_best_bid_ask(self, condition_id: str) -> tuple | None:
        """Get best bid/ask from Gamma API by condition ID."""
        prices = self.get_market_prices(condition_id)
        if prices and prices["best_bid"] is not None and prices["best_ask"] is not None:
            return (float(prices["best_bid"]), float(prices["best_ask"]))
        return None

    def get_midpoint(self, token_id: str, condition_id: str = None) -> float | None:
        """
        Calculate midpoint price. Uses Gamma API (condition_id-based).
        Falls back to CLOB's /midpoints if available.
        """
        if condition_id:
            ba = self.get_best_bid_ask(condition_id)
            if ba:
                return (ba[0] + ba[1]) / 2

        # Fallback: try CLOB midpoint endpoint (might work)
        data = self._get("/midpoints", {"token_id": token_id})
        if data and isinstance(data, dict):
            mp = data.get("midpoint") or data.get("price")
            if mp:
                return float(mp)
        return None
