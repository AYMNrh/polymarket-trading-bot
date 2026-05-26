#!/usr/bin/env python3
"""Dry-run/live guardrail runner for the Strategy 2 real-money pilot.

This module is intentionally conservative: by default it never places orders.
It records what Strategy 2 would have bought using live market asks and applies
the real-money pilot limits before any future order-placement adapter is used.
"""

from __future__ import annotations

import json
import os
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_trader import (
    FORECAST_LOCATIONS,
    STRATEGY2_MAX_ENTRY,
    STRATEGY2_MIN_EV_RATIO,
    STRATEGY2_MIN_FORECAST_GAP_F,
    STRATEGY2_MIN_VOLUME,
    PaperTrader,
    _entry_price_for_side,
    _extract_bucket_bounds,
    _extract_market_date,
    _forecast_gap_f,
    _get_forecast_temp,
    _mark_price_for_side,
)

DATA_DIR = Path(__file__).parent / "data"
LIVE_STATE_FILE = DATA_DIR / "live_strategy2_state.json"
LIVE_ORDERS_LOG = DATA_DIR / "live_strategy2_orders.jsonl"

DEFAULT_LIVE_CONFIG = {
    "enabled": True,
    "dry_run": True,
    "bankroll_limit": 20.0,
    "stake": 1.0,
    "max_open_positions": 10,
    "daily_max_loss": 5.0,
    "max_trades": 20,
}


class LiveTradingDisabled(RuntimeError):
    """Raised when non-dry-run trading is requested without explicit support."""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def load_live_config() -> dict[str, Any]:
    """Load pilot limits from env vars, defaulting to dry-run safe values."""
    return {
        "enabled": _env_bool("LIVE_TRADING_ENABLED", DEFAULT_LIVE_CONFIG["enabled"]),
        "dry_run": _env_bool("LIVE_DRY_RUN", DEFAULT_LIVE_CONFIG["dry_run"]),
        "bankroll_limit": _env_float("LIVE_MAX_BANKROLL", DEFAULT_LIVE_CONFIG["bankroll_limit"]),
        "stake": _env_float("LIVE_STAKE", DEFAULT_LIVE_CONFIG["stake"]),
        "max_open_positions": _env_int("LIVE_MAX_OPEN_POSITIONS", DEFAULT_LIVE_CONFIG["max_open_positions"]),
        "daily_max_loss": _env_float("LIVE_DAILY_MAX_LOSS", DEFAULT_LIVE_CONFIG["daily_max_loss"]),
        "max_trades": _env_int("LIVE_MAX_TRADES", DEFAULT_LIVE_CONFIG["max_trades"]),
    }


def _initial_state(config: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "strategy": "live-strategy-2-tail",
        "created_at": now,
        "updated_at": now,
        "config": config,
        "positions": {},
        "closed_trades": 0,
        "daily_realized_pnl": {},
        "spent": 0.0,
        "last_cycle": {},
    }


def load_live_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_live_config()
    if LIVE_STATE_FILE.exists():
        try:
            state = json.loads(LIVE_STATE_FILE.read_text())
            state["config"] = config
            state.setdefault("positions", {})
            state.setdefault("daily_realized_pnl", {})
            state.setdefault("closed_trades", 0)
            state.setdefault("spent", 0.0)
            return state
        except Exception:
            pass
    return _initial_state(config)


def save_live_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    LIVE_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def log_live_event(event_type: str, message: str, **details: Any) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": "live-strategy-2-tail",
        "event_type": event_type,
        "message": message,
        "details": details,
    }
    with LIVE_ORDERS_LOG.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_live_events(limit: int = 80) -> list[dict[str, Any]]:
    if not LIVE_ORDERS_LOG.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with LIVE_ORDERS_LOG.open() as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return events[-limit:][::-1]


def _city_from_title(title: str) -> str | None:
    title_lower = title.lower()
    for city_name in FORECAST_LOCATIONS:
        if city_name.lower() in title_lower:
            return city_name
    return None


def _forecast_context(title: str, market: dict[str, Any]) -> dict[str, Any] | None:
    city = market.get("city") or _city_from_title(title)
    if not city:
        return None
    loc = FORECAST_LOCATIONS.get(city)
    if not loc:
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


def _open_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in state.get("positions", {}).values() if p.get("status") == "dry_run_open"]


def _today_realized_loss(state: dict[str, Any]) -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pnl = float(state.get("daily_realized_pnl", {}).get(today, 0.0) or 0.0)
    return abs(min(0.0, pnl))


