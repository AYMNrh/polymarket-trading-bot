#!/usr/bin/env python3
"""Layer 2 (Order Book Microstructure) + Layer 4 (Execution) analysis."""
from orchestrator import WhaleOrchestrator
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()

# Layer 2: Direct order book analysis - check raw wall scores
orch._cache_order_books()
books = orch.order_books
print(f'Cached {len(books)} order books.')

if books:
    wall_markets = []
    for city, ob in books.items():
        ws = ob.get('wall_score', 0)
        sp = ob.get('spread', 0)
        depth = ob.get('depth_imbalance', 0)
        if abs(ws) > 0.3 or abs(depth) > 0.3:
            wall_markets.append((city, ws, sp, depth))
    
    wall_markets.sort(key=lambda x: abs(x[1]), reverse=True)
    print(f'Markets with significant walls: {len(wall_markets)}')
    print()
    for city, ws, sp, depth in wall_markets[:15]:
        side = 'BUY' if ws > 0 else 'SELL'
        print(f'  {city:20s} wall:{ws:+.2f} ({side}) spread:{sp:.4f} depth_imb:{depth:+.2f}')
    print()
    
    # Layer 4: Check pending executions
    em = getattr(orch, 'execution_manager', None)
    if em:
        execs = em.get_pending_executions()
        print(f'Pending executions: {len(execs)}')
        if execs:
            for e in execs[:5]:
                print(f'  {e}')
    else:
        print('No execution manager found.')
else:
    print('No order books cached.')

# Also dump raw signal thresholds
print()
print('=== Strategy Thresholds ===')
strat = orch.strategy
for attr in ['min_confidence', 'min_ev', 'max_spread', 'min_wall_score']:
    val = getattr(strat, attr, 'N/A')
    print(f'  {attr}: {val}')
