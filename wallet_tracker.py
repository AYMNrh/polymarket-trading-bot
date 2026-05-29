"""Wallet tracking for low-entry copy candidates through Polymarket Analytics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_analytics import AnalyticsError, PolymarketAnalyticsClient, first_float, first_str

BASE_DIR = Path(__file__).parent
WATCHLIST_FILE = BASE_DIR / "data" / "watch_wallets.json"
STATE_FILE = BASE_DIR / "data" / "wallet_tracker_state.json"

MAX_ENTRY_PRICE = 0.15
MAX_TRADE_SIZE = 100.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_watchlist() -> list[str]:
    if WATCHLIST_FILE.exists():
        try:
            data = json.loads(WATCHLIST_FILE.read_text())
            if isinstance(data, list):
                return [str(x) for x in data]
            if isinstance(data, dict):
                return [str(x) for x in data.get("wallets", [])]
        except Exception:
            pass
    return []


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"wallets": [], "signals": [], "last_scan": None, "last_error": None}


def scan_wallets() -> dict[str, Any]:
    client = PolymarketAnalyticsClient()
    wallets = load_watchlist()
    signals: list[dict[str, Any]] = []
    errors: list[str] = []
    for wallet in wallets:
        try:
            trades = client.retrieve_recent_trades(wallet=wallet, limit=50)
        except AnalyticsError as exc:
            errors.append(f"{wallet}: {exc}")
            continue
        for trade in trades:
            price = first_float(trade, ("price", "avg_price", "execution_price"), 999.0) or 999.0
            size = first_float(trade, ("size", "amount", "usdc_size", "value"), 0.0) or 0.0
            side = first_str(trade, ("side", "action")).upper()
            if price > MAX_ENTRY_PRICE or size > MAX_TRADE_SIZE or side not in {"BUY", "LONG"}:
                continue
            signals.append({
                "wallet": wallet,
                "price": round(price, 4),
                "size": round(size, 2),
                "side": side,
                "market": first_str(trade, ("market_slug", "slug", "market")),
                "outcome": first_str(trade, ("outcome", "side_a_outcome")),
                "timestamp": first_str(trade, ("timestamp", "created_at", "time")),
                "raw": trade,
            })
    signals.sort(key=lambda row: (row.get("timestamp") or ""), reverse=True)
    state = {
        "wallets": wallets,
        "signals": signals[:100],
        "last_scan": now_iso(),
        "last_error": "; ".join(errors) if errors else None,
    }
    save_state(state)
    return state