def _risk_block_reason(state: dict[str, Any], config: dict[str, Any], condition_id: str) -> str | None:
    if not config.get("enabled", False):
        return "live_trading_not_enabled"
    if not config.get("dry_run", True):
        raise LiveTradingDisabled("Non-dry-run order placement is not implemented yet.")
    if int(state.get("closed_trades", 0) or 0) >= int(config["max_trades"]):
        return "max_trades_reached"
    if _today_realized_loss(state) >= float(config["daily_max_loss"]):
        return "daily_max_loss_reached"
    open_positions = _open_positions(state)
    if len(open_positions) >= int(config["max_open_positions"]):
        return "max_open_positions_reached"
    if any(p.get("condition_id") == condition_id for p in open_positions):
        return "already_open"
    if float(state.get("spent", 0.0) or 0.0) + float(config["stake"]) > float(config["bankroll_limit"]):
        return "bankroll_limit_reached"
    return None


def _candidate_from_market(trader: PaperTrader, market: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    title = market.get("question", "")
    title_lower = title.lower()
    if "or higher" not in title_lower and "or below" not in title_lower:
        return None, "not_tail_bucket"

    price = _entry_price_for_side(market, "BUY")
    if price is None:
        return None, "missing_best_ask"
    if price > STRATEGY2_MAX_ENTRY:
        return None, "entry_price_too_high"

    volume = float(market.get("volume", 0) or 0)
    if volume < STRATEGY2_MIN_VOLUME:
        return None, "volume_too_low"

    context = _forecast_context(title, market)
    if not context:
        return None, "missing_forecast_context"
    if context["forecast_gap"] is None or context["forecast_gap"] < STRATEGY2_MIN_FORECAST_GAP_F:
        return None, "forecast_gap_too_small"

    fair_price = trader._estimate_fair_price(
        title,
        context["market_date"],
        context["forecast_temp"],
        context["bucket_low"],
        context["bucket_high"],
    )
    if fair_price is None or fair_price <= 0 or fair_price >= 1:
        return None, "bad_fair_price"

    ev_ratio = fair_price / price
    if ev_ratio < STRATEGY2_MIN_EV_RATIO:
        return None, "ev_ratio_too_low"

    condition_id = market.get("conditionId", "")
    candidate = {
        "condition_id": condition_id,
        "market_id": str(market.get("id", "")),
        "polymarket_slug": market.get("slug", ""),
        "title": title[:140],
        "city": context["city"],
        "side": "BUY",
        "entry_price": round(price, 4),
        "current_price": round(price, 4),
        "fair_price": round(fair_price, 4),
        "ev_ratio": round(ev_ratio, 2),
        "forecast_temp": context["forecast_temp"],
        "forecast_gap": round(float(context["forecast_gap"]), 2),
        "bucket_low": context["bucket_low"],
        "bucket_high": context["bucket_high"],
        "market_date": context["market_date"],
        "market_volume": volume,
    }
    return candidate, "candidate"


TRAILING_STOP_PCT = 30.0       # 30% trail from peak (matches S2 paper strategy)
MFE_ACTIVATE_PCT = 50.0         # trailing activates after +50% MFE


def _update_position_price(pos: dict) -> float | None:
    """Fetch current executable exit price from Gamma API. Returns price or None."""
    lookup_id = pos.get("market_id") or pos.get("condition_id")
    if not lookup_id:
        return None
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/markets/{lookup_id}",
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return _mark_price_for_side(data, pos.get("side", "BUY"))
    except Exception:
        return None


def _is_market_resolved(market_id: str) -> tuple[bool, float | None]:
    """Check if a market is closed/resolved via Gamma API.
    Returns (is_resolved, settlement_price)."""
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=8,
        )
        if r.status_code != 200:
            return False, None
        data = r.json()
        if not data.get("closed", False):
            return False, None
        prices_str = data.get("outcomePrices", "[0.5,0.5]")
        try:
            prices = json.loads(prices_str)
            return True, float(prices[0])
        except (json.JSONDecodeError, IndexError, TypeError):
            return False, None
    except Exception:
        return False, None


