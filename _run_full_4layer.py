#!/usr/bin/env python3
import json
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

from orchestrator import WhaleOrchestrator
from pathlib import Path

orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()

if not markets:
    print('No weather markets found.')
    exit(0)

# LAYER 1
print('=' * 60)
print('LAYER 1 - Macro Signal Scanning (Gamma Markets)')
print('=' * 60)

orch.strategy.load_whale_activity([])
signals = orch.strategy.scan_all_markets(markets)

print(f"Total Gamma markets scanned: {len(markets)}")
print(f"Actionable signals generated: {len(signals)}")

if signals:
    sigs_sorted = sorted(signals, key=lambda s: abs(s.get("expected_value", 0)), reverse=True)
    print()
    for s in sigs_sorted[:8]:
        print(f"  {s.get('city','?'):20s} EV:{s.get('expected_value',0):+.4f}  conf:{s.get('confidence',0)*100:.0f}%  dir:{s.get('direction','?')}  entry:${s.get('entry_price',0):.4f}")

# LAYER 2
print()
print('=' * 60)
print('LAYER 2 - Order Book Microstructure and Wall Detection')
print('=' * 60)

orch._cache_order_books()
cache = orch._order_book_cache

print(f"Order books cached: {len(cache)}")

wall_markets = []
for item in cache:
    ws = item.get("wall_score", 0) or 0
    sp = item.get("spread", 0) or 0
    depth = item.get("depth_imbalance", 0) or 0
    token = item.get("token_id", "?")[:10]
    city = item.get("city", "?")
    if abs(ws) > 0.3 or abs(depth) > 0.3:
        wall_markets.append((city, token, vs, sp, depth))

wall_markets.sort(key=lambda x: abs(x[2]), reverse=True)
print(f"Markets with significant walls: {len(wall_markets)}")
print()
for city, token, ws, sp, depth in wall_markets[15]:
    side = "BUY" if vs > 0 else "SELL"
    print(f"  {city:20s} wall:{ws:+.2f} ({side})  spread:{sp:.4f}  depth:{depth:+.2f}  token:{token}")

# LAYER 3
print()
print('=' * 60)
print('LAYER 3 - Portfolio Construction (EV-weighted allocation)')
print('=' * 60)

portfolio = orch.strategy.build_portfolio(signals, capital=1000)
print(f"Portfolio positions built: {len(portfolio)}")

if portfolio:
    print()
    total_cap = sum(abs(p.get("size", 0) * p.get("entry_price", 0)) for p in portfolio)
    print(f"Total capital deployed: ${total_cap:.2f}")
    print()
    print(f"  {City:20s} {Dir:4s} {Entry:>8s} {EV:>7s} {Conf:>4s} {Wall:>5s} {Spread:>7s}")
    print(f"  {'-'*20} {'-'*4} {'-'*8} {'-'*7} {'-'*4} {'-'*5} {'-'*7}")
    for p in portfolio[:10]:
        ob = p.get("order_book", {}) or {}
        ws = ob.get("wall_score", 0)
        sp = ob.get("spread", 0)
        print(f"   p['city']:20s} p['direction']:4s} ${p['entry_price']:.4f}  {p['expected_value']:+.4f}  {p['confidence']*100:.0f}%  {ws:+.2f}  {sp:.4f}")

# LAYER 4
print()
print('=' * 60)
print('LAYER 4 - Self-Learning Feedback and Execution Readiness')
print('=' * 60)

learner = orch.learner
if learner:
    metrics = getattr(learner, 'get_metrics', None)
    if metrics:
        m = metrics()
        print(f"Self-learning stats: {json.dumps(m, default=str)[:200]}")
    else:
        print(f"Learner type: {type(learner).__name__}")

print()
print('--- Execution Readiness ---')
strat = orch.strategy
for attr in ['min_confidence', 'min_ev', 'max_spread', 'min_wall_score', 'max_capital_per_position', 'slippage_bps']:
    val = getattr(strat, attr, 'N/A')
    print(f"  {attr}: {val}")

print()
print('--- Position Tracker ---')
print(orch.tracker.position_tracker.summary())

print()
print('--- Whale Watch State ---')
try:
    ww_path = Path("whale_watch.json")
    if ww_path.exists():
        ww = json.loads(ww_path.read_text())
        if isinstance(ww, list):
            print(f"Whale watch entries: {len(ww)}")
            if ww:
                for w in ww[-3]:
                    print(f"  {w.get('name','?'):20s} type:{w.get('type','?')}  confidence:{w.get('confidence',0)}")
        elif isinstance(ww, dict):
            print(f"Whale watch keys: {list(ww.keys())[:10]}")
except Exception as e:
    print(f"  Could not load: {e}")
