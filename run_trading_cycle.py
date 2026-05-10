import logging, json, sys
logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

from polymarket_scraper import PolymarketScraper
from paper_trader import PaperTrader
from config import load_config

cfg = load_config()
scraper = PolymarketScraper()
trader = PaperTrader(bankroll=100.0)

# 1. Discover weather markets via event slugs (proven approach)
markets = trader.discover_weather_markets()
print(f'Markets: {len(markets)}')

# 2. Get whale overlay
wallets = cfg.get('watched_wallets', [])
all_pos = []
for w in wallets:
    if w.get('label') in ('ColdMath', 'Sharky6999', 'RN1'):
        try:
            p = scraper.get_positions(w['address'])
            if p: all_pos.extend(p)
        except:
            pass

print(f'Whale positions: {len(all_pos)}')

# 3. Evaluate and trade
opened = 0
for m in markets:
    try:
        r = trader.evaluate_and_trade(m, whale_positions=all_pos)
        if r:
            opened += 1
    except:
        pass

s = trader.summary()
print(f'Opened: {opened}, Open: {s["open_positions"]}, PnL: ${s["total_pnl"]:.2f}, Exp: ${s["exposure"]:.2f}')
