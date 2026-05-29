"""Live-gated multi-outcome arbitrage bot using Polymarket Analytics data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_analytics import (
    AnalyticsError,
    PolymarketAnalyticsClient,
    first_float,
    first_str,
    parse_json_list,
)
from clob_pricing import fetch_book, quote_from_book

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "data" / "arb_bot_state.json"
EVENT_LOG = BASE_DIR / "data" / "arb_events.jsonl"

BANKROLL_INITIAL = 100.0
MIN_ARB_PCT = 2.0
MIN_VOLUME = 500.0
MAX_STAKE_PER_ARB = 15.0
MIN_OUTCOMES = 2
MIN_BOOK_DEPTH_USD = 25.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict[str, Any]:
    return {
        "mode": "analytics-arbitrage",
        "schema_version": 2,
        "bankroll": BANKROLL_INITIAL,
        "equity": BANKROLL_INITIAL,
        "positions": [],
        "history": [],
        "opportunities": [],
        "last_error": None,
        "last_scan": None,
        "total_pnl": 0.0,
        "total_trades": 0,
        "win_count": 0,
        "loss_count": 0,
    }


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text())
            if raw.get("schema_version") != 2:
                return default_state()
            state = {**default_state(), **raw}
            return state
        except Exception:
            pass
    return default_state()


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def log_event(event_type: str, **details: Any) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a") as f:
        f.write(json.dumps({"ts": now_iso(), "event_type": event_type, "details": details}, default=str) + "\n")


def _market_group_key(row: dict[str, Any]) -> str:
    return first_str(row, ("neg_risk_market_id", "negRiskMarketID", "event_slug", "eventSlug", "slug", "question"))


def _token_id(row: dict[str, Any]) -> str:
    direct = first_str(row, ("token_id", "clob_token_id", "side_a_token_id", "yes_token_id"))
    if direct:
        return direct
    tokens = parse_json_list(row.get("clobTokenIds") or row.get("clob_token_ids"))
    return str(tokens[0]) if tokens else ""


def _yes_price(row: dict[str, Any]) -> tuple[float | None, float]:
    token = _token_id(row)
    if token:
        quote = quote_from_book(token, fetch_book(token), outcome="Yes")
        if quote.best_ask is not None:
            return quote.best_ask, quote.ask_depth
    direct = first_float(row, ("yes_price", "yesPrice", "side_a_price", "sideAPrice", "price_yes", "best_ask", "bestAsk"))
    if direct and 0 < direct < 1:
        return direct, 0.0
    prices = parse_json_list(row.get("outcomePrices") or row.get("outcome_prices"))
    if prices:
        try:
            price = float(prices[0])
            return (price, 0.0) if 0 < price < 1 else (None, 0.0)
        except Exception:
            return None, 0.0
    return None, 0.0


def scan_arbitrage(limit: int = 500) -> tuple[list[dict[str, Any]], str | None]:
    client = PolymarketAnalyticsClient()
    rows: list[dict[str, Any]] = []
    try:
        for offset in range(0, limit, 100):
            batch = client.retrieve_markets({"closed": "False", "min_volume": str(MIN_VOLUME)}, limit=100, offset=offset)
            rows.extend(batch)
            if len(batch) < 100:
                break
    except AnalyticsError as exc:
        return [], str(exc)

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        neg_risk = row.get("negRisk") or row.get("neg_risk") or row.get("negative_risk")
        # If Analytics does not expose the flag, do not assume true. False
        # positives here are dangerous because buying all YES outcomes is only
        # guaranteed in mutually-exclusive negative-risk groups.
        if str(neg_risk).lower() not in {"true", "1", "yes"}:
            continue
        key = _market_group_key(row)
        if not key:
            continue
        groups.setdefault(key, []).append(row)

    opportunities: list[dict[str, Any]] = []
    for key, markets in groups.items():
        if len(markets) < 2:
            continue
        details = []
        yes_sum = 0.0
        total_volume = 0.0
        total_liquidity = 0.0
        for row in markets:
            price, depth = _yes_price(row)
            if price is None:
                continue
            if depth and depth < MIN_BOOK_DEPTH_USD:
                continue
            volume = first_float(row, ("volume_total", "volumeNum", "volume", "total_volume"), 0.0) or 0.0
            liquidity = first_float(row, ("liquidity", "liquidityNum", "liquidity_num"), 0.0) or 0.0
            yes_sum += price
            total_volume += volume
            total_liquidity += liquidity
            details.append({
                "question": first_str(row, ("question", "title", "market_title")),
                "slug": first_str(row, ("slug", "market_slug")),
                "price": round(price, 6),
                "volume": round(volume, 2),
                "liquidity": round(liquidity, 2),
                "token_id": _token_id(row),
                "condition_id": first_str(row, ("condition_id", "conditionId")),
            })
        if len(details) < 2 or yes_sum <= 0 or yes_sum >= 1:
            continue
        arb_pct = (1.0 - yes_sum) / yes_sum * 100
        if arb_pct < MIN_ARB_PCT or total_volume < MIN_VOLUME:
            continue
        opportunities.append({
            "group_key": key,
            "title": details[0]["question"],
            "outcomes": len(details),
            "yes_sum": round(yes_sum, 6),
            "arb_pct": round(arb_pct, 2),
            "capital_per_unit": round(yes_sum, 6),
            "profit_per_unit": round(1.0 - yes_sum, 6),
            "total_volume": round(total_volume, 2),
            "total_liquidity": round(total_liquidity, 2),
            "outcome_details": sorted(details, key=lambda x: -x["price"]),
            "scanned_at": now_iso(),
        })

    opportunities.sort(key=lambda item: (-item["arb_pct"], -item["total_volume"]))
    return opportunities, None


def _already_open(state: dict[str, Any], group_key: str) -> bool:
    return any(p.get("group_key") == group_key and p.get("status") == "open" for p in state.get("positions", []))


def _enter_best_opportunity(state: dict[str, Any]) -> None:
    for opportunity in state.get("opportunities", []):
        if _already_open(state, opportunity["group_key"]):
            continue
        capital = float(opportunity["capital_per_unit"])
        if capital <= 0:
            continue
        units = min(MAX_STAKE_PER_ARB / capital, state["bankroll"] / capital)
        if units <= 0:
            continue
        cost = round(units * capital, 4)
        expected_payout = round(units, 4)
        expected_profit = round(expected_payout - cost, 4)
        if expected_profit <= 0:
            continue
        state["bankroll"] = round(state["bankroll"] - cost, 4)
        position = {
            **opportunity,
            "status": "open",
            "units": round(units, 4),
            "cost": cost,
            "expected_payout": expected_payout,
            "expected_profit": expected_profit,
            "entry_time": now_iso(),
            "live_order_status": "not_submitted",
        }
        state.setdefault("positions", []).append(position)
        log_event("ARB_ENTER", **position)
        break


def run_once() -> dict[str, Any]:
    state = load_state()
    opportunities, error = scan_arbitrage()
    state["last_scan"] = now_iso()
    state["last_error"] = error
    state["opportunities"] = opportunities
    if not error:
        _enter_best_opportunity(state)
    open_value = sum(float(p.get("expected_payout", 0) or 0) for p in state.get("positions", []) if p.get("status") == "open")
    state["equity"] = round(float(state["bankroll"]) + open_value, 4)
    save_state(state)
    return state
