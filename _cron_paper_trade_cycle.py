#!/usr/bin/env python3
"""EV + whale overlay paper trading cycle — cron run with state persistence."""
import sys
sys.path.insert(0, '/home/aymen/projects/scripts/trading-bot')

import logging, json
logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

from polymarket_scraper import PolymarketScraper
from paper_trader import PaperTrader, _save_state
from config import load_config

cfg = load_config()
scraper = PolymarketScraper()
trader = PaperTrader(bankroll=100.0)

print(f"Starting bankroll: ${trader.state['bankroll']:.2f}")
print(f"Open positions before cycle: {len([p for p in trader.state.get('positions',{}).values() if p.get('status')=='open'])}")

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

# 4. Resolve closed positions
trader.update_prices()
resolved = trader.resolve_positions()
if resolved:
    print(f'Resolved: {resolved} positions')

# 5. Save state to disk so it persists
_save_state(trader.state)

# 7. Also update exposure in state
trader.state['exposure'] = sum(p.get('value', 0) for p in trader.state.get('positions',{}).values() if p.get('status') == 'open')
trader.state['last_sync'] = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
_save_state(trader.state)

s = trader.summary()
print(f'Opened: {opened}, Open: {s["open_positions"]}, PnL: ${s["total_pnl"]:.2f}, Exp: ${s["exposure"]:.2f}')
print(f'Bankroll remaining: ${s["bankroll"]:.2f}')
print()

# Print open positions
for p in trader.get_open_positions()[:10]:
    print(f'  {p["side"]} {p["title"][:40]:40s} ${p["value"]:.2f} @ {p["entry_price"]*100:.1f}¢ PnL:${p["pnl"]:+.2f} EV:{p["ev"]:+.2f}')
