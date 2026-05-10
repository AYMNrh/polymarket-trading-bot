#!/usr/bin/env python3
import json
from pathlib import Path

data_dir = Path(__file__).parent / 'data'
wf_path = data_dir / 'whale_portfolios.json'

if not wf_path.exists():
    print("No whale data")
    exit(0)

wf = json.loads(wf_path.read_text())
whales = []
for addr, v in wf.items():
    p = v.get('profile', {})
    whales.append({
        'name': p.get('name', addr[:8]),
        'trades': p.get('trades', 0),
        'vol': p.get('total_volume', 0),
        'label': p.get('label', ''),
    })

whales.sort(key=lambda x: x['trades'], reverse=True)
active = [w for w in whales if w['trades'] > 0]

print(f"whale_count={len(wf)} active={len(active)}")
top_trader = active[0] if active else None
if top_trader:
    print(f"top={top_trader['name']}|{top_trader['trades']}trades|${top_trader['vol']:,.0f}vol")
