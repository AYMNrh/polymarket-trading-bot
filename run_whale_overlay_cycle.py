#!/usr/bin/env python3
"""EV + Whale overlay paper trading cycle — cron job entry point."""
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from config import load_config
from polymarket_clob import PolymarketClobClient
from polymarket_scraper import PolymarketScraper
from paper_trader import PaperTrader, _save_state


def main():
    cfg = load_config()
    wallets = cfg.get("watched_wallets", [])
    scraper = PolymarketScraper()
    clob = PolymarketClobClient()
    trader = PaperTrader(bankroll=100.0)

    # 1. Whale overlay: aggregate positions from priority whales
    priority_labels = {"ColdMath", "Sharky6999", "RN1"}
    priority_whales = [w for w in wallets if w.get("label") in priority_labels]
    all_whale_positions = []
    for w in priority_whales:
        try:
            pos = scraper.get_positions(w["address"])
            if pos:
                all_whale_positions.extend(pos)
                logger.info("  %s: %d positions", w["label"], len(pos))
        except Exception as e:
            logger.warning("  %s: skipped (%s)", w["label"], e)

    logger.info("  Total whale positions for overlay: %d", len(all_whale_positions))

    # 2. Weather markets from Gamma API
    markets = clob.get_gamma_markets(tag="weather", limit=100)
    logger.info("  Weather markets found: %d", len(markets))

    # 3. Evaluate & trade
    trades_opened = 0
    for m in markets:
        try:
            result = trader.evaluate_and_trade(m, whale_positions=all_whale_positions)
            if result:
                trades_opened += 1
        except Exception as e:
            logger.debug("Skipping market %s: %s", m.get("id", "?"), e)

    # 4. Resolve closed positions and persist state
    trader.update_prices()
    resolved = trader.resolve_positions()
    if resolved:
        logger.info("Resolved %d positions", resolved)
    _save_state(trader.state)

    # 5. Report
    summary = trader.summary()
    print(f"\nCycle complete: {trades_opened} trades opened, "
          f"{summary['open_positions']} open, "
          f"${summary['total_pnl']:.2f} PnL")
    print()
    print(trader.get_portfolio_report())


if __name__ == "__main__":
    main()
