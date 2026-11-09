"""BTC/ETH 15-minute "buy winner late" dry-run bot.

Single truth API: Falcon/Polymarket Analytics for market discovery,
CLOB for executable prices. No Gamma mixing.

Strategy: Watch 15m windows, in the last 120s if one side is clearly
favored with decent volume, buy that side and hold to actual resolution.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_analytics import (
    AnalyticsError,
    PolymarketAnalyticsClient,
    first_float,
    first_str,
    parse_json_list,
    summarize_trades_volume,
)
from clob_pricing import fetch_book, quote_from_book, simulate_buy_fill
from strategy_logging import asset_from_slug, fill_quality, log_strategy_record, should_log_decision

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "data" / "btc_eth_15m_state.json"
EVENT_LOG = BASE_DIR / "data" / "btc_eth_15m_events.jsonl"

BANKROLL_INITIAL = 100.0
FLAT_STAKE = 10.0  # flat $10 stake per trade
MIN_VOLUME = 250.0  # min $ volume to consider a window
MIN_BOOK_DEPTH = 50.0  # min $ depth on the winner side
BUY_THRESHOLD = 0.78  # winner must be at $0.78+ executable VWAP
MAX_BUY_PRICE = 0.92  # don't buy if VWAP is above this
LOSER_BID_MAX = 0.20  # loser must be weak enough to confirm the move
SCAN_INTERVAL_SECS = 15  # how often prices are refreshed
MIN_ENTRY_SECONDS_LEFT = 90
LAST_N_SECONDS = 210  # tuned entry band: 90-210 seconds left
MAX_FILL_LEVELS = 1  # require the full stake at the best ask
MARKET_SLUGS = ["btc-updown-15m", "eth-updown-15m"]
MARKET_INTERVAL_SECONDS = 900
DISCOVERY_WINDOWS_AHEAD = 3
TRADE_VOLUME_FALLBACK_SECONDS = 900
RESOLUTION_GRACE_SECONDS = 45

MAX_COMBINED_COST = 1.05  # reject windows where combined ask > $1.05 (too tight)
SCHEMA_VERSION = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def default_state() -> dict[str, Any]:
    return {
        "mode": "btc-eth-15m-buy-winner-late",
        "schema_version": SCHEMA_VERSION,
        "bankroll": BANKROLL_INITIAL,
        "equity": BANKROLL_INITIAL,
        "trades": [],
        "windows": {},
        "last_scan": None,
        "last_error": None,
        "last_signal": {},
        "active_windows_summary": [],
        "total_pnl": 0.0,
        "total_trades": 0,
        "win_count": 0,
        "loss_count": 0,
    }


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text())
            if raw.get("schema_version") != SCHEMA_VERSION:
                return default_state()
            return {**default_state(), **raw}
        except Exception:
            pass
    return default_state()


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to tmp then rename
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.rename(STATE_FILE)


def log_event(event_type: str, **details: Any) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a") as f:
        f.write(json.dumps({"ts": now_iso(), "event_type": event_type, "details": details}, default=str) + "\n")


def _decision_record(strategy: str, result: dict[str, Any]) -> dict[str, Any]:
    window = result.get("window", {})
    fill = result.get("fill") or {}
    side = result.get("winner")
    best_ask = result.get("up_ask") if side == "Up" else result.get("down_ask") if side == "Down" else None
    vwap = result.get("buy_price") or fill.get("vwap")
    quality = fill_quality(best_ask, vwap, len(fill.get("levels", [])) if fill else None)
    return {
        "strategy": strategy,
        "action": result.get("action"),
        "reason": result.get("reason"),
        "slug": window.get("slug"),
        "asset": asset_from_slug(window.get("slug")),
        "question": window.get("question"),
        "side": side,
        "volume": window.get("volume"),
        "seconds_left": result.get("seconds_left"),
        "up_ask": result.get("up_ask"),
        "down_ask": result.get("down_ask"),
        "best_ask": best_ask,
        "vwap": vwap,
        "profit_pct": result.get("profit_pct"),
        "requested_stake": fill.get("requested_stake"),
        "filled_cost": fill.get("filled_cost"),
        "shares": fill.get("shares"),
        "levels_used": len(fill.get("levels", [])) if fill else None,
        "fill_levels": fill.get("levels", []),
        **quality,
    }


def log_decision_once(state: dict[str, Any], strategy: str, result: dict[str, Any]) -> None:
    window = result.get("window", {})
    key = "|".join([
        strategy,
        str(window.get("slug")),
        str(result.get("action")),
        str(result.get("reason")),
        str(result.get("winner")),
    ])
    if should_log_decision(state, key):
        log_strategy_record(_decision_record(strategy, result))


def _tokens(row: dict[str, Any]) -> tuple[str, str]:
    up = first_str(row, ("up_token_id", "side_a_token_id", "yes_token_id"))
    down = first_str(row, ("down_token_id", "side_b_token_id", "no_token_id"))
    if up and down:
        return up, down
    tokens = parse_json_list(row.get("clobTokenIds") or row.get("clob_token_ids"))
    if len(tokens) >= 2:
        return str(tokens[0]), str(tokens[1])
    return "", ""


def _volume_from_falcon_trades(client: PolymarketAnalyticsClient, slug: str) -> dict[str, Any]:
    try:
        trades = client.retrieve_recent_trades(market_slug=slug, limit=200, offset=0)
    except Exception as exc:
        return {"volume": 0.0, "source": "falcon_trades_error", "error": str(exc)[:180]}
    summary = summarize_trades_volume(trades)
    return {
        "volume": float(summary["trade_volume"]),
        "source": "falcon_trades",
        **summary,
    }


def _book_prices(up_token: str, down_token: str, label: str) -> dict[str, Any] | None:
    try:
        up_book = fetch_book(up_token)
        down_book = fetch_book(down_token)
        up_quote = quote_from_book(up_token, up_book, outcome="Up")
        down_quote = quote_from_book(down_token, down_book, outcome="Down")
    except Exception as e:
        logger.warning("Book fetch failed for %s: %s", label[:40], e)
        return None

    # Near resolution: winner has no asks, loser has no bids.
    up_ask = up_quote.best_ask if up_quote.best_ask is not None else 1.0
    down_ask = down_quote.best_ask if down_quote.best_ask is not None else 1.0
    up_bid = up_quote.best_bid if up_quote.best_bid is not None else 0.0
    down_bid = down_quote.best_bid if down_quote.best_bid is not None else 0.0

    return {
        "up_ask": up_ask,
        "down_ask": down_ask,
        "up_bid": up_bid,
        "down_bid": down_bid,
        "up_ask_depth": up_quote.ask_depth if up_quote.ask_depth > 0 else up_quote.bid_depth,
        "down_ask_depth": down_quote.ask_depth if down_quote.ask_depth > 0 else down_quote.bid_depth,
        "up_bid_depth": up_quote.bid_depth,
        "down_bid_depth": down_quote.bid_depth,
        "up_book": up_book,
        "down_book": down_book,
        "up_fill": simulate_buy_fill(up_token, up_book, FLAT_STAKE).to_dict(),
        "down_fill": simulate_buy_fill(down_token, down_book, FLAT_STAKE).to_dict(),
        "combined_ask": round(up_ask + down_ask, 6),
        "combined_bid": round(up_bid + down_bid, 6),
    }


def discover_windows(client: PolymarketAnalyticsClient) -> list[dict[str, Any]]:
    """Fetch active 15m windows from Falcon API."""
    windows = []
    now = now_ts()
    for slug in MARKET_SLUGS:
        current_start = math.floor(now / MARKET_INTERVAL_SECONDS) * MARKET_INTERVAL_SECONDS
        rows = []
        seen_slugs = set()
        for idx in range(DISCOVERY_WINDOWS_AHEAD):
            exact_slug = f"{slug}-{int(current_start + idx * MARKET_INTERVAL_SECONDS)}"
            try:
                exact_rows = client.retrieve_markets(
                    {"market_slug": exact_slug, "min_volume": "0", "closed": "False"},
                    limit=5, offset=0,
                )
            except AnalyticsError as exc:
                logger.warning("Falcon exact query failed for %s: %s", exact_slug, exc)
                continue
            for row in exact_rows:
                row_slug = first_str(row, ("slug", "market_slug"))
                if row_slug == exact_slug and row_slug not in seen_slugs:
                    rows.append(row)
                    seen_slugs.add(row_slug)

        for row in rows:
            q = first_str(row, ("question", "title"))
            s = first_str(row, ("slug", "market_slug"))
            end = first_str(row, ("end_date", "endDate", "endDateIso"))
            vol = first_float(row, ("volume_total", "volume", "volumeNum"), 0.0) or 0.0

            if not s or not end:
                continue

            up_t, down_t = _tokens(row)
            if not up_t or not down_t:
                continue

            try:
                end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue

            volume_source = "falcon_market"
            trade_volume = None
            trade_count = None
            latest_trade_ts = None
            seconds_left = end_ts - now
            if 0 < seconds_left <= TRADE_VOLUME_FALLBACK_SECONDS and vol < MIN_VOLUME:
                fallback = _volume_from_falcon_trades(client, s)
                trade_volume = fallback.get("trade_volume", fallback.get("volume"))
                trade_count = fallback.get("trade_count")
                latest_trade_ts = fallback.get("latest_trade_ts")
                if float(fallback.get("volume") or 0.0) > vol:
                    vol = float(fallback["volume"])
                    volume_source = fallback["source"]

            windows.append({
                "slug": s,
                "question": q,
                "end_date": end,
                "end_ts": end_ts,
                "volume": vol,
                "market_volume": first_float(row, ("volume_total", "volume", "volumeNum"), 0.0) or 0.0,
                "volume_source": volume_source,
                "trade_volume": trade_volume,
                "trade_count": trade_count,
                "latest_trade_ts": latest_trade_ts,
                "up_token": up_t,
                "down_token": down_t,
                "condition_id": first_str(row, ("condition_id", "conditionId")),
            })

    # Sort by end date ascending (nearest to close first)
    windows.sort(key=lambda w: w["end_ts"])
    return windows


def evaluate_window(window: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """Check if a window has a buy-winner-late opportunity. Returns None or opportunity dict."""
    slug = window["slug"]
    seconds_left = window["end_ts"] - now_ts()

    # Don't trade windows already resolved
    if seconds_left <= 0:
        return None

    # Don't trade if we already have an active trade on this slug
    if any(t.get("slug") == slug and t.get("status") == "open" for t in state.get("trades", [])):
        return None

    if seconds_left < MIN_ENTRY_SECONDS_LEFT:
        return {
            "window": window,
            "action": "skip",
            "reason": f"outside_time_band ({int(seconds_left)}s)",
            "seconds_left": int(seconds_left),
        }

    # Check volume threshold
    if window["volume"] < MIN_VOLUME:
        return {
            "window": window,
            "action": "skip",
            "reason": f"low_volume (${window['volume']:.0f})",
            "seconds_left": int(seconds_left),
        }

    # Get book prices
    book = _book_prices(window["up_token"], window["down_token"], window["question"])
    if book is None:
        return {"window": window, "action": "skip", "reason": "no_book"}

    # Check for winner at $0.95+
    winner = None
    fill = None
    depth = None

    if BUY_THRESHOLD <= book["up_ask"] <= MAX_BUY_PRICE and book["up_ask_depth"] >= MIN_BOOK_DEPTH and book["down_bid"] <= LOSER_BID_MAX:
        winner = "Up"
        fill = book["up_fill"]
        depth = book["up_ask_depth"]
        loser_bid = book["down_bid"]
        loser_depth = book["down_bid_depth"]
    elif BUY_THRESHOLD <= book["down_ask"] <= MAX_BUY_PRICE and book["down_ask_depth"] >= MIN_BOOK_DEPTH and book["up_bid"] <= LOSER_BID_MAX:
        winner = "Down"
        fill = book["down_fill"]
        depth = book["down_ask_depth"]
        loser_bid = book["up_bid"]
        loser_depth = book["up_bid_depth"]
    else:
        reason_parts = []
        if max(book["up_ask"], book["down_ask"]) < BUY_THRESHOLD:
            reason_parts.append("winner_vwap_below_threshold")
        if min(book["up_bid"], book["down_bid"]) > LOSER_BID_MAX:
            reason_parts.append("loser_bid_too_high")
        reason = " ".join(reason_parts) or f"no_clear_winner (up={book['up_ask']:.3f} down={book['down_ask']:.3f})"
        return {
            "window": window,
            "action": "waiting",
            "reason": reason,
            "up_ask": book["up_ask"],
            "down_ask": book["down_ask"],
            "seconds_left": int(seconds_left),
        }

    if not fill or not fill.get("complete"):
        return {
            "window": window,
            "action": "skip",
            "reason": "insufficient_ask_liquidity_for_stake",
            "up_ask": book["up_ask"],
            "down_ask": book["down_ask"],
            "seconds_left": int(seconds_left),
            "fill": fill,
        }

    if len(fill.get("levels", [])) > MAX_FILL_LEVELS:
        return {
            "window": window,
            "action": "skip",
            "reason": "fill_too_deep",
            "up_ask": book["up_ask"],
            "down_ask": book["down_ask"],
            "seconds_left": int(seconds_left),
            "fill": fill,
        }

    buy_price = float(fill.get("vwap") or 0)
    if buy_price <= 0 or buy_price > MAX_BUY_PRICE:
        return {
            "window": window,
            "action": "skip",
            "reason": f"vwap_too_expensive (${buy_price:.3f})",
            "up_ask": book["up_ask"],
            "down_ask": book["down_ask"],
            "seconds_left": int(seconds_left),
            "fill": fill,
        }

    # Opportunity found
    profit_pct = (1.0 - buy_price) * 100
    return {
        "window": window,
        "action": "buy_winner",
        "winner": winner,
        "buy_price": round(buy_price, 6),
        "loser_bid": loser_bid,
        "depth": depth,
        "loser_depth": loser_depth,
        "profit_pct": round(profit_pct, 2),
        "fill": fill,
        "up_ask": book["up_ask"],
        "down_ask": book["down_ask"],
        "seconds_left": int(seconds_left),
    }


def execute_trade(opportunity: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """Execute a dry-run trade. Returns the trade dict or None if can't afford it."""
    fill = opportunity["fill"]
    deploy = float(fill["filled_cost"])
    buy_price = opportunity["buy_price"]
    shares = round(float(fill["shares"]), 4)
    cost = round(float(fill["filled_cost"]), 2)
    payout = round(shares, 2)
    profit = round(payout - cost, 2)

    if cost <= 0 or cost > state["bankroll"]:
        return None

    trade = {
        "slug": opportunity["window"]["slug"],
        "question": opportunity["window"]["question"],
        "status": "open",
        "winner": opportunity["winner"],
        "buy_price": buy_price,
        "best_ask": opportunity["up_ask"] if opportunity["winner"] == "Up" else opportunity["down_ask"],
        "vwap": buy_price,
        "shares": shares,
        "cost": cost,
        "requested_stake": fill["requested_stake"],
        "expected_payout": payout,
        "expected_profit": profit,
        "profit_pct": opportunity["profit_pct"],
        "loser_bid": opportunity["loser_bid"],
        "loser_depth": opportunity["loser_depth"],
        "depth": opportunity["depth"],
        "fill_levels": fill["levels"],
        "entry_time": now_iso(),
        "exit_time": None,
        "actual_payout": None,
        "actual_pnl": None,
        "resolved": False,
    }

    state["bankroll"] = round(state["bankroll"] - cost, 2)
    state.setdefault("trades", []).append(trade)
    log_event("TRADE_ENTER", **trade)
    record = _decision_record("btc-eth-15m", {**opportunity, "action": "trade_enter"})
    log_strategy_record({**record, "status": "open"})
    return trade