def _manage_exits(state: dict[str, Any]) -> dict[str, Any]:
    """Update prices, apply trailing stops, and resolve closed markets.
    Returns stats about what happened this cycle."""
    closed_count = 0
    resolved_count = 0
    trailing_count = 0

    for pos_key, pos in list(state.get("positions", {}).items()):
        if pos.get("status") != "dry_run_open":
            continue

        entry_price = float(pos.get("entry_price", 1))
        shares = float(pos.get("shares", 0))
        stake = float(pos.get("stake", 0))

        # --- Check market resolution first ---
        market_id = pos.get("market_id", "")
        if market_id:
            is_resolved, settle_price = _is_market_resolved(market_id)
            if is_resolved and settle_price is not None:
                pos["current_price"] = settle_price
                value_r = shares * settle_price
                pnl_r = value_r - shares * entry_price
                pos["value"] = round(value_r, 2)
                pos["pnl"] = round(pnl_r, 2)
                pos["pnl_pct"] = round((pnl_r / max(0.01, stake)) * 100, 2)
                pos["exit_price"] = settle_price
                pos["close_reason"] = "resolved"
                pos["status"] = "closed"
                pos["closed_at"] = datetime.now(timezone.utc).isoformat()
                _record_close(state, pos, pnl_r)
                resolved_count += 1
                log_live_event("CLOSED", "market resolved", position_id=pos_key,
                               pnl=round(pnl_r, 2), reason="resolved",
                               market_id=market_id, title=pos.get("title", "")[:60])
                continue

        # --- Fetch executable exit price for still-open markets ---
        price = _update_position_price(pos)
        if price is None:
            continue

        # Update position metrics
        pos["current_price"] = price
        value = shares * price
        pnl = value - shares * entry_price
        pnl_pct = (pnl / max(0.01, stake)) * 100
        pos["value"] = round(value, 2)
        pos["pnl"] = round(pnl, 2)
        pos["pnl_pct"] = round(pnl_pct, 2)

        # Track peak for trailing stop
        peak = pos.get("peak_pnl_pct")
        if peak is None or pnl_pct > peak:
            pos["peak_pnl_pct"] = pnl_pct

        # MFE tracking
        mfe = pos.get("mfe_price")
        if mfe is None or price > mfe:
            pos["mfe_price"] = price

        # --- Check trailing stop (activates at +50% MFE) ---
        mfe_price = pos.get("mfe_price", entry_price)
        mfe_mfe_pct = ((mfe_price - entry_price) / max(0.0001, entry_price)) * 100
        hit_mfe_protect = mfe_mfe_pct >= MFE_ACTIVATE_PCT

        if hit_mfe_protect:
            peak_pnl = pos.get("peak_pnl_pct", pnl_pct)
            if peak_pnl > 0 and pnl_pct < peak_pnl * (1 - TRAILING_STOP_PCT / 100):
                pos["exit_price"] = price
                pos["close_reason"] = "trailing_stop"
                pos["status"] = "closed"
                pos["closed_at"] = datetime.now(timezone.utc).isoformat()
                _record_close(state, pos, pnl)
                trailing_count += 1
                log_live_event("CLOSED", "trailing stop hit", position_id=pos_key,
                               pnl=round(pnl, 2), peak_pnl=round(peak_pnl, 1),
                               reason="trailing_stop", market_id=market_id)

    return {
        "closed": closed_count + trailing_count + resolved_count,
        "trailing_stops": trailing_count,
        "resolved": resolved_count,
    }


def _record_close(state: dict[str, Any], pos: dict, pnl: float) -> None:
    """Update state counters when a position closes."""
    state["closed_trades"] = (state.get("closed_trades", 0) or 0) + 1
    stake = float(pos.get("stake", 0))

    # Reclaim spent capital
    current_spent = float(state.get("spent", 0.0) or 0.0)
    state["spent"] = round(max(0.0, current_spent - stake), 2)

    # Track daily realized PnL
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = state.setdefault("daily_realized_pnl", {})
    daily[today] = round(float(daily.get(today, 0.0) or 0.0) + pnl, 2)


