#!/usr/bin/env python3
"""EV + whale overlay paper trading cycle cron entrypoint."""
import logging
import os
import sys

PROJECT = os.path.dirname(os.path.abspath(__file__))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

from config import load_config
from paper_trader import PaperTrader, _save_state
from polymarket_scraper import PolymarketScraper


def load_whale_positions(scraper: PolymarketScraper, cfg: dict) -> tuple[list[dict], int]:
    """Load overlay positions from configured watched wallets."""
    wallets = cfg.get("watched_wallets", [])
    allowed_labels = set(cfg.get("overlay_wallet_labels", []))
    seen_addresses = set()
    all_positions = []
    used_wallets = 0

    for wallet in wallets:
        address = wallet.get("address")
        label = wallet.get("label", "")
        if not address or address in seen_addresses:
            continue
        if allowed_labels and label not in allowed_labels:
            continue
        seen_addresses.add(address)
        used_wallets += 1
        try:
            positions = scraper.get_positions(address)
            if positions:
                all_positions.extend(positions)
        except Exception:
            pass
    return all_positions, used_wallets


cfg = load_config()
scraper = PolymarketScraper()
trader = PaperTrader(bankroll=100.0)

# 1. Discover curated US weather markets
markets = trader.discover_weather_markets()
print(f"Discovered US weather markets: {len(markets)}")

# 2. Load whale overlay inputs
all_pos, whale_wallets_used = load_whale_positions(scraper, cfg)
print(f"Whale wallets used: {whale_wallets_used}")
print(f"Whale positions loaded: {len(all_pos)}")
cycle_report = trader.build_cycle_report(markets, all_pos)
print(
    "Candidates:"
    f" liquid={cycle_report['liquid_candidates']}"
    f" ev={cycle_report['ev_candidates']}"
    f" tradable={cycle_report['tradable_candidates']}"
    f" whale={cycle_report['markets_with_whale_signal']}"
)

# 3. Evaluate and trade
opened = 0
errors = 0
for market in markets:
    try:
        result = trader.evaluate_and_trade(market, whale_positions=all_pos)
        if result and result.get("status") == "open":
            opened += 1
    except Exception:
        errors += 1

# 4. Mark-to-market and resolve closed positions
trader.update_prices()
stopped = trader.apply_risk_stops()
if stopped:
    print(f"Risk stops: {len(stopped)} positions")
resolved = trader.resolve_positions()
if resolved:
    print(f"Resolved: {resolved} positions")

# 5. Self-learning: daily review only, no intraday parameter application
if resolved and trader._daily_review_due():
    from self_learning import SelfLearningEngine

    learner = SelfLearningEngine()
    learning_data = trader.export_for_learning()
    if learning_data:
        observations = learner.review_resolved_trades(learning_data)
        if observations:
            print(f"Daily review: {len(observations)} insights generated")
            for obs in observations[:5]:
                print(f"  {obs[:80]}")
        trader.mark_learning_review_complete()

summary = trader.summary()
print(
    f"Opened: {opened}, Stops: {len(stopped)}, Open: {summary['open_positions']}, "
    f"PnL: ${summary['total_pnl']:.2f}, Exp: ${summary['exposure']:.2f}, Errors: {errors}"
)

# 6. Persist state
_save_state(trader.state)
