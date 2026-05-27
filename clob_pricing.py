"""Authoritative Polymarket CLOB pricing helpers.

Gamma is still useful for discovery and metadata, but strategy decisions must
use executable CLOB books. The CLOB book arrays are not guaranteed to be sorted
best-first, so every best price is computed across the whole side.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").rstrip("/")


@dataclass
class BookQuote:
    token_id: str
    outcome: str
    best_bid: float | None
    best_ask: float | None
    bid_size: float
    ask_size: float
    spread: float | None
    bid_depth: float
    ask_depth: float
    source: str = "clob_book"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _float_or_none(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price >= 1:
        return None
    return price


def _level_price(level: dict[str, Any]) -> float | None:
    return _float_or_none(level.get("price"))


def _level_size(level: dict[str, Any]) -> float:
    try:
        return max(0.0, float(level.get("size", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _book_side_stats(levels: list[dict[str, Any]], side: str) -> tuple[float | None, float, float]:
    prices = [p for p in (_level_price(level) for level in levels) if p is not None]
    if not prices:
        return None, 0.0, 0.0
    best = max(prices) if side == "bid" else min(prices)
    best_size = sum(_level_size(level) for level in levels if _level_price(level) == best)
    depth = sum(_level_size(level) * float(_level_price(level) or 0) for level in levels)
    return best, best_size, depth


def fetch_book(token_id: str, session: requests.Session | None = None) -> dict[str, Any] | None:
    client = session or requests.Session()
    try:
        r = client.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning("CLOB book fetch failed for %s: %s", str(token_id)[:12], exc)
        return None


def quote_from_book(token_id: str, book: dict[str, Any] | None, outcome: str = "Yes") -> BookQuote:
    if not book:
        return BookQuote(
            token_id=str(token_id),
            outcome=outcome,
            best_bid=None,
            best_ask=None,
            bid_size=0.0,
            ask_size=0.0,
            spread=None,
            bid_depth=0.0,
            ask_depth=0.0,
            error="missing_book",
        )

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid, bid_size, bid_depth = _book_side_stats(bids, "bid")
    best_ask, ask_size, ask_depth = _book_side_stats(asks, "ask")
    spread = round(best_ask - best_bid, 6) if best_bid is not None and best_ask is not None else None
    return BookQuote(
        token_id=str(token_id),
        outcome=outcome,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=round(bid_size, 4),
        ask_size=round(ask_size, 4),
        spread=spread,
        bid_depth=round(bid_depth, 4),
        ask_depth=round(ask_depth, 4),
    )


def yes_token_id(market: dict[str, Any]) -> str | None:
    """Return the YES token id from a Gamma market object."""
    tokens = [str(t) for t in _parse_json_list(market.get("clobTokenIds"))]
    if not tokens:
        return None

    outcomes = [str(o).strip().lower() for o in _parse_json_list(market.get("outcomes"))]
    for idx, outcome in enumerate(outcomes):
        if outcome == "yes" and idx < len(tokens):
            return tokens[idx]

    # Polymarket binary weather markets generally store YES first. If the event
    # payload omits outcomes, the first token is still the YES token in practice.
    return tokens[0]


def quote_yes_market(market: dict[str, Any], session: requests.Session | None = None) -> BookQuote:
    token_id = yes_token_id(market)
    if not token_id:
        return BookQuote(
            token_id="",
            outcome="Yes",
            best_bid=None,
            best_ask=None,
            bid_size=0.0,
            ask_size=0.0,
            spread=None,
            bid_depth=0.0,
            ask_depth=0.0,
            error="missing_yes_token",
        )
    return quote_from_book(token_id, fetch_book(token_id, session=session), outcome="Yes")


def executable_entry_price(quote: BookQuote) -> float | None:
    return quote.best_ask


def executable_exit_price(quote: BookQuote) -> float:
    return quote.best_bid if quote.best_bid is not None else 0.0
