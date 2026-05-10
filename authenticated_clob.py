"""
Authenticated Polymarket CLOB API client.
Uses HMAC-SHA256 signatures for authentication.

Auth flow:
1. Base64-decode the secret
2. HMAC-SHA256(timestamp + method + path + body, decoded_secret)
3. Send as headers: POLY_API_KEY, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_PASSPHRASE
"""
import base64
import hashlib
import hmac
import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


class AuthenticatedClobClient:
    """Polymarket CLOB API with HMAC auth for whale trade history."""

    def __init__(self, api_key: str, secret: str, passphrase: str,
                 endpoint: str = "https://clob.polymarket.com"):
        self.api_key = api_key
        self.secret = base64.urlsafe_b64decode(secret + "==")  # Decode URL-safe base64
        self.passphrase = passphrase
        self.endpoint = endpoint.rstrip("/")
        self._session = requests.Session()

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Generate HMAC-SHA256 signature."""
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            self.secret,
            message.encode(),
            hashlib.sha256
        ).digest()
        return base64.b64encode(signature).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        """Generate authenticated headers for a request."""
        timestamp = str(int(time.time() * 1000))
        signature = self._sign(timestamp, method, path, body)
        return {
            "POLY_API_KEY": self.api_key,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, params: dict = None,
                data: dict = None) -> dict | list | None:
        """Make an authenticated request to the CLOB API."""
        url = f"{self.endpoint}{path}"
        body = json.dumps(data) if data else ""
        headers = self._headers(method, path, body)

        try:
            r = self._session.request(
                method, url, params=params, data=body,
                headers=headers, timeout=30,
            )
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 401:
                logger.error("CLOB auth failed: %s", r.text[:100])
                return None
            else:
                logger.warning("CLOB %s %s: %d %s", method, path,
                               r.status_code, r.text[:100])
                return None
        except Exception as e:
            logger.error("CLOB request failed %s %s: %s", method, path, e)
            return None

    def get_orders(self, maker: str, limit: int = 50) -> list[dict]:
        """Get orders for a specific wallet."""
        result = self.request(
            "GET", "/data/orders",
            params={"maker_address": maker, "limit": limit},
        )
        return result if isinstance(result, list) else []

    def get_trades(self, maker: str, limit: int = 50) -> list[dict]:
        """Get trades for a specific wallet."""
        result = self.request(
            "GET", "/data/trades",
            params={"maker_address": maker, "limit": limit},
        )
        return result if isinstance(result, list) else []

    def get_market(self, condition_id: str) -> dict | None:
        """Get market details."""
        return self.request("GET", f"/markets/{condition_id}")

    def analyze_whale(self, wallet_address: str, label: str = "") -> dict:
        """Fetch and analyze a whale's trades from CLOB."""
        orders = self.get_orders(wallet_address, limit=100)
        if not orders:
            return {"wallet": label, "error": "no orders found"}

        from collections import defaultdict
        city_counts = defaultdict(int)
        temp_counts = defaultdict(int)
        outcome_counts = defaultdict(int)
        total_volume = 0.0
        trades_parsed = []

        for o in orders:
            market = o.get("market", {})
            question = market.get("question", market.get("title", "?"))
            outcome = o.get("outcome", "?")
            price = float(o.get("price", 0))
            size = float(o.get("tokenSize", 0))
            value = price * size
            filled = o.get("filled", False)
            timestamp = o.get("creationTimestamp", "")

            # Extract city and temp 
            city = self._extract_city(question)
            temp = self._extract_temperature(question)

            trades_parsed.append({
                "question": question[:80],
                "outcome": outcome,
                "price": round(price, 4),
                "size": round(size, 4),
                "value": round(value, 2),
                "city": city or "?",
                "temperature": temp,
                "filled": filled,
            })
            
            if city:
                city_counts[city] += 1
            if temp:
                temp_counts[temp] += 1
            outcome_counts[outcome] += 1
            if filled:
                total_volume += value

        return {
            "wallet": label or wallet_address[:10],
            "total_orders": len(orders),
            "filled_orders": sum(1 for o in orders if o.get("filled")),
            "total_volume": round(total_volume, 2),
            "top_cities": sorted(city_counts.items(), key=lambda x: -x[1])[:10],
            "top_temperatures": sorted(temp_counts.items(), key=lambda x: -x[1])[:10],
            "top_outcomes": sorted(outcome_counts.items(), key=lambda x: -x[1])[:5],
            "recent_trades": trades_parsed[:10],
        }

    @staticmethod
    def _extract_city(text: str) -> str | None:
        if not text: return None
        t = text.lower()
        for c in ["new york","nyc","chicago","los angeles","miami","houston",
                   "phoenix","denver","seattle","boston","dallas","san francisco",
                   "washington","philadelphia","atlanta","london","tokyo"]:
            if c in t: return c.title()
        return None

    @staticmethod
    def _extract_temperature(text: str) -> int | None:
        if not text: return None
        import re
        m = re.search(r'(\d+)\s*(?:°|deg|degree|F)', text)
        return int(m.group(1)) if m else None
