#!/usr/bin/env python3
import json
from pathlib import Path
from datetime import datetime

BASE = Path('/home/aymen/projects/scripts/trading-bot')

# 1. Paper portfolio
state = {}
pf = BASE / 'data/paper_portfolio.json'
if pf.exists():
    state = json.loads(pf.read_text())

summary = {
    'bankroll': state.get('bankroll', 0),
    'open_positions': len([p for p in state.get('positions', {}).values() if p.get('status') == 'open']),
    'closed': len([p for p in state.get('positions', {}).values() if p.get('status') == 'closed']),
    'wins': state.get('wins', 0),
    'losses': state.get('losses', 0),
    'exposure': state.get('exposure', 0),
    'total_pnl': sum(p.get('pnl', 0) for p in state.get('positions', {}).values() if p.get('status') == 'closed'),
}

# 2. Last action from trades log
last_action = None
trades_log = BASE / 'data/paper_trades.jsonl'
if trades_log.exists():
    lines = trades_log.read_text().strip().split('\n')
    for l in reversed(lines):
        if l.strip():
            try:
                last = json.loads(l)
                last_action = {
                    'action': last.get('action', '?'),
                    'title': str(last.get('title', ''))[:45],
                    'pnl': last.get('pnl', 0),
                    'time': last.get('time', ''),
                }
                break
            except:
                continue

# Recent OPEN trades since last heartbeat
recent_opens = []
if trades_log.exists():
    for l in lines[-20:]:
        if l.strip():
            try:
                t = json.loads(l)
                if t.get('action') == 'OPEN':
                    recent_opens.append({
                        'title': str(t.get('title', ''))[:40],
                        'size': t.get('size', 0),
                        'price': t.get('price', 0),
                    })
            except:
                continue

# 3. Whale data
whale_summary = {}
whale_file = BASE / 'data/whale_portfolios.json'
if whale_file.exists():
    wf = json.loads(whale_file.read_text())
    active = {k: v for k, v in wf.items() if v.get('profile', {}).get('trades', 0) > 0}
    for name, info in list(active.items())[:3]:
        p = info.get('profile', {})
        pos = info.get('positions', {})
        top_token = ''
        top_val = 0
        if isinstance(pos, dict) and pos:
            top = max(pos.items(), key=lambda x: abs(x[1].get('alloc_usd', 0) if isinstance(x[1], dict) else 0))
            top_token = top[0]
            top_val = top[1].get('alloc_usd', 0) if isinstance(top[1], dict) else 0
        elif isinstance(pos, list) and pos:
            top_token = str(pos[0]) if isinstance(pos[0], str) else pos[0].get('token', '')
            top_val = 0
        whale_summary[name] = {
            'trades': p.get('trades', 0),
            'positions_count': len(pos),
            'top_token': top_token,
            'top_val': top_val,
        }

# 4. Decoder logs
decoder_logs = []
decoder_dir = BASE / 'data/whale_decoder_logs'
if decoder_dir.exists() and decoder_dir.is_dir():
    logs = sorted(decoder_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
    for log in logs[:3]:
        decoder_logs.append({
            'name': log.name,
            'modified': datetime.fromtimestamp(log.stat().st_mtime).strftime('%m-%d %H:%M'),
        })

# 5. Check whale_positions.json too
whale_positions = {}
wp_file = BASE / 'data/whale_positions.json'
if wp_file.exists():
    try:
        whale_positions = json.loads(wp_file.read_text())
    except:
        pass

result = {
    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    'portfolio': summary,
    'last_action': last_action,
    'recent_opens': recent_opens[-3:],
    'active_whales': len(whale_summary),
    'whale_details': whale_summary,
    'decoder_logs': decoder_logs,
    'whale_positions_keys': list(whale_positions.keys())[:5],
}

print(json.dumps(result, default=str))