def _normalize_outcome(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"up", "higher", "yes"} or "up" in text:
        return "up"
    if text in {"down", "lower", "no"} or "down" in text:
        return "down"
    return None


def _winning_outcome(client: PolymarketAnalyticsClient, slug: str) -> str | None:
    """Return the actual resolved outcome for a market, or None if not settled."""
    fields = (
        "winning_outcome",
        "winner",
        "resolved_outcome",
        "outcome",
        "result",
    )
    for closed in ("True", "False"):
        try:
            rows = client.retrieve_markets(
                {"market_slug": slug, "min_volume": "0", "closed": closed},
                limit=10,
                offset=0,
            )
        except Exception:
            continue
        for row in rows:
            row_slug = first_str(row, ("slug", "market_slug", "event_slug"))
            if row_slug != slug:
                continue
            for field in fields:
                outcome = _normalize_outcome(row.get(field))
                if outcome:
                    return outcome
    return None


def resolve_trades(state: dict[str, Any]) -> None:
    """Check open trades against actual resolved market outcomes."""
    client = PolymarketAnalyticsClient()
    for trade in state.get("trades", []):
        if trade.get("status") not in {"open", "awaiting_resolution"}:
            continue

        slug = trade["slug"]
        # Find the window by slug
        window = None
        for w in state.get("windows", {}).values():
            if w.get("slug") == slug:
                window = w
                break
        if window is None:
            market = state.get("windows", {}).get(slug, {})
            window = market if market else None
        if window is None:
            continue

        if now_ts() < window["end_ts"]:
            continue

        actual_winner = _winning_outcome(client, slug)
        if actual_winner is None:
            if now_ts() >= float(window["end_ts"]) + RESOLUTION_GRACE_SECONDS:
                first_pending = trade.get("status") != "awaiting_resolution"
                trade["status"] = "awaiting_resolution"
                trade["awaiting_resolution_since"] = trade.get("awaiting_resolution_since") or now_iso()
                if first_pending:
                    log_event("TRADE_AWAITING_RESOLUTION", slug=slug, question=trade.get("question", ""))
            continue

        bought_side = str(trade.get("winner", "")).lower()
        won = bought_side == actual_winner
        if won:
            trade["actual_payout"] = trade["expected_payout"]
            trade["actual_pnl"] = trade["expected_profit"]
        else:
            trade["actual_payout"] = 0.0
            trade["actual_pnl"] = -trade["cost"]

        trade["actual_winner"] = actual_winner
        trade["exit_time"] = now_iso()
        trade["resolved"] = True
        trade["status"] = "closed"

        state["bankroll"] = round(state["bankroll"] + trade["actual_payout"], 2)
        state["total_pnl"] = round(state["total_pnl"] + trade["actual_pnl"], 2)
        state["total_trades"] += 1
        if trade["actual_pnl"] >= 0:
            state["win_count"] += 1
        else:
            state["loss_count"] += 1

        log_event(
            "TRADE_RESOLVE",
            slug=slug,
            pnl=trade["actual_pnl"],
            bought=trade["winner"],
            actual=actual_winner,
            question=trade.get("question", ""),
        )
        log_strategy_record({
            "strategy": "btc-eth-15m",
            "action": "trade_resolve",
            "reason": "resolved",
            "slug": slug,
            "asset": asset_from_slug(slug),
            "question": trade.get("question"),
            "side": trade.get("winner"),
            "best_ask": trade.get("best_ask"),
            "vwap": trade.get("vwap") or trade.get("buy_price"),
            "volume": window.get("volume"),
            "seconds_left": 0,
            "pnl": trade.get("actual_pnl"),
            "actual_winner": actual_winner,
            "won": won,
            "requested_stake": trade.get("requested_stake"),
            "filled_cost": trade.get("cost"),
            "shares": trade.get("shares"),
            "levels_used": len(trade.get("fill_levels", [])),
            "fill_levels": trade.get("fill_levels", []),
            **fill_quality(trade.get("best_ask"), trade.get("vwap") or trade.get("buy_price"), len(trade.get("fill_levels", []))),
        })


