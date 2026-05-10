"""
Authenticated Polymarket CLOB client using Builder API key + HMAC.
Works for builder-scoped endpoints (/builder/trades, /builder/orders).
"""
import base64
import hashlib
import hmac
import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


class BuilderClobClient:
    """Polymarket CLOB API auth with Builder HMAC signing."""

    def __init__(self, api_key: str, secret: str, passphrase: str,
                 builder_code: str = "",
                 endpoint: str = "https://clob.polymarket.com"):
        self.api_key = api_key
        self.passphrase = passphrase
        self.builder_code = builder_code
        self.endpoint = endpoint.rstrip("/")
        
        # Decode secret (URL-safe base64 + padding fix)
        s = secret.replace('-', '+').replace('_', '/')
        pad = 4 - (len(s) % 4)
        if pad != 4:
            s += '=' * pad
        self.secret_bytes = base64.b64decode(s)

    def _sign(self, ts: int, method: str, path: str, body: str = "") -> str:
        """HMAC-SHA256 signature as URL-safe base64."""
        msg = str(ts) + method.upper() + path
        if body:
            msg += body
        sig = hmac.new(self.secret_bytes, msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(sig).decode().replace("+", "-").replace("/", "_")

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = int(time.time() * 1000)
        return {
            "POLY_BUILDER_API_KEY": self.api_key,
            "POLY_BUILDER_SIGNATURE": self._sign(ts, method, path, body),
            "POLY_BUILDER_TIMESTAMP": str(ts),
            "POLY_BUILDER_PASSPHRASE": self.passphrase,
        }

    def _get(self, path: str, params: dict = None) -> dict | list | None:
        url = f"{self.endpoint}{path}"
        try:
            if params:
                query = "&".join(f"{k}={v}" for k, v in params.items())
                url += "?" + query
            headers = self._headers("GET", path)
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 400:
                logger.warning("Builder API 400: %s", r.text[:150])
                return None
            else:
                logger.warning("Builder API %d: %s", r.status_code, r.text[:100])
                return None
        except Exception as e:
            logger.error("Builder API request failed: %s", e)
            return None

    def get_builder_trades(self, limit: int = 100, after: str = "",
                           before: str = "") -> list[dict]:
        """Get trades attributed to this builder code."""
        params = {"limit": limit, "builder_code": self.builder_code}
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        path = "/builder/trades"
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        result = self._get(f"{path}?{qs}")
        if result and isinstance(result, dict):
            return result.get("data", [])
        return []

    def get_weather_markets(self) -> list[dict]:
        """Get weather markets (uses public endpoint, no auth needed)."""
        try:
            r = requests.get(f"{self.endpoint}/markets", params={"limit": 500}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                markets = data.get("data", []) if isinstance(data, dict) else data
                weather = []
                for m in markets:
                    tags = [t.get("label", "").lower() for t in m.get("tags", [])]
                    title = m.get("title", "").lower()
                    if any(t in tags for t in ["weather", "climate", "temperature"]) or "weather" in title or "nyc" in title:
                        weather.append(m)
                return weather
        except Exception as e:
            logger.error("Failed to fetch markets: %s", e)
        return []
