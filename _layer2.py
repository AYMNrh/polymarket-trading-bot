#!/usr/bin/env python3
"""Layer 2: Order Book Microstructure"""
import json, logging
logging.basicConfig(level=logging.INFO)
from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()
print(f"Total markets: {len(markets)}")
orch._cache_order_books()
cache = orch._order_book_cache
print(f"Cached {len(cache)} order books")
if cache:
    print(f"First city: {cache[0].get(chr(99)+chr(105)+chr(116)+chr(121), chr(63))}")
    ws_list = [item.get("wall_score",0) or 0 for item in cache]
    sp_list = [item.get("spread",0) or 0 for item in cache]
    dp_list = [item.get("depth_imbalance",0) or 0 for item in cache]

    sigwalls = []
    for i,item in enumerate(cache):
        if abs(ws_list[i]) > 0.3 or abs(dp_list[i]) > 0.3:
            sigwalls.append((item.get("city","?"), item.get("token_id","?")[:8], ws_list[i], sp_list[i], dp_list[i]))

    sigwalls.sort(key=lambda x: abs(x[2]), reverse=True)
    print(f"Markets with significant walls: {len(sigwalls)}")
    for city,tok,ws,sp,dp in sigwalls[:15]:
        side = "BUY" if ws > 0 else "SELL"
        print(f"  {city:20s} wall:{ws:+.2f} ({side}) spread:{sp:.4f} depth:{dp:+.2f}")
    nz = [s for s in ws_list if abs(s) > 0]
    print(f"Non-zero wall scores: {len(nz)}")
    if nz:
        print(f"Avg: {sum(nz)/len(nz):+.4f}  Max: {max(nz):+.4f}  Min: {min(nz):+.4f}")
    tight = [s for s in sp_list if 0 < s < 0.05]
    print(f"Tight spreads (<5%): {len(tight)} of {len(sp_list)}")
else:
    print("No order books cached.")
