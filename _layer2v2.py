#!/usr/bin/env python3
"""Layer 2: Order Book Microstructure (with prior scan to populate cache)"""
import json, logging
logging.basicConfig(level=logging.INFO)
from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()
print(f"Total markets: {len(markets)}")
if not markets: exit(0)

# Must scan first to populate strategy._order_book_cache
orch.strategy.load_whale_activity([])
signals = orch.strategy.scan_all_markets(markets)
print(f"Signals generated: {len(signals)}")

# Now cache order books from the strategy data
orch._cache_order_books()
cache = orch._order_book_cache
print(f"Order books cached: {len(cache)}")
if cache:
    ws_list = [item.get("wall_score",0) or 0 for item in cache]
    sp_list = [item.get("spread",0) or 0 for item in cache]
    dp_list = [item.get("depth_imbalance",0) or 0 for item in cache]
    ci_list = [item.get("city","?") for item in cache]
    tok_list = [item.get("token_id","?") for item in cache]

    sigwalls = []
    for i in range(len(cache)):
        if abs(ws_list[i]) > 0.3 or abs(dp_list[i]) > 0.3:
            sigwalls.append((ci_list[i], tok_list[i][:8], ws_list[i], sp_list[i], dp_list[i]))

    sigwalls.sort(key=lambda x: abs(x[2]), reverse=True)
    print(f"Significant walls: {len(sigwalls)}")
    for city,tok,ws,sp,dp in sigwalls[:15]:
        side = "BUY" if ws > 0 else "SELL"
        print(f"  {city:20s} wall:{ws:+7.4f} ({side}) spread:{sp:.4f} depth:{dp:+.2f}")
    nz = [s for s in ws_list if abs(s) > 0]
    print(f"Non-zero wall scores: {len(nz)}")
    if nz:
        avg = sum(nz) / len(nz)
        mx = max(nz)
        mn = min(nz)
        print(f"Avg: {avg:+7.4f}  Max: {mx:+7.4f}  Min: {mn:+7.4f}")
    tight = [s for s in sp_list if 0 < s < 0.05]
    print(f"Tight spreads (<5%): {len(tight)} of {len(sp_list)}")
    all_spreads = [s for s in sp_list if s > 0]
    if all_spreads:
        avg_sp = sum(all_spreads) / len(all_spreads)
        print(f"Avg spread: {avg_sp:.4f}")
else:
    print("No order books cached. Checking strategy cache directly...")
    sc = orch.strategy._order_book_cache
    print(f"Strategy _order_book_cache keys: {len(sc)}")
    if sc:
        first_key = list(sc.keys())[0]
        print(f"First key: {first_key}")
        print(f"First val: {json.dumps(str(sc[first_key]))[:200]}")
