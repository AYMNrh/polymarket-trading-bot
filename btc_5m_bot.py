"""BTC 5-minute "buy winner late" dry-run bot (v1.1).

Strategy: Watch 5m UP/DOWN windows, in last 240s determine the favored
side by bids, then enter when the winner's ask >= $0.85 (market has
decided), the loser is <= $0.25, and book depth >= $500. Hold to
resolution.

Parameters:
  - Bankroll: $100
  - Entry window: last 240s (from midpoint of 5m window)
  - Entry condition: winner ask >= $0.85, loser ask <= $0.25, depth >= $500
  - Position: 10% of bankroll, cap $10 while bankroll < $200
  - Max 1 open trade
  - Exit: hold to resolution
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
STATE_FILE = BASE_DIR / "data" / "btc_5m_state.json"
EVENT_LOG = BASE_DIR / "data" / "btc_5m_events.jsonl"

BANKROLL_INITIAL = 100.0
STAKE_PCT = 0.10           # deploy 10% of bankroll per trade
STAKE_CAP = 10.0            # cap single stake at $10 while bankroll < $200
MAX_OPEN_TRADES = 2
MAX_OPEN_TRADES_PER_ASSET = 1
MIN_BOOK_DEPTH = 500.0      # $500 liquidity on winner side
BUY_THRESHOLD = 0.75        # winner ask must be >= this price (market decided)
MAX_BUY_PRICE = 0.95        # don't buy if winner ask > this (no profit room)
MIN_VOLUME = 100.0          # min $ Falcon volume to consider a window
MIN_ENTRY_SECONDS_LEFT = 210
MAX_ENTRY_SECONDS_LEFT = 300
LOOKAHEAD_SECONDS = MAX_ENTRY_SECONDS_LEFT
MAX_FILL_LEVELS = 1         # require the full stake at the best ask
MARKETS = [
    {"asset": "BTC", "slug": "btc-updown-5m", "trade_enabled": True},
    {"asset": "SOL", "slug": "sol-updown-5m", "trade_enabled": False},
]
MARKET_SLUG = "btc-updown-5m"
MARKET_INTERVAL_SECONDS = 300
DISCOVERY_WINDOWS_AHEAD = 4
TRADE_VOLUME_FALLBACK_SECONDS = 600
RESOLUTION_GRACE_SECONDS = 45

SCHEMA_VERSION = 5  # buy-winner-late-v1 with clean opposite check


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def default_state() -> dict[str, Any]:
    return {
        "mode": "btc-5m-buy-winner-late-v1",
        "schema_version": SCHEMA_VERSION,
        "bankroll": BANKROLL_INITIAL,
        "equity": BANKROLL_INITIAL,
        "trades": [],
        "windows": {},
        "last_scan": None,
        "last_error": None,
        "last_signal": {},
        "active_windows_summary": [],
        "asset_stats": {},
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
    best_ask = result.get("best_ask")
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


def _book_prices(up_token: str, down_token: str) -> dict[str, Any] | None:
    try:
        up_book = fetch_book(up_token)
        down_book = fetch_book(down_token)
        up_quote = quote_from_book(up_token, up_book, outcome="Up")
        down_quote = quote_from_book(down_token, down_book, outcome="Down")
    except Exception as e:
        logger.warning("Book fetch failed: %s", e)
        return None

    # Near resolution: winner has no asks, loser has no bids.
    # Use fallbacks so we don't go blind.
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
        "combined_ask": round(up_ask + down_ask, 6),
        "combined_bid": round(up_bid + down_bid, 6),
    }


def discover_windows(client: PolymarketAnalyticsClient) -> list[dict[str, Any]]:
    """Fetch active 5m windows from Falcon API."""
    windows = []
    now = now_ts()
    current_start = math.floor(now / MARKET_INTERVAL_SECONDS) * MARKET_INTERVAL_SECONDS
    rows = []
    seen_slugs = set()
    meta_by_slug = {m["slug"]: m for m in MARKETS}
    for market in MARKETS:
        for idx in range(DISCOVERY_WINDOWS_AHEAD):
            exact_slug = f"{market['slug']}-{int(current_start + idx * MARKET_INTERVAL_SECONDS)}"
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
                    row["_market_asset"] = market["asset"]
                    row["_trade_enabled"] = market["trade_enabled"]
                    rows.append(row)
                    seen_slugs.add(row_slug)

    for row in rows:
        q = first_str(row, ("question", "title"))
        s = first_str(row, ("slug", "market_slug"))
        end = first_str(row, ("end_date", "endDate", "endDateIso"))
        vol = first_float(row, ("volume_total", "volume", "volumeNum"), 0.0) or 0.0

        if not s or not end:
            continue
        asset = first_str(row, ("_market_asset",), "")
        trade_enabled = bool(row.get("_trade_enabled"))
        if not asset:
            for prefix, meta in meta_by_slug.items():
                if s.startswith(prefix):
                    asset = meta["asset"]
                    trade_enabled = meta["trade_enabled"]
                    break
        if not asset:
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
            "asset": asset,
            "trade_enabled": trade_enabled,
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

    windows.sort(key=lambda w: w["end_ts"])
    return windows


def evaluate_window(window: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """Check if a 5m window has a buy-winner-late opportunity."""
    slug = window["slug"]
    seconds_left = window["end_ts"] - now_ts()

    if seconds_left <= 0:
        return None

    if not window.get("trade_enabled", True):
        return {
            "window": window,
            "action": "watch",
            "reason": "watch_only",
            "seconds_left": int(seconds_left),
        }

    # Don't trade if we have an open trade on this slug
    if any(t.get("slug") == slug and t.get("status") == "open" for t in state.get("trades", [])):
        return None

    asset = window.get("asset") or asset_from_slug(slug)
    if any((t.get("asset") or asset_from_slug(t.get("slug"))) == asset and t.get("status") == "open" for t in state.get("trades", [])):
        return None

    # Only evaluate in the empirically strongest part of the 5m window.
    if seconds_left > LOOKAHEAD_SECONDS:
        return None
    if seconds_left < MIN_ENTRY_SECONDS_LEFT:
        return {
            "window": window,
            "action": "skip",
            "reason": f"outside_time_band ({int(seconds_left)}s)",
            "seconds_left": int(seconds_left),
        }

    # Volume filter
    if window["volume"] < MIN_VOLUME:
        return {
            "window": window,
            "action": "skip",
            "reason": f"low_volume (${window['volume']:.0f})",
            "seconds_left": int(seconds_left),
        }

    # Get book prices
    book = _book_prices(window["up_token"], window["down_token"])
    if book is None:
        return {"window": window, "action": "skip", "reason": "no_book"}

    # Check if one side is clearly favored
    winner = None
    fill = None
    depth = None
    wait_reason = None

    # Determine winner by bids (what people pay = market sentiment)
    if book["up_bid"] >= book["down_bid"]:
        winner_side, winner_ask, winner_depth, loser_ask = (
            "Up", book["up_ask"], book["up_ask_depth"], book["down_ask"]
        )
    else:
        winner_side, winner_ask, winner_depth, loser_ask = (
            "Down", book["down_ask"], book["down_ask_depth"], book["up_ask"]
        )

    # Check entry: winner expensive (market decided), but not maxed out, enough depth
    if BUY_THRESHOLD <= winner_ask <= MAX_BUY_PRICE and winner_depth >= MIN_BOOK_DEPTH:
        winner = winner_side
        stake = calc_stake(state)
        fill = simulate_buy_fill(
            window["up_token"] if winner == "Up" else window["down_token"],
            book["up_book"] if winner == "Up" else book["down_book"],
            stake,
        ).to_dict()
        depth = winner_depth
    else:
        # Build a descriptive wait reason
        parts = []
        if winner_ask < BUY_THRESHOLD:
            parts.append(f"{winner_side}_ask=${winner_ask:.3f}")
        if winner_ask > MAX_BUY_PRICE:
            parts.append(f"{winner_side}_too_expensive (${winner_ask:.3f})")
        if winner_depth < MIN_BOOK_DEPTH:
            parts.append(f"{winner_side}_depth=${winner_depth:.0f}")
        wait_reason = " ".join(parts) or f"no_divergence (up=${book['up_ask']:.3f} down=${book['down_ask']:.3f})"

    if winner:
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
        profit_pct = (1.0 - buy_price) * 100
        return {
            "window": window,
            "action": "buy_winner",
            "winner": winner,
            "buy_price": round(buy_price, 6),
            "best_ask": winner_ask,
            "depth": depth,
            "profit_pct": round(profit_pct, 2),
            "fill": fill,
            "up_ask": book["up_ask"],
            "down_ask": book["down_ask"],
            "seconds_left": int(seconds_left),
        }

    # No clear winner yet
    return {
        "window": window,
        "action": "waiting",
        "reason": wait_reason,
        "up_ask": book["up_ask"],
        "down_ask": book["down_ask"],
        "seconds_left": int(seconds_left),
    }


def calc_stake(state: dict[str, Any]) -> float:
    """Calculate stake: 10% of bankroll, capped at $10 while bankroll < $200."""
    raw = state["bankroll"] * STAKE_PCT
    if state["bankroll"] < 200:
        return min(raw, STAKE_CAP)
    return round(raw, 2)


def execute_trade(opportunity: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """Execute a dry-run trade. Returns the trade dict or None if can't afford it."""
    # Check max open trades
    open_trades = [t for t in state.get("trades", []) if t.get("status") == "open"]
    if len(open_trades) >= MAX_OPEN_TRADES:
        return None
    asset = opportunity["window"].get("asset") or asset_from_slug(opportunity["window"].get("slug"))
    open_asset_trades = [
        t for t in open_trades
        if (t.get("asset") or asset_from_slug(t.get("slug"))) == asset
    ]
    if len(open_asset_trades) >= MAX_OPEN_TRADES_PER_ASSET:
        return None

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
        "asset": asset,
        "question": opportunity["window"]["question"],
        "status": "open",
        "winner": opportunity["winner"],
        "buy_price": buy_price,
        "best_ask": opportunity["best_ask"],
        "vwap": buy_price,
        "shares": shares,
        "cost": cost,
        "requested_stake": fill["requested_stake"],
        "expected_payout": payout,
        "expected_profit": profit,
        "profit_pct": opportunity["profit_pct"],
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
    record = _decision_record("btc-5m", {**opportunity, "action": "trade_enter"})
    log_strategy_record({**record, "status": "open"})
    return trade


