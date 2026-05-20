import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

trades = Path('data/paper_trades.jsonl')
lines = trades.read_text().strip().split('\n')

cutoff = datetime.now() - timedelta(hours=2)
recent = []
for line in lines[-200:]:
    if line:
        t = json.loads(line)
        t_time = t.get('time', '')
        if t_time and 'T' in t_time:
            try:
                dt = datetime.fromisoformat(t_time)
                if dt > cutoff:
                    recent.append(t)
            except:
                recent.append(t)
        else:
            recent.append(t)

# Most active markets in last 2h
markets = Counter()
for r in recent:
    title = str(r.get('title', ''))[:30]
    markets[title] += 1

print('Most active markets (last 2h):')
for m, count in markets.most_common(5):
    print(f'  {m:30s}: {count} trades')

# Biggest winning/losing trades
winners = [c for c in recent if c.get('pnl',0) > 0]
losers = [c for c in recent if c.get('pnl',0) < 0]
if winners:
    best = max(winners, key=lambda x: x.get('pnl',0))
    print(f'Best trade: +${best.get("pnl",0):.2f} - {str(best.get("title",""))[:30]}')
if losers:
    worst = min(losers, key=lambda x: x.get('pnl',0))
    print(f'Worst trade: ${worst.get("pnl",0):.2f} - {str(worst.get("title",""))[:30]}')

# Total volume
total_vol = sum(abs(r.get('size', 0)) for r in recent)
print(f'Total volume: ${total_vol:.0f}')
