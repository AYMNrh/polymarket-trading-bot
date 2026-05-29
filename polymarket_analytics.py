"""Polymarket Analytics/Falcon API client.

This is the single market-data source for the simplified system. Trading may
still use Polymarket CLOB for order submission, but signals, prices, wallet
tracking, volume, and market metadata come through this client.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"

FALCON_API_BASE = os.getenv("POLYMARKET_ANALYTICS_BASE", "https://narrative.agent.heisenberg.so").rstrip("/")
MARKETS_AGENT_ID = 574
TRADES_AGENT_ID = 556


class AnalyticsError(RuntimeError):
    pass


def _load_env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value.strip().strip("\"'")
    if not ENV_FILE.exists():
        return None
    found = None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if key.strip() == name:
            found = raw.strip().strip("\"'")
    return found


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("results", "items", "markets", "trades"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    for key in ("results", "items", "markets", "trades"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


class PolymarketAnalyticsClient:
    def __init__(self, api_key: str | None = None, base_url: str = FALCON_API_BASE):
        self.api_key = api_key or _load_env_value("POLYMARKET_ANALYTICS_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise AnalyticsError("POLYMARKET_ANALYTICS_API_KEY is missing")
        url = f"{self.base_url}{path}"
        try:
            response = self.session.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(5, 20),
            )
        except Exception as exc:
            self.last_error = f"request failed: {exc}"
            raise AnalyticsError(self.last_error) from exc

        try:
            data = response.json()
        except Exception as exc:
            self.last_error = f"non-json response from {path}: HTTP {response.status_code}"
            raise AnalyticsError(self.last_error) from exc

        if response.status_code >= 400 or data.get("status") == "error" or data.get("error") is True:
            message = data.get("msg") or data.get("message") or data.get("error", {}).get("message") or str(data)[:220]
            self.last_error = f"{path}: {message}"
            raise AnalyticsError(self.last_error)

        self.last_error = None
        return data

    def retrieve_markets(self, params: dict[str, Any] | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        payload = {
            "agent_id": MARKETS_AGENT_ID,
            "params": params or {},
            "pagination": {"limit": max(10, limit), "offset": offset},
            "formatter_config": {"format_type": "raw"},
        }
        data = self.post("/api/v2/semantic/retrieve/parameterized", payload)
        return _as_list(data)

    def retrieve_recent_trades(self, wallet: str | None = None, market_slug: str | None = None,
                               limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if wallet:
            params["proxy_wallet"] = wallet
        if market_slug:
            params["market_slug"] = market_slug
        data = self.post(
            "/api/v2/semantic/retrieve/parameterized",
            {
                "agent_id": TRADES_AGENT_ID,
                "params": params,
                "pagination": {"limit": max(10, limit), "offset": offset},
                "formatter_config": {"format_type": "raw"},
            },
        )
        return _as_list(data)


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def first_float(row: dict[str, Any], keys: tuple[str, ...], default: float | None = None) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def first_str(row: dict[str, Any], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return str(value)
    return default
