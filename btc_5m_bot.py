"""BTC 5-minute Up/Down straddle bot using Polymarket Analytics data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_analytics import AnalyticsError, PolymarketAnalyticsClient, first_float, first_str, parse_json_list
from wallet_execution import PolymarketExecutionAdapter
from clob_pricing import fetch_book, quote_from_book

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "data" / "btc_5m_state.json"
EVENT_LOG = BASE_DIR / "data" / "btc_5m_events.jsonl"

BANKROLL_INITIAL = 100.0
STAKE_PER_SIDE = 50.0
STOP_LOSS_PCT = 0.30
MIN_VOLUME = 5000.0
MAX_COMBINED_ENTRY = 1.05
MIN_BOOK_DEPTH_USD = 50.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict[str, Any]:
    return {
        "mode": "btc-5m-straddle",
        "schema_version": 2,
        "bankroll": BANKROLL_INITIAL,
        "equity": BANKROLL_INITIAL,
        "current_cycle": None,
        "past_cycles": [],
        "last_scan": None,
        "last_error": None,
        "total_pnl": 0.0,
        "total_cycles": 0,
        "win_count": 0,
        "loss_count": 0,
        "live_enabled": False,
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


def _prices(row: dict[str, Any]) -> tuple[float | None, float | None]:
    up = first_float(row, ("up_price", "side_a_price", "yes_price", "best_ask", "bestAsk"))
    down = first_float(row, ("down_price", "side_b_price", "no_price"))
    if up is None or down is None:
        prices = parse_json_list(row.get("outcomePrices") or row.get("outcome_prices"))
        if len(prices) >= 2:
            try:
                up = float(prices[0])
                down = float(prices[1])
            except Exception:
                pass
    if up is not None and down is None:
        down = 1.0 - up
    return up, down


def _book_prices(up_token: str, down_token: str) -> tuple[dict[str, Any] | None, str | None]:
    up_quote = quote_from_book(up_token, fetch_book(up_token), outcome="Up")
    down_quote = quote_from_book(down_token, fetch_book(down_token), outcome="Down")
    if up_quote.best_ask is None or down_quote.best_ask is None:
        return None, "missing executable ask on one side"
    if up_quote.best_bid is None or down_quote.best_bid is None:
        return None, "missing executable bid on one side"
    depth = min(up_quote.ask_depth, down_quote.ask_depth)
    if depth < MIN_BOOK_DEPTH_USD:
        return None, f"book depth too low: ${depth:.2f}"
    return {
        "up_price": up_quote.best_ask,
        "down_price": down_quote.best_ask,
        "up_bid": up_quote.best_bid,
        "down_bid": down_quote.best_bid,
        "up_quote": up_quote.to_dict(),
        "down_quote": down_quote.to_dict(),
        "depth": depth,
    }, None


def _tokens(row: dict[str, Any]) -> tuple[str, str]:
    direct_up = first_str(row, ("up_token_id", "side_a_token_id", "yes_token_id"))
    direct_down = first_str(row, ("down_token_id", "side_b_token_id", "no_token_id"))
    if direct_up and direct_down:
        return direct_up, direct_down
    tokens = parse_json_list(row.get("clobTokenIds") or row.get("clob_token_ids"))
    if len(tokens) >= 2:
        return str(tokens[0]), str(tokens[1])
    return direct_up, direct_down


def find_current_market() -> tuple[dict[str, Any] | None, str | None]:
    client = PolymarketAnalyticsClient()
    try:
        rows = client.retrieve_markets(
            {
                "market_slug": "btc-updown-5m",
                "min_volume": "0",
                "closed": "False",
            },
            limit=50,
            offset=0,
        )
    except AnalyticsError as exc:
        return None, str(exc)

    candidates = []
    for row in rows:
        slug = first_str(row, ("slug", "market_slug"))
        question = first_str(row, ("question", "title"))
        text = f"{slug} {question}".lower()
        if "bitcoin" not in text:
            continue
        if "up" not in text or "down" not in text:
            continue
        up_token, down_token = _tokens(row)
        if not up_token or not down_token:
            continue
        book, book_error = _book_prices(up_token, down_token)
        if book_error:
            continue
        volume = first_float(row, ("volume_total", "volume", "volumeNum"), 0.0) or 0.0
        candidates.append({
            "slug": slug,
            "question": question,
            "condition_id": first_str(row, ("condition_id", "conditionId")),
            "end_date": first_str(row, ("end_date", "endDate", "endDateIso")),
            "volume": volume,
            "up_price": book["up_price"],
            "down_price": book["down_price"],
            "up_bid": book["up_bid"],
            "down_bid": book["down_bid"],
            "up_quote": book["up_quote"],
            "down_quote": book["down_quote"],
            "book_depth": book["depth"],
            "up_token_id": up_token,
            "down_token_id": down_token,
            "raw": row,
        })
    if not candidates:
        return None, "no active BTC 5m market from analytics"
    candidates.sort(key=lambda row: row.get("end_date") or "")
    return candidates[0], None


def _winning_outcome(slug: str) -> str | None:
    client = PolymarketAnalyticsClient()
    try:
        rows = client.retrieve_markets({"market_slug": slug, "closed": "True"}, limit=10, offset=0)
    except AnalyticsError:
        return None
    for row in rows:
        if first_str(row, ("slug", "event_slug")) == slug:
            outcome = first_str(row, ("winning_outcome", "winner", "resolved_outcome"))
            return outcome.lower() if outcome else None
    return None


def _close_cycle(state: dict[str, Any], reason: str) -> None:
    cycle = state.get("current_cycle")
    if not cycle:
        return
    cycle["status"] = "closed"
    cycle["closed_at"] = now_iso()
    cycle["close_reason"] = reason
    if reason in {"market_rotated", "resolved"}:
        winner = _winning_outcome(cycle.get("slug", ""))
        if winner:
            if "up" in winner and not cycle.get("up_sold"):
                cycle["cash_returned"] = round(float(cycle.get("cash_returned", 0) or 0) + float(cycle.get("up_shares", 0) or 0), 2)
                cycle["pnl"] = round(float(cycle.get("pnl", 0) or 0) + float(cycle.get("up_shares", 0) or 0) - STAKE_PER_SIDE, 2)
            if "down" in winner and not cycle.get("down_sold"):
                cycle["cash_returned"] = round(float(cycle.get("cash_returned", 0) or 0) + float(cycle.get("down_shares", 0) or 0), 2)
                cycle["pnl"] = round(float(cycle.get("pnl", 0) or 0) + float(cycle.get("down_shares", 0) or 0) - STAKE_PER_SIDE, 2)
            cycle["winning_outcome"] = winner
    pnl = float(cycle.get("pnl", 0) or 0)
    state["bankroll"] = round(float(state["bankroll"]) + float(cycle.get("cash_returned", 0) or 0), 2)
    state["total_pnl"] = round(float(state["total_pnl"]) + pnl, 2)
    state["total_cycles"] = int(state.get("total_cycles", 0) or 0) + 1
    if pnl >= 0:
        state["win_count"] = int(state.get("win_count", 0) or 0) + 1
    else:
        state["loss_count"] = int(state.get("loss_count", 0) or 0) + 1
    state.setdefault("past_cycles", []).append(cycle)
    state["current_cycle"] = None
    log_event("BTC_CLOSE", reason=reason, **cycle)


def run_once() -> dict[str, Any]:
    state = load_state()
    state["last_scan"] = now_iso()
    market, error = find_current_market()
    state["last_error"] = error
    executor = PolymarketExecutionAdapter()
    state["live_enabled"] = bool(not executor.config.dry_run and executor.config.live_enabled)

    if not market:
        save_state(state)
        return state

    cycle = state.get("current_cycle")
    if not cycle or cycle.get("slug") != market["slug"]:
        if cycle and cycle.get("status") == "open":
            _close_cycle(state, "market_rotated")
        combined = market["up_price"] + market["down_price"]
        if combined <= 0 or combined > MAX_COMBINED_ENTRY:
            state["last_error"] = f"entry rejected: combined executable asks={combined:.4f}"
            save_state(state)
            return state

        total_stake = STAKE_PER_SIDE * 2
        if float(state["bankroll"]) < total_stake:
            state["last_error"] = "bankroll below required $100 entry"
            save_state(state)
            return state

        up_order = executor.place_limit_buy(
            token_id=market["up_token_id"],
            price=float(market["up_price"]),
            stake=STAKE_PER_SIDE,
            metadata={"strategy": "btc-5m-straddle", "side": "UP", "slug": market["slug"]},
        )
        down_order = executor.place_limit_buy(
            token_id=market["down_token_id"],
            price=float(market["down_price"]),
            stake=STAKE_PER_SIDE,
            metadata={"strategy": "btc-5m-straddle", "side": "DOWN", "slug": market["slug"]},
        )
        state["bankroll"] = round(float(state["bankroll"]) - total_stake, 2)
        state["current_cycle"] = {
            "slug": market["slug"],
            "question": market["question"],
            "condition_id": market["condition_id"],
            "status": "open",
            "opened_at": now_iso(),
            "end_date": market["end_date"],
            "up_entry": round(float(market["up_price"]), 6),
            "down_entry": round(float(market["down_price"]), 6),
            "up_current": round(float(market["up_price"]), 6),
            "down_current": round(float(market["down_price"]), 6),
            "up_bid": round(float(market["up_bid"]), 6),
            "down_bid": round(float(market["down_bid"]), 6),
            "book_depth": round(float(market["book_depth"]), 2),
            "up_shares": up_order["shares"],
            "down_shares": down_order["shares"],
            "up_sold": False,
            "down_sold": False,
            "stake_per_side": STAKE_PER_SIDE,
            "cash_returned": 0.0,
            "pnl": 0.0,
            "orders": [up_order, down_order],
        }
        log_event("BTC_ENTER", **state["current_cycle"])
    else:
        up = float(market["up_price"])
        down = float(market["down_price"])
        cycle["up_current"] = round(up, 6)
        cycle["down_current"] = round(down, 6)
        cycle["up_bid"] = round(float(market["up_bid"]), 6)
        cycle["down_bid"] = round(float(market["down_bid"]), 6)
        up_change = (up - float(cycle["up_entry"])) / max(0.0001, float(cycle["up_entry"]))
        down_change = (down - float(cycle["down_entry"])) / max(0.0001, float(cycle["down_entry"]))

        if up_change <= -STOP_LOSS_PCT and not cycle.get("up_sold"):
            returned = up * float(cycle["up_shares"])
            executor.place_limit_sell(
                token_id=cycle.get("orders", [{}])[0].get("token_id", ""),
                price=up,
                shares=float(cycle["up_shares"]),
                metadata={"strategy": "btc-5m-straddle", "side": "UP", "reason": "stop_loss", "slug": cycle["slug"]},
            )
            cycle["up_sold"] = True
            cycle["up_exit"] = round(up, 6)
            cycle["cash_returned"] = round(float(cycle["cash_returned"]) + returned, 2)
            cycle["pnl"] = round(float(cycle["pnl"]) + returned - STAKE_PER_SIDE, 2)
            log_event("BTC_STOP", side="UP", price=up, returned=returned, slug=cycle["slug"])
        if down_change <= -STOP_LOSS_PCT and not cycle.get("down_sold"):
            returned = down * float(cycle["down_shares"])
            executor.place_limit_sell(
                token_id=cycle.get("orders", [{}, {}])[1].get("token_id", ""),
                price=down,
                shares=float(cycle["down_shares"]),
                metadata={"strategy": "btc-5m-straddle", "side": "DOWN", "reason": "stop_loss", "slug": cycle["slug"]},
            )
            cycle["down_sold"] = True
            cycle["down_exit"] = round(down, 6)
            cycle["cash_returned"] = round(float(cycle["cash_returned"]) + returned, 2)
            cycle["pnl"] = round(float(cycle["pnl"]) + returned - STAKE_PER_SIDE, 2)
            log_event("BTC_STOP", side="DOWN", price=down, returned=returned, slug=cycle["slug"])

    cycle = state.get("current_cycle")
    open_value = 0.0
    if cycle and cycle.get("status") == "open":
        if not cycle.get("up_sold"):
            open_value += float(cycle["up_current"]) * float(cycle["up_shares"])
        if not cycle.get("down_sold"):
            open_value += float(cycle["down_current"]) * float(cycle["down_shares"])
        open_value += float(cycle.get("cash_returned", 0) or 0)
    state["equity"] = round(float(state["bankroll"]) + open_value, 2)
    save_state(state)
    return state