def resolve_trades(state: dict[str, Any]) -> None:
    """Check open trades for actual market resolution via Falcon API."""
    from polymarket_analytics import PolymarketAnalyticsClient, first_str

    client = PolymarketAnalyticsClient()

    for trade in state.get("trades", []):
        if trade.get("status") not in {"open", "awaiting_resolution"}:
            continue
        if trade.get("resolved"):
            continue

        slug = trade["slug"]

        # Query the market to check actual resolution status
        # Use the full slug to get the exact market, not the prefix
        try:
            rows = client.retrieve_markets(
                {"market_slug": slug, "min_volume": "0", "closed": "False"},
                limit=5, offset=0,
            )
        except Exception:
            rows = []

        # Check if this specific slug has resolved
        actual_winner = None
        for row in rows:
            s = first_str(row, ("slug", "market_slug"))
            if s == slug:
                winning = row.get("winning_outcome")
                if winning:
                    actual_winner = str(winning).lower()
                break

        if actual_winner is None:
            # Try closed markets
            try:
                rows = client.retrieve_markets(
                    {"market_slug": slug, "min_volume": "0", "closed": "True"},
                    limit=5, offset=0,
                )
                for row in rows:
                    s = first_str(row, ("slug", "market_slug"))
                    if s == slug:
                        winning = row.get("winning_outcome")
                        if winning:
                            actual_winner = str(winning).lower()
                        break
            except Exception:
                pass

        if actual_winner is None:
            window = state.get("windows", {}).get(slug, {})
            end_ts = float(window.get("end_ts") or 0)
            if end_ts and now_ts() >= end_ts + RESOLUTION_GRACE_SECONDS:
                first_pending = trade.get("status") != "awaiting_resolution"
                trade["status"] = "awaiting_resolution"
                trade["awaiting_resolution_since"] = trade.get("awaiting_resolution_since") or now_iso()
                if first_pending:
                    log_event("TRADE_AWAITING_RESOLUTION", slug=slug, question=trade.get("question", ""))
            continue

        # Market has resolved — determine if we won or lost
        our_side = str(trade.get("winner", "")).lower()
        won = our_side == actual_winner

        if won:
            trade["actual_payout"] = trade["expected_payout"]
            trade["actual_pnl"] = trade["expected_profit"]
        else:
            # We lost — token is worthless
            trade["actual_payout"] = 0.0
            trade["actual_pnl"] = -trade["cost"]

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

        log_event("TRADE_RESOLVE", slug=slug, pnl=trade["actual_pnl"],
                  winner=trade["winner"], actual=actual_winner,
                  question=trade.get("question", ""))
        log_strategy_record({
            "strategy": "btc-5m",
            "action": "trade_resolve",
            "reason": "resolved",
            "slug": slug,
            "asset": asset_from_slug(slug),
            "question": trade.get("question"),
            "side": trade.get("winner"),
            "best_ask": trade.get("best_ask"),
            "vwap": trade.get("vwap") or trade.get("buy_price"),
            "volume": state.get("windows", {}).get(slug, {}).get("volume"),
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

    # Find active windows near resolution
    now = now_ts()
    active_windows = [w for w in windows if w["end_ts"] > now and w["end_ts"] - now <= LOOKAHEAD_SECONDS]
    active_windows.sort(key=lambda w: w["end_ts"])

    # Build active windows summary for dashboard
    state["active_windows_summary"] = []
    shown = 0
    for w in windows:
        secs_left = int(w["end_ts"] - now_ts())
        if secs_left <= 0:
            continue
        book = _book_prices(w["up_token"], w["down_token"])
        if book:
            state["active_windows_summary"].append({
                "slug": w["slug"],
                "asset": w.get("asset"),
                "trade_enabled": w.get("trade_enabled", True),
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
            shown += 1
            if shown >= 10:
                break

    # Evaluate each window
    best_opp = None
    for w in active_windows:
        opp = evaluate_window(w, state)
        if opp is None:
            continue
        if opp["action"] != "buy_winner":
            log_decision_once(state, "btc-5m", opp)
        if opp["action"] == "buy_winner":
            log_decision_once(state, "btc-5m", opp)
            if best_opp is None or opp["seconds_left"] < best_opp["seconds_left"]:
                best_opp = opp

    # Execute best opportunity
    trade = None
    if best_opp:
        trade = execute_trade(best_opp, state)
        if trade:
            state["last_signal"] = {
                "slug": best_opp["window"]["slug"],
                "asset": best_opp["window"].get("asset"),
                "winner": best_opp["winner"],
                "buy_price": best_opp["buy_price"],
                "best_ask": best_opp["best_ask"],
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
                "reason": "insufficient_bankroll_or_max_trades",
                "cost": calc_stake(state),
                "bankroll": state["bankroll"],
                "open_trades": len([t for t in state.get("trades", []) if t.get("status") == "open"]),
            }
    else:
        # Show the best waiting opportunity
        waiting = []
        for w in active_windows:
            opp = evaluate_window(w, state)
            if opp and opp.get("action") != "buy_winner":
                log_decision_once(state, "btc-5m", opp)
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

    # Calculate equity
    open_value = sum(
        float(t.get("expected_payout", 0) or 0) for t in state.get("trades", [])
        if t.get("status") == "open"
    )
    state["equity"] = round(state["bankroll"] + open_value, 2)
    state["asset_stats"] = asset_stats(state)
    save_state(state)
    return state


def asset_stats(state: dict[str, Any]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for asset in [m["asset"] for m in MARKETS]:
        rows = [
            t for t in state.get("trades", [])
            if (t.get("asset") or asset_from_slug(t.get("slug"))) == asset
        ]
        closed = [t for t in rows if t.get("status") == "closed"]
        open_rows = [t for t in rows if t.get("status") == "open"]
        pending = [t for t in rows if t.get("status") == "awaiting_resolution"]
        pnl = round(sum(float(t.get("actual_pnl", 0) or 0) for t in closed), 2)
        wins = len([t for t in closed if float(t.get("actual_pnl", 0) or 0) >= 0])
        losses = len(closed) - wins
        stats[asset] = {
            "trades": len(closed),
            "open": len(open_rows),
            "pending": len(pending),
            "wins": wins,
            "losses": losses,
            "pnl": pnl,
            "trade_enabled": next((m["trade_enabled"] for m in MARKETS if m["asset"] == asset), False),
        }
    return stats