def run_live_strategy2_cycle() -> dict[str, Any]:
    """Scan Strategy 2 candidates and record dry-run would-buy entries."""
    config = load_live_config()
    state = load_live_state(config)
    trader = PaperTrader()

    if not config.get("dry_run", True):
        raise LiveTradingDisabled("Non-dry-run order placement is not implemented yet.")

    # --- Manage exits first: update prices, apply trailing stops, resolve ---
    exit_stats = _manage_exits(state)

    markets = trader.discover_weather_markets()
    scanned = 0
    candidates = 0
    would_buy = 0
    blocked = 0
    skip_reasons: dict[str, int] = {}
    scan_by_city: dict[str, dict[str, Any]] = {}

    for market in markets:
        scanned += 1
        city_key = market.get("city") or "Unknown"
        city_stats = scan_by_city.setdefault(
            city_key,
            {"scanned": 0, "candidates": 0, "would_buy": 0, "blocked": 0, "skips": {}},
        )
        city_stats["scanned"] += 1
        candidate, reason = _candidate_from_market(trader, market)
        if not candidate:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            city_stats["skips"][reason] = city_stats["skips"].get(reason, 0) + 1
            continue
        candidates += 1
        city_stats["candidates"] += 1

        block_reason = _risk_block_reason(state, config, candidate["condition_id"])
        if block_reason:
            blocked += 1
            city_stats["blocked"] += 1
            log_live_event("BLOCKED", block_reason, **candidate)
            continue

        stake = round(float(config["stake"]), 2)
        position_id = f"{candidate['condition_id']}-BUY-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        shares = round(stake / max(0.0001, float(candidate["entry_price"])), 2)
        position = {
            **candidate,
            "position_id": position_id,
            "status": "dry_run_open",
            "stake": stake,
            "shares": shares,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": True,
        }
        state["positions"][position_id] = position
        state["spent"] = round(float(state.get("spent", 0.0) or 0.0) + stake, 2)
        would_buy += 1
        city_stats["would_buy"] += 1
        log_live_event("WOULD_BUY", "dry-run order passed all pilot gates", **position)

    cycle = {
        "scanned": scanned,
        "candidates": candidates,
        "would_buy": would_buy,
        "blocked": blocked,
        "open_positions": len(_open_positions(state)),
        "spent": round(float(state.get("spent", 0.0) or 0.0), 2),
        "exits": exit_stats,
        "skip_reasons": skip_reasons,
        "scan_by_city": scan_by_city,
        "config": config,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    state["last_cycle"] = cycle
    save_live_state(state)
    log_live_event("CYCLE_END", "live Strategy 2 dry-run cycle finished", **cycle)
    return cycle


def _reload_state_from_disk() -> dict[str, Any]:
    """Reload full state from disk (after cycle modifies it in-memory)."""
    config = load_live_config()
    return load_live_state(config)


def _format_report(cycle: dict[str, Any], state: dict[str, Any]) -> str:
    """Build a readable report from cycle data and current state."""
    lines = []
    lines.append("**Live S2 Tail Dry Run**")
    lines.append(f"`{cycle['ts'][:19]}Z`")
    lines.append("")

    # --- Cycle summary ---
    lines.append("**Cycle**")
    lines.append(f"  Scanned: {cycle['scanned']} | Candidates: {cycle['candidates']}")
    lines.append(f"  Would buy: {cycle['would_buy']} | Blocked: {cycle['blocked']}")
    lines.append(f"  Open: {cycle['open_positions']} | Spent: ${cycle['spent']:.2f} / $20.00")

    exits = cycle.get("exits", {})
    if exits.get("closed", 0):
        lines.append(f"  Exits: {exits['closed']} ({exits.get('resolved',0)} resolved, {exits.get('trailing_stops',0)} trailing)")

    # --- Limits ---
    closed = state.get("closed_trades", 0)
    max_trades = cycle["config"].get("max_trades", 20)
    daily_pnl = state.get("daily_realized_pnl", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_loss = abs(min(0, float(daily_pnl.get(today, 0))))
    daily_max = cycle["config"].get("daily_max_loss", 5.0)
    lines.append(f"  Trades: {closed}/{max_trades} | Daily loss: ${today_loss:.2f} / $5.00")

    # --- Current open positions ---
    open_positions = _open_positions(state)
    if open_positions:
        lines.append("")
        lines.append(f"**Open Positions ({len(open_positions)})**")
        for p in sorted(open_positions, key=lambda x: float(x.get("pnl", 0) or 0)):
            city = p.get("city", "?")
            pnl = float(p.get("pnl", 0) or 0)
            peak = p.get("peak_pnl_pct")
            peak_str = f" peak={peak:.0f}%" if peak is not None else ""
            title = p.get("title", "?")
            date_str = p.get("market_date", "?")
            lines.append(f"  {city} | ${pnl:+.2f}{peak_str} | {title[:55]}")

    # --- Skip reasons ---
    skips = cycle.get("skip_reasons", {})
    if skips:
        top = sorted(skips.items(), key=lambda x: -x[1])[:5]
        lines.append("")
        lines.append(f"**Skips** ({sum(skips.values())} total)")
        for reason, count in top:
            lines.append(f"  {reason}: {count}")

    # --- Recent events from log ---
    events = load_live_events(limit=10)
    closes = [e for e in events if e.get("event_type") == "CLOSED"]
    if closes:
        lines.append("")
        lines.append(f"**Recent Closes ({len(closes)})**")
        for e in closes[:5]:
            details = e.get("details", {})
            pnl = float(details.get("pnl", 0) or 0)
            reason = details.get("reason", "?")
            title = details.get("title", e.get("message", ""))[:45]
            lines.append(f"  ${pnl:+.2f} {reason} | {title}")

    return "\n".join(lines)


def main() -> int:
    cycle = run_live_strategy2_cycle()
    state = _reload_state_from_disk()
    print(_format_report(cycle, state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
