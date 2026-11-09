"""Shadow dry-test strategies derived from the micro-bankroll report.

These strategies do not place real orders. They share the same Falcon market
discovery and CLOB order-book source as the tuned 15m bot, but keep separate
state files so their results can be compared cleanly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from btc_eth_15m_bot import (
    LAST_N_SECONDS,
    MAX_FILL_LEVELS,
    MIN_ENTRY_SECONDS_LEFT,
    MIN_VOLUME,
    RESOLUTION_GRACE_SECONDS,
    _book_prices,
    _winning_outcome,
    discover_windows,
    now_iso,
    now_ts,
)
from clob_pricing import simulate_buy_fill
from polymarket_analytics import PolymarketAnalyticsClient

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STAKE = 5.0
BANKROLL_INITIAL = 100.0
BUY_MIN = 0.78
BUY_MAX = 0.92
LOSER_BID_MAX = 0.20
PASSIVE_ASK_MIN = 0.78
PASSIVE_ASK_MAX = 0.86
PASSIVE_TICKS = 0.02
PASSIVE_ORDER_TTL_SECONDS = 60
SESSION_START_UTC = 12
SESSION_END_UTC = 16
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StrategyConfig:
    code: str
    title: str
    state_file: Path
    mode: str


STRATEGIES = {
    "passive": StrategyConfig(
        code="passive",
        title="B2 Passive Maker Late-Favorite",
        state_file=DATA_DIR / "shadow_b2_passive_state.json",
        mode="shadow-b2-passive-maker-late-favorite",
    ),
    "session": StrategyConfig(
        code="session",
        title="B4 Session-Filtered Late-Favorite",
        state_file=DATA_DIR / "shadow_b4_session_state.json",
        mode="shadow-b4-session-filtered-late-favorite",
    ),
    "spot": StrategyConfig(
        code="spot",
        title="B1 Spot/VWAP-Confirmed Late-Favorite",
        state_file=DATA_DIR / "shadow_b1_spot_confirmed_state.json",
        mode="shadow-b1-spot-vwap-confirmed-late-favorite",
    ),
}


def default_state(config: StrategyConfig) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "schema_version": SCHEMA_VERSION,
        "bankroll": BANKROLL_INITIAL,
        "equity": BANKROLL_INITIAL,
        "stake": STAKE,
        "trades": [],
        "orders": [],
        "windows": {},
        "active_windows_summary": [],
        "last_scan": None,
        "last_error": None,
        "last_signal": {},
        "total_pnl": 0.0,
        "total_trades": 0,
        "win_count": 0,
        "loss_count": 0,
        "reset_at": None,
    }


def load_state(code: str) -> dict[str, Any]:
    config = STRATEGIES[code]
    if config.state_file.exists():
        try:
            raw = json.loads(config.state_file.read_text())
            if raw.get("schema_version") == SCHEMA_VERSION:
                return {**default_state(config), **raw}
        except Exception:
            pass
    return default_state(config)


def save_state(code: str, state: dict[str, Any]) -> None:
    config = STRATEGIES[code]
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.rename(config.state_file)


def reset_states() -> None:
    ts = now_iso()
    for code, config in STRATEGIES.items():
        state = default_state(config)
        state["reset_at"] = ts
        save_state(code, state)


def _asset(window: dict[str, Any]) -> str:
    slug = str(window.get("slug") or "")
    return "ETH" if slug.startswith("eth-") else "BTC"


def _side_metrics(book: dict[str, Any], side: str) -> dict[str, Any]:
    if side == "Up":
        return {
            "ask": book["up_ask"],
            "bid": book["up_bid"],
            "depth": book["up_ask_depth"],
            "loser_bid": book["down_bid"],
            "loser_depth": book["down_bid_depth"],
            "token_key": "up_token",
            "book": book["up_book"],
        }
    return {
        "ask": book["down_ask"],
        "bid": book["down_bid"],
        "depth": book["down_ask_depth"],
        "loser_bid": book["up_bid"],
        "loser_depth": book["up_bid_depth"],
        "token_key": "down_token",
        "book": book["down_book"],
    }


def _favorite(book: dict[str, Any]) -> str | None:
    up_ok = book["up_ask"] >= book["down_ask"]
    return "Up" if up_ok else "Down"


def _is_session_now() -> bool:
    hour = datetime.now(timezone.utc).hour
    return SESSION_START_UTC <= hour < SESSION_END_UTC


_spot_cache: dict[str, tuple[float, dict[str, float]]] = {}


def _spot_vwap(asset: str) -> dict[str, float] | None:
    now = now_ts()
    cached = _spot_cache.get(asset)
    if cached and now - cached[0] < 20:
        return cached[1]
    symbol = "BTCUSDT" if asset == "BTC" else "ETHUSDT"
    try:
        ticker = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=(3, 5),
        ).json()
        klines = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 15},
            timeout=(3, 5),
        ).json()
        price = float(ticker["price"])
        num = 0.0
        den = 0.0
        for kline in klines:
            high = float(kline[2])
            low = float(kline[3])
            close = float(kline[4])
            volume = float(kline[5])
            typical = (high + low + close) / 3.0
            num += typical * volume
            den += volume
        if den <= 0:
            return None
        data = {"price": price, "vwap": num / den}
        _spot_cache[asset] = (now, data)
        return data
    except Exception:
        return None


def _resolve_trades(code: str, state: dict[str, Any], client: PolymarketAnalyticsClient) -> None:
    for trade in state.get("trades", []):
        if trade.get("status") not in {"open", "awaiting_resolution"}:
            continue
        window = state.get("windows", {}).get(trade.get("slug"), {})
        end_ts = float(window.get("end_ts") or trade.get("end_ts") or 0)
        if end_ts and now_ts() < end_ts:
            continue
        actual = _winning_outcome(client, trade["slug"])
        if actual is None:
            if end_ts and now_ts() >= end_ts + RESOLUTION_GRACE_SECONDS:
                trade["status"] = "awaiting_resolution"
                trade["awaiting_resolution_since"] = trade.get("awaiting_resolution_since") or now_iso()
            continue
        won = str(trade.get("side", "")).lower() == actual
        trade["actual_winner"] = actual
        trade["exit_time"] = now_iso()
        trade["status"] = "closed"
        trade["resolved"] = True
        if won:
            trade["actual_payout"] = trade["expected_payout"]
            trade["actual_pnl"] = trade["expected_profit"]
        else:
            trade["actual_payout"] = 0.0
            trade["actual_pnl"] = -trade["cost"]
        state["bankroll"] = round(state["bankroll"] + trade["actual_payout"], 2)
        state["total_pnl"] = round(state["total_pnl"] + trade["actual_pnl"], 2)
        state["total_trades"] += 1
        if trade["actual_pnl"] >= 0:
            state["win_count"] += 1
        else:
            state["loss_count"] += 1


def _open_trade_count(state: dict[str, Any]) -> int:
    return len([t for t in state.get("trades", []) if t.get("status") == "open"])


def _enter_trade(code: str, state: dict[str, Any], window: dict[str, Any], side: str,
                 price: float, shares: float, cost: float, details: dict[str, Any]) -> None:
    if cost <= 0 or cost > state["bankroll"]:
        state["last_signal"] = {"executed": False, "reason": "insufficient_bankroll", "cost": cost}
        return
    payout = round(shares, 2)
    profit = round(payout - cost, 2)
    trade = {
        "slug": window["slug"],
        "question": window["question"],
        "end_ts": window["end_ts"],
        "status": "open",
        "side": side,
        "buy_price": round(price, 6),
        "shares": round(shares, 4),
        "cost": round(cost, 2),
        "expected_payout": payout,
        "expected_profit": profit,
        "profit_pct": round((1.0 - price) * 100, 2),
        "entry_time": now_iso(),
        "resolved": False,
        **details,
    }
    state["bankroll"] = round(state["bankroll"] - trade["cost"], 2)
    state.setdefault("trades", []).append(trade)
    state["last_signal"] = {
        "executed": True,
        "slug": window["slug"],
        "side": side,
        "buy_price": trade["buy_price"],
        "seconds_left": int(window["end_ts"] - now_ts()),
        "profit_pct": trade["profit_pct"],
    }


def _evaluate_taker(code: str, state: dict[str, Any], window: dict[str, Any],
                    book: dict[str, Any], require_session: bool = False,
                    require_spot: bool = False) -> None:
    seconds_left = window["end_ts"] - now_ts()
    if seconds_left <= 0 or seconds_left > LAST_N_SECONDS:
        return
    if seconds_left < MIN_ENTRY_SECONDS_LEFT:
        state["last_signal"] = {"executed": False, "reason": f"outside_time_band ({int(seconds_left)}s)", "seconds_left": int(seconds_left)}
        return
    if require_session and not _is_session_now():
        state["last_signal"] = {"executed": False, "reason": "outside_session", "seconds_left": int(seconds_left)}
        return
    if window["volume"] < MIN_VOLUME:
        state["last_signal"] = {"executed": False, "reason": f"low_volume (${window['volume']:.0f})", "seconds_left": int(seconds_left)}
        return

    side = _favorite(book)
    if not side:
        return
    metrics = _side_metrics(book, side)
    if not (BUY_MIN <= metrics["ask"] <= 0.92 and metrics["loser_bid"] <= LOSER_BID_MAX):
        state["last_signal"] = {
            "executed": False,
            "reason": f"no_clear_winner (up={book['up_ask']:.3f} down={book['down_ask']:.3f})",
            "seconds_left": int(seconds_left),
            "up_ask": book["up_ask"],
            "down_ask": book["down_ask"],
        }
        return
    if require_spot:
        spot = _spot_vwap(_asset(window))
        if spot is None:
            state["last_signal"] = {"executed": False, "reason": "no_spot_vwap", "seconds_left": int(seconds_left)}
            return
        confirmed = spot["price"] > spot["vwap"] if side == "Up" else spot["price"] < spot["vwap"]
        if not confirmed:
            state["last_signal"] = {
                "executed": False,
                "reason": "spot_vwap_not_confirmed",
                "side": side,
                "spot": round(spot["price"], 4),
                "vwap": round(spot["vwap"], 4),
                "seconds_left": int(seconds_left),
            }
            return
    fill = simulate_buy_fill(window[metrics["token_key"]], metrics["book"], STAKE).to_dict()
    if not fill.get("complete"):
        state["last_signal"] = {"executed": False, "reason": "insufficient_ask_liquidity", "seconds_left": int(seconds_left)}
        return
    if len(fill.get("levels", [])) > MAX_FILL_LEVELS:
        state["last_signal"] = {"executed": False, "reason": "fill_too_deep", "seconds_left": int(seconds_left)}
        return
    price = float(fill["vwap"])
    if price > 0.92:
        state["last_signal"] = {"executed": False, "reason": f"vwap_too_expensive (${price:.3f})", "seconds_left": int(seconds_left)}
        return
    _enter_trade(code, state, window, side, price, float(fill["shares"]), float(fill["filled_cost"]), {
        "strategy_code": code,
        "volume": window.get("volume"),
        "volume_source": window.get("volume_source"),
        "fill_levels": fill.get("levels", []),
    })


def _evaluate_passive(state: dict[str, Any], window: dict[str, Any], book: dict[str, Any]) -> None:
    seconds_left = window["end_ts"] - now_ts()
    if seconds_left <= 0 or seconds_left > LAST_N_SECONDS or seconds_left < MIN_ENTRY_SECONDS_LEFT:
        return
    if window["volume"] < MIN_VOLUME:
        state["last_signal"] = {"executed": False, "reason": f"low_volume (${window['volume']:.0f})", "seconds_left": int(seconds_left)}
        return
    side = _favorite(book)
    if not side:
        return
    metrics = _side_metrics(book, side)
    if not (PASSIVE_ASK_MIN <= metrics["ask"] <= PASSIVE_ASK_MAX and metrics["loser_bid"] <= LOSER_BID_MAX):
        state["last_signal"] = {
            "executed": False,
            "reason": f"no_passive_setup (up={book['up_ask']:.3f} down={book['down_ask']:.3f})",
            "seconds_left": int(seconds_left),
        }
        return
    limit_price = round(max(0.01, metrics["ask"] - PASSIVE_TICKS), 4)
    order = {
        "slug": window["slug"],
        "question": window["question"],
        "side": side,
        "limit_price": limit_price,
        "stake": STAKE,
        "created_ts": now_ts(),
        "expires_ts": now_ts() + PASSIVE_ORDER_TTL_SECONDS,
        "status": "open",
    }
    state.setdefault("orders", []).append(order)
    state["last_signal"] = {
        "executed": False,
        "reason": "passive_order_posted",
        "slug": window["slug"],
        "side": side,
        "limit_price": limit_price,
        "seconds_left": int(seconds_left),
    }


def _process_passive_orders(state: dict[str, Any], windows_by_slug: dict[str, dict[str, Any]],
                            book_cache: dict[str, dict[str, Any]]) -> None:
    for order in state.get("orders", []):
        if order.get("status") != "open":
            continue
        window = windows_by_slug.get(order["slug"])
        if not window:
            if now_ts() > float(order["expires_ts"]):
                order["status"] = "cancelled"
                order["cancelled_at"] = now_iso()
                order["cancel_reason"] = "ttl_expired"
            continue
        book = book_cache.get(order["slug"])
        if not book:
            continue
        side = order["side"]
        metrics = _side_metrics(book, side)
        if now_ts() > float(order["expires_ts"]):
            order["status"] = "cancelled"
            order["cancelled_at"] = now_iso()
            order["cancel_reason"] = "ttl_expired"
            continue
        if metrics["ask"] <= float(order["limit_price"]):
            shares = STAKE / float(order["limit_price"])
            _enter_trade("passive", state, window, side, float(order["limit_price"]), shares, STAKE, {
                "strategy_code": "passive",
                "passive_order": True,
                "posted_limit": order["limit_price"],
                "fill_trigger_ask": metrics["ask"],
                "volume": window.get("volume"),
                "volume_source": window.get("volume_source"),
            })
            order["status"] = "filled"
            order["filled_at"] = now_iso()


def _update_summary(state: dict[str, Any], windows: list[dict[str, Any]], book_cache: dict[str, dict[str, Any]]) -> None:
    state["active_windows_summary"] = []
    for w in windows[:8]:
        secs = int(w["end_ts"] - now_ts())
        if secs <= 0:
            continue
        book = book_cache.get(w["slug"])
        state["active_windows_summary"].append({
            "slug": w["slug"],
            "question": w["question"],
            "seconds_left": secs,
            "volume": round(float(w.get("volume") or 0), 0),
            "volume_source": w.get("volume_source"),
            "up_ask": book.get("up_ask") if book else None,
            "down_ask": book.get("down_ask") if book else None,
        })


def run_all() -> dict[str, dict[str, Any]]:
    client = PolymarketAnalyticsClient()
    states = {code: load_state(code) for code in STRATEGIES}
    for state in states.values():
        state["last_scan"] = now_iso()
        state["last_error"] = None
    try:
        windows = discover_windows(client)
    except Exception as exc:
        for state in states.values():
            state["last_error"] = f"discovery: {exc}"
        for code, state in states.items():
            save_state(code, state)
        return states

    windows_by_slug = {w["slug"]: w for w in windows}
    book_cache: dict[str, dict[str, Any]] = {}
    for w in windows[:8]:
        try:
            book = _book_prices(w["up_token"], w["down_token"], w["question"])
        except Exception:
            book = None
        if book:
            book_cache[w["slug"]] = book

    for code, state in states.items():
        for w in windows:
            state.setdefault("windows", {})[w["slug"]] = w
        _resolve_trades(code, state, client)
        _update_summary(state, windows, book_cache)

    # Process existing passive orders before posting new ones.
    _process_passive_orders(states["passive"], windows_by_slug, book_cache)

    for w in windows:
        if _open_trade_count(states["passive"]) == 0 and not any(o.get("status") == "open" for o in states["passive"].get("orders", [])):
            book = book_cache.get(w["slug"])
            if book:
                _evaluate_passive(states["passive"], w, book)
        if _open_trade_count(states["session"]) == 0:
            book = book_cache.get(w["slug"])
            if book:
                _evaluate_taker("session", states["session"], w, book, require_session=True)
        if _open_trade_count(states["spot"]) == 0:
            book = book_cache.get(w["slug"])
            if book:
                _evaluate_taker("spot", states["spot"], w, book, require_spot=True)

    for state in states.values():
        if not state.get("last_signal"):
            state["last_signal"] = {"executed": False, "reason": "no_window_in_entry_zone"}
        open_value = sum(float(t.get("expected_payout", 0) or 0) for t in state.get("trades", []) if t.get("status") == "open")
        state["equity"] = round(state["bankroll"] + open_value, 2)

    for code, state in states.items():
        save_state(code, state)
    return states
