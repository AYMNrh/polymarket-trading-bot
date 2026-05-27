#!/usr/bin/env python3
"""Real-price A/B strategy pilot using Polymarket CLOB books.

This runner tests two historical-data-derived strategies with a single wallet
execution path. It is dry-run by default and records every candidate, skip,
price, position, and close for dashboard/realtime analysis.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clob_pricing import BookQuote, executable_entry_price, executable_exit_price, quote_yes_market
from paper_trader import (
    FORECAST_LOCATIONS,
    STOP_LOSS_PCT,
    PaperTrader,
    _extract_bucket_bounds,
    _extract_market_date,
    _forecast_gap_f,
    _get_forecast_temp,
)
from wallet_execution import PolymarketExecutionAdapter

DATA_DIR = Path(__file__).parent / "data"
AB_STATE_FILE = DATA_DIR / "live_ab_state.json"
AB_EVENTS_LOG = DATA_DIR / "live_ab_events.jsonl"

STRATEGY_A = "strategy-a-bid-backed-middle"
STRATEGY_B = "strategy-b-bid-backed-tail-below"

DEFAULT_CONFIG = {
    "dry_run": True,
    "bankroll_limit": 20.0,
    "stake": 1.0,
    "max_open_positions": 3,
    "daily_max_loss": 5.0,
    "max_closed_trades": 20,
    "watch_scans_required": 2,
    "min_volume": 500.0,
    "max_entry": 0.002,
    "max_spread": 0.002,
    "min_bid_size": 1.0,
    "min_ask_size": 1.0,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _initial_state() -> dict[str, Any]:
    now = _now()
    return {
        "strategy": "live-ab-clob-pilot",
        "created_at": now,
        "updated_at": now,
        "config": DEFAULT_CONFIG.copy(),
        "positions": {},
        "watchlist": {},
        "closed_trades": 0,
        "spent": 0.0,
        "daily_realized_pnl": {},
        "last_cycle": {},
    }


def load_ab_state() -> dict[str, Any]:
    if AB_STATE_FILE.exists():
        try:
            state = json.loads(AB_STATE_FILE.read_text())
            state.setdefault("config", DEFAULT_CONFIG.copy())
            state["config"] = {**DEFAULT_CONFIG, **state.get("config", {})}
            state.setdefault("positions", {})
            state.setdefault("watchlist", {})
            state.setdefault("closed_trades", 0)
            state.setdefault("spent", 0.0)
            state.setdefault("daily_realized_pnl", {})
            state.setdefault("last_cycle", {})
            return state
        except Exception:
            pass
    return _initial_state()


def save_ab_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    state["updated_at"] = _now()
    AB_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def log_ab_event(event_type: str, message: str, **details: Any) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    entry = {
        "ts": _now(),
        "strategy": "live-ab-clob-pilot",
        "event_type": event_type,
        "message": message,
        "details": details,
    }
    with AB_EVENTS_LOG.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_ab_events(limit: int = 120) -> list[dict[str, Any]]:
    if not AB_EVENTS_LOG.exists():
        return []
    events: list[dict[str, Any]] = []
    with AB_EVENTS_LOG.open() as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events[-limit:][::-1]


def _city_from_title(title: str) -> str | None:
    lower = title.lower()
    for city in FORECAST_LOCATIONS:
        if city.lower() in lower:
            return city
    return None


def _forecast_context(market: dict[str, Any]) -> dict[str, Any] | None:
    title = market.get("question", "")
    city = market.get("city") or _city_from_title(title)
    loc = FORECAST_LOCATIONS.get(city or "")
    if not city or not loc:
        return None
    market_date = market.get("date") or _extract_market_date(title)
    if not market_date:
        return None
    bucket_low, bucket_high = _extract_bucket_bounds(title)
    forecast_temp = _get_forecast_temp(city, market_date, loc["unit"])
    if forecast_temp is None:
        return None
    return {
        "city": city,
        "market_date": market_date,
        "bucket_low": bucket_low,
        "bucket_high": bucket_high,
        "forecast_temp": forecast_temp,
        "forecast_gap": _forecast_gap_f(forecast_temp, bucket_low, bucket_high),
    }


def _bucket_type(title: str) -> str:
    lower = title.lower()
    if "or below" in lower:
        return "tail_below"
    if "or higher" in lower or "or above" in lower:
        return "tail_higher"
    return "middle"


def _market_key(strategy: str, market: dict[str, Any]) -> str:
    return f"{strategy}:{market.get('conditionId') or market.get('condition_id') or market.get('id') or market.get('market_id')}"


def _base_candidate(strategy: str, market: dict[str, Any], quote: BookQuote, context: dict[str, Any]) -> dict[str, Any]:
    entry = executable_entry_price(quote)
    return {
        "strategy": strategy,
        "condition_id": market.get("conditionId", ""),
        "market_id": str(market.get("id", "")),
        "polymarket_slug": market.get("slug", ""),
        "title": market.get("question", "")[:180],
        "city": context["city"],
        "side": "BUY",
        "token_id": quote.token_id,
        "entry_price": round(float(entry), 6) if entry is not None else None,
        "exit_bid": round(float(quote.best_bid), 6) if quote.best_bid is not None else None,
        "spread": quote.spread,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
        "bid_depth": quote.bid_depth,
        "ask_depth": quote.ask_depth,
        "bucket_type": _bucket_type(market.get("question", "")),
        "bucket_low": context["bucket_low"],
        "bucket_high": context["bucket_high"],
        "forecast_temp": context["forecast_temp"],
        "forecast_gap": round(float(context["forecast_gap"] or 0), 2),
        "market_date": context["market_date"],
        "market_volume": float(market.get("volume", 0) or 0),
        "price_source": quote.source,
    }


def evaluate_strategy_a(market: dict[str, Any], quote: BookQuote) -> tuple[dict[str, Any] | None, str]:
    context = _forecast_context(market)
    if not context:
        return None, "missing_forecast_context"
    candidate = _base_candidate(STRATEGY_A, market, quote, context)
    if candidate["bucket_type"] != "middle":
        return None, "not_middle_bucket"
    if candidate["city"] not in {"Miami", "Seattle", "Dallas", "Houston"}:
        return None, "city_not_in_strategy_a"
    return _apply_common_filters(candidate, quote, "strategy_a") or (candidate, "candidate")


def evaluate_strategy_b(market: dict[str, Any], quote: BookQuote) -> tuple[dict[str, Any] | None, str]:
    context = _forecast_context(market)
    if not context:
        return None, "missing_forecast_context"
    candidate = _base_candidate(STRATEGY_B, market, quote, context)
    if candidate["bucket_type"] != "tail_below":
        return None, "not_tail_below"
    if candidate["city"] not in {"Miami", "Seattle"}:
        return None, "city_not_in_strategy_b"
    return _apply_common_filters(candidate, quote, "strategy_b") or (candidate, "candidate")


def _apply_common_filters(candidate: dict[str, Any], quote: BookQuote, strategy: str) -> tuple[None, str] | None:
    cfg = DEFAULT_CONFIG
    if quote.error:
        return None, quote.error
    if candidate["entry_price"] is None:
        return None, "missing_best_ask"
    if quote.best_bid is None:
        return None, "missing_best_bid"
    if candidate["entry_price"] > cfg["max_entry"]:
        return None, "entry_too_high"
    if candidate["spread"] is None or candidate["spread"] > cfg["max_spread"]:
        return None, "spread_too_wide"
    if candidate["bid_size"] < cfg["min_bid_size"]:
        return None, "bid_size_too_low"
    if candidate["ask_size"] < cfg["min_ask_size"]:
        return None, "ask_size_too_low"
    if candidate["market_volume"] < cfg["min_volume"]:
        return None, "volume_too_low"
    gap = float(candidate["forecast_gap"] or 0)
    city = candidate["city"]
    if strategy == "strategy_a":
        if city == "Houston":
            if not (4.0 <= gap <= 6.0):
                return None, "gap_not_houston_a"
        elif not (1.0 <= gap <= 3.0):
            return None, "gap_not_middle_a"
    if strategy == "strategy_b":
        if city == "Miami" and not (7.0 <= gap <= 10.0):
            return None, "gap_not_miami_b"
        if city == "Seattle" and not (4.0 <= gap <= 10.0):
            return None, "gap_not_seattle_b"
    return None


def _open_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in state.get("positions", {}).values() if p.get("status") == "dry_run_open"]


def _today_loss(state: dict[str, Any]) -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pnl = float(state.get("daily_realized_pnl", {}).get(today, 0.0) or 0.0)
    return abs(min(0.0, pnl))


def _risk_block_reason(state: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    cfg = state.get("config", DEFAULT_CONFIG)
    if int(state.get("closed_trades", 0) or 0) >= int(cfg["max_closed_trades"]):
        return "max_closed_trades_reached"
    if _today_loss(state) >= float(cfg["daily_max_loss"]):
        return "daily_max_loss_reached"
    if len(_open_positions(state)) >= int(cfg["max_open_positions"]):
        return "max_open_positions_reached"
    if any(
        p.get("strategy") == candidate["strategy"] and p.get("condition_id") == candidate["condition_id"]
        for p in _open_positions(state)
    ):
        return "already_open"
    if float(state.get("spent", 0.0) or 0.0) + float(cfg["stake"]) > float(cfg["bankroll_limit"]):
        return "bankroll_limit_reached"
    return None


def _watchlist_ready(state: dict[str, Any], candidate: dict[str, Any]) -> bool:
    cfg = state.get("config", DEFAULT_CONFIG)
    key = _market_key(candidate["strategy"], candidate)
    watch = state.setdefault("watchlist", {}).setdefault(key, {})
    previous_count = int(watch.get("seen_count", 0) or 0)
    watch.update({
        "seen_count": previous_count + 1,
        "last_seen": _now(),
        "candidate": candidate,
    })
    return watch["seen_count"] >= int(cfg["watch_scans_required"])


def _record_close(state: dict[str, Any], pos: dict[str, Any], pnl: float) -> None:
    state["closed_trades"] = int(state.get("closed_trades", 0) or 0) + 1
    stake = float(pos.get("stake", 0) or 0)
    state["spent"] = round(max(0.0, float(state.get("spent", 0) or 0) - stake), 2)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = state.setdefault("daily_realized_pnl", {})
    daily[today] = round(float(daily.get(today, 0) or 0) + pnl, 2)


def manage_ab_exits(state: dict[str, Any]) -> dict[str, int]:
    stats = {"closed": 0, "stop_losses": 0, "take_profits": 0, "trailing_stops": 0}
    for pos_key, pos in list(state.get("positions", {}).items()):
        if pos.get("status") != "dry_run_open":
            continue
        quote = quote_yes_market({"clobTokenIds": json.dumps([pos.get("token_id", "")])})
        price = executable_exit_price(quote)
        entry = float(pos.get("entry_price", 0) or 0)
        shares = float(pos.get("shares", 0) or 0)
        stake = float(pos.get("stake", 0) or 0)
        pnl = shares * (price - entry)
        pnl_pct = pnl / max(0.01, stake) * 100
        pos.update({
            "current_price": round(price, 6),
            "value": round(shares * price, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "last_price_source": quote.to_dict(),
        })
        if pnl_pct > float(pos.get("peak_pnl_pct", pnl_pct) or pnl_pct):
            pos["peak_pnl_pct"] = round(pnl_pct, 2)

        reason = None
        peak = float(pos.get("peak_pnl_pct", pnl_pct) or pnl_pct)
        if pnl_pct <= -STOP_LOSS_PCT:
            reason = "stop_loss"
            stats["stop_losses"] += 1
        elif pnl_pct >= 300.0:
            reason = "take_profit_3x"
            stats["take_profits"] += 1
        elif peak >= 75.0 and pnl_pct < peak * 0.70:
            reason = "trailing_stop"
            stats["trailing_stops"] += 1

        if reason:
            pos.update({
                "status": "closed",
                "closed_at": _now(),
                "exit_price": round(price, 6),
                "close_reason": reason,
            })
            _record_close(state, pos, pnl)
            stats["closed"] += 1
            log_ab_event("CLOSED", reason, **{**pos, "position_id": pos_key, "realized_pnl": round(pnl, 2)})
    return stats


def run_ab_cycle() -> dict[str, Any]:
    state = load_ab_state()
    trader = PaperTrader()
    execution = PolymarketExecutionAdapter()

    exit_stats = manage_ab_exits(state)
    markets = trader.discover_weather_markets()
    skip_reasons: dict[str, int] = {}
    by_strategy = {
        STRATEGY_A: {"candidates": 0, "watched": 0, "would_buy": 0, "blocked": 0},
        STRATEGY_B: {"candidates": 0, "watched": 0, "would_buy": 0, "blocked": 0},
    }
    sample_candidates: list[dict[str, Any]] = []

    for market in markets:
        quote = quote_yes_market(market)
        for strategy, evaluator in ((STRATEGY_A, evaluate_strategy_a), (STRATEGY_B, evaluate_strategy_b)):
            candidate, reason = evaluator(market, quote)
            if not candidate:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue
            by_strategy[strategy]["candidates"] += 1
            sample_candidates.append(candidate)
            if not _watchlist_ready(state, candidate):
                by_strategy[strategy]["watched"] += 1
                log_ab_event("WATCH", "candidate waiting for consecutive scan", **candidate)
                continue
            block = _risk_block_reason(state, candidate)
            if block:
                by_strategy[strategy]["blocked"] += 1
                log_ab_event("BLOCKED", block, **candidate)
                continue
            stake = float(state["config"]["stake"])
            order = execution.place_limit_buy(
                token_id=candidate["token_id"],
                price=float(candidate["entry_price"]),
                stake=stake,
                metadata=candidate,
            )
            position_id = f"{candidate['strategy']}:{candidate['condition_id']}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            position = {
                **candidate,
                "position_id": position_id,
                "status": "dry_run_open" if order.get("dry_run", True) else "live_open",
                "stake": stake,
                "shares": order["shares"],
                "current_price": candidate["exit_bid"] or 0.0,
                "value": round(order["shares"] * float(candidate["exit_bid"] or 0.0), 2),
                "pnl": round(order["shares"] * (float(candidate["exit_bid"] or 0.0) - float(candidate["entry_price"])), 2),
                "opened_at": _now(),
                "execution": order,
            }
            state["positions"][position_id] = position
            state["spent"] = round(float(state.get("spent", 0) or 0) + stake, 2)
            by_strategy[strategy]["would_buy"] += 1
            log_ab_event("WOULD_BUY", "CLOB-priced candidate passed all gates", **position)

    cycle = {
        "ts": _now(),
        "scanned": len(markets),
        "exits": exit_stats,
        "skip_reasons": skip_reasons,
        "by_strategy": by_strategy,
        "open_positions": len(_open_positions(state)),
        "spent": round(float(state.get("spent", 0) or 0), 2),
        "sample_candidates": sample_candidates[-25:],
        "config": state.get("config", DEFAULT_CONFIG),
    }
    state["last_cycle"] = cycle
    save_ab_state(state)
    log_ab_event("CYCLE_END", "AB CLOB pilot cycle finished", **cycle)
    return cycle


def _format_report(cycle: dict[str, Any]) -> str:
    lines = ["**AB CLOB Pilot**", f"`{cycle['ts'][:19]}Z`", ""]
    lines.append(f"Scanned {cycle['scanned']} markets | Open {cycle['open_positions']} | Spent ${cycle['spent']:.2f}")
    for strategy, stats in cycle.get("by_strategy", {}).items():
        lines.append(
            f"{strategy}: candidates={stats['candidates']} watched={stats['watched']} "
            f"would_buy={stats['would_buy']} blocked={stats['blocked']}"
        )
    skips = cycle.get("skip_reasons", {})
    if skips:
        lines.append("")
        lines.append("Top skips: " + ", ".join(f"{k}={v}" for k, v in sorted(skips.items(), key=lambda x: -x[1])[:6]))
    return "\n".join(lines)


def main() -> int:
    print(_format_report(run_ab_cycle()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