def run_once() -> dict[str, Any]:
    state = load_state()
    state["last_scan"] = now_iso()
    client = PolymarketAnalyticsClient()

    # Resolve completed windows first
    resolve_trades(state)

    # Discover windows
    try:
        windows = discover_windows(client)
    except Exception as e:
        state["last_error"] = f"discovery: {e}"
        save_state(state)
        return state

    # Store windows index by slug for resolution
    for w in windows:
        state.setdefault("windows", {})[w["slug"]] = w

    # Find active windows
    now = now_ts()
    active_windows = [w for w in windows if w["end_ts"] > now and w["end_ts"] - now <= LAST_N_SECONDS]
    active_windows.sort(key=lambda w: w["end_ts"])

    # Record all windows for dashboard
    state["active_windows_summary"] = []
    for w in windows[:10]:  # lots of future windows
        secs_left = int(w["end_ts"] - now_ts())
        if secs_left <= 0:
            continue
        up_t, down_t = _tokens({"up_token_id": w["up_token"], "down_token_id": w["down_token"]})
        book = _book_prices(up_t, down_t, w["question"])
        if book:
            state["active_windows_summary"].append({
                "slug": w["slug"],
                "question": w["question"],
                "end_date": w["end_date"],
                "seconds_left": secs_left,
                "volume": round(w["volume"], 0),
                "volume_source": w.get("volume_source"),
                "trade_count": w.get("trade_count"),
                "latest_trade_ts": w.get("latest_trade_ts"),
                "up_ask": book["up_ask"],
                "down_ask": book["down_ask"],
                "up_bid": book["up_bid"],
                "down_bid": book["down_bid"],
            })

    # Evaluate each window
    best_opp = None
    for w in active_windows:
        opp = evaluate_window(w, state)
        if opp is None:
            continue
        if opp["action"] != "buy_winner":
            log_decision_once(state, "btc-eth-15m", opp)
        if opp["action"] == "buy_winner":
            log_decision_once(state, "btc-eth-15m", opp)
            if best_opp is None or opp["seconds_left"] < best_opp["seconds_left"]:
                best_opp = opp

    # Execute best opportunity
    trade = None
    if best_opp:
        trade = execute_trade(best_opp, state)
        if trade:
            state["last_signal"] = {
                "slug": best_opp["window"]["slug"],
                "winner": best_opp["winner"],
                "buy_price": best_opp["buy_price"],
                "best_ask": best_opp["up_ask"] if best_opp["winner"] == "Up" else best_opp["down_ask"],
                "profit_pct": best_opp["profit_pct"],
                "requested_stake": best_opp["fill"]["requested_stake"],
                "filled_cost": best_opp["fill"]["filled_cost"],
                "shares": best_opp["fill"]["shares"],
                "levels_used": len(best_opp["fill"]["levels"]),
                "seconds_left": best_opp["seconds_left"],
                "executed": True,
            }
        else:
            state["last_signal"] = {
                "reason": "insufficient_bankroll",
                "cost": FLAT_STAKE,
                "bankroll": state["bankroll"],
            }
    else:
        # Still show the best waiting opportunity
        waiting = []
        for w in active_windows:
            opp = evaluate_window(w, state)
            if opp and opp.get("action") != "buy_winner":
                log_decision_once(state, "btc-eth-15m", opp)
                waiting.append(opp)
        if waiting:
            best_waiting = min(waiting, key=lambda x: x.get("seconds_left", 999))
            state["last_signal"] = {
                "reason": best_waiting.get("reason"),
                "up_ask": best_waiting.get("up_ask"),
                "down_ask": best_waiting.get("down_ask"),
                "seconds_left": best_waiting.get("seconds_left"),
                "executed": False,
            }
        else:
            state["last_signal"] = {"reason": "no_window_in_entry_zone", "executed": False}

    # Calculate equity
    open_value = sum(
        float(t.get("expected_payout", 0) or 0) for t in state.get("trades", [])
        if t.get("status") == "open"
    )
    state["equity"] = round(state["bankroll"] + open_value, 2)
    save_state(state)
    return state
