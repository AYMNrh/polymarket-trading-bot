#!/usr/bin/env python3
"""Shared runner for the two production-candidate paper strategies."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import load_config
from paper_trader import (
    STRATEGY1_MODE,
    STRATEGY2_MODE,
    STRATEGY3_MODE,
    PaperTrader,
    _log_runtime_event,
    _save_state,
)
from polymarket_scraper import PolymarketScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _load_quality_whale_positions(scraper: PolymarketScraper) -> list[dict]:
    """Load quality-weighted watched wallet positions for signal annotation."""
    cfg = load_config()
    positions: list[dict] = []
    seen: set[str] = set()
    for wallet in cfg.get("watched_wallets", []):
        address = wallet.get("address")
        if not address or address in seen or not wallet.get("weight", False):
            continue
        seen.add(address)
        try:
            for pos in scraper.get_positions(address) or []:
                if isinstance(pos, dict):
                    pos["wallet_address"] = address
                    pos["wallet_label"] = wallet.get("label", address[:10])
                    positions.append(pos)
        except Exception as exc:
            _log_runtime_event("shared", "WHALE_LOAD_ERROR", str(exc), address=address)
    return positions


def _load_cached_tail_whales(data_dir: Path) -> list[dict]:
    """Tail strategy can use the larger cached whale portfolio file for context."""
    path = data_dir / "whale_portfolios.json"
    if not path.exists():
        return []
    try:
        whale_data = json.loads(path.read_text())
    except Exception as exc:
        _log_runtime_event("tail-cache", "WHALE_CACHE_ERROR", str(exc), path=str(path))
        return []

    positions: list[dict] = []
    for wallet_addr, wallet_data in whale_data.items():
        if not isinstance(wallet_data, dict):
            continue
        for pos in wallet_data.get("positions", wallet_data.get("trades", [])) or []:
            if isinstance(pos, dict):
                pos["wallet_address"] = wallet_addr
                positions.append(pos)
    return positions


def run_strategy(mode: str) -> dict:
    if mode not in {STRATEGY1_MODE, STRATEGY2_MODE, STRATEGY3_MODE}:
        raise ValueError(f"Unsupported strategy mode: {mode}")

    scraper = PolymarketScraper()
    trader = PaperTrader(mode=mode)
    _log_runtime_event(mode, "CYCLE_START", "cycle started", equity=trader.equity())

    markets = trader.discover_weather_markets()
    if mode == STRATEGY1_MODE:
        whale_positions = _load_quality_whale_positions(scraper)
    else:
        whale_positions = _load_cached_tail_whales(trader.state_file.parent)

    opened = 0
    errors = 0
    for market in markets:
        try:
            result = trader.evaluate_and_trade(market, whale_positions=whale_positions)
            if result and result.get("status") == "open" and result.get("entry_ts", "").startswith(
                datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ):
                opened += 1
        except Exception as exc:
            errors += 1
            _log_runtime_event(mode, "EVALUATION_ERROR", str(exc), market=market.get("question", "?"))

    trader.update_prices()
    closed = trader.apply_risk_stops()
    resolved = trader.resolve_positions()
    _save_state(trader.state, trader.state_file)

    summary = trader.summary()
    cycle = {
        "mode": mode,
        "markets": len(markets),
        "opened": opened,
        "closed": len(closed),
        "resolved": resolved,
        "errors": errors,
        "open_positions": summary.get("open_positions", 0),
        "bankroll": summary.get("bankroll", 0),
        "equity": trader.equity(),
        "total_pnl": summary.get("total_pnl", 0),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    trader.state["last_strategy_cycle"] = cycle
    _save_state(trader.state, trader.state_file)
    _log_runtime_event(mode, "CYCLE_END", "cycle finished", **cycle)
    return cycle


def run_position_monitor(mode: str) -> dict:
    """Monitor open positions without scanning for new entries."""
    if mode not in {STRATEGY1_MODE, STRATEGY2_MODE, STRATEGY3_MODE}:
        raise ValueError(f"Unsupported strategy mode: {mode}")

    trader = PaperTrader(mode=mode)
    _log_runtime_event(mode, "MONITOR_START", "position monitor started", equity=trader.equity())
    trader.update_prices()
    closed = trader.apply_risk_stops()
    resolved = trader.resolve_positions()
    _save_state(trader.state, trader.state_file)

    summary = trader.summary()
    cycle = {
        "mode": mode,
        "closed": len(closed),
        "resolved": resolved,
        "open_positions": summary.get("open_positions", 0),
        "bankroll": summary.get("bankroll", 0),
        "equity": trader.equity(),
        "total_pnl": summary.get("total_pnl", 0),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    trader.state["last_monitor_cycle"] = cycle
    _save_state(trader.state, trader.state_file)
    _log_runtime_event(mode, "MONITOR_END", "position monitor finished", **cycle)
    return cycle


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "monitor":
        mode = argv[2] if len(argv) > 2 else STRATEGY1_MODE
        cycle = run_position_monitor(mode)
    else:
        mode = argv[1] if len(argv) > 1 else STRATEGY1_MODE
        cycle = run_strategy(mode)
    print(json.dumps(cycle, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
