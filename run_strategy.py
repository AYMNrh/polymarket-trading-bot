#!/usr/bin/env python3
"""Full 4-layer strategy analysis — cron run."""
import json, logging
logging.basicConfig(level=logging.INFO)

from orchestrator import WhaleOrchestrator

orch = WhaleOrchestrator()

# LAYER 1+3: Strategy with Gamma API order book signals
markets = orch.clob.get_weather_markets()
if markets:
    orch.strategy.load_whale_activity([])
    signals = orch.strategy.scan_all_markets(markets)
    portfolio = orch.strategy.build_portfolio(signals, capital=1000)
    orch._cache_order_books()
    
    print(f'Scanned {len(markets)} Gamma markets.')
    print(f'Found {len(signals)} actionable signals.')
    print(f'Portfolio: {len(portfolio)} positions.')
    
    if portfolio:
        print()
        for p in portfolio[:10]:
            ob = p.get('order_book', {}) or {}
            ws = ob.get('wall_score', 0)
            sp = ob.get('spread', 0)
            print(f'  {p["city"]:20s} {p["direction"]:4s} @ ${p["entry_price"]:.4f}  '
                  f'EV:{p["expected_value"]:+.4f}  conf:{p["confidence"]*100:.0f}%  '
                  f'wall:{ws:+.2f}  spread:{sp:.4f}')
    
    # Conviction summary
    print()
    print(orch.tracker.position_tracker.summary())
else:
    print('No weather markets found.')
