#!/usr/bin/env python3
import json
from pathlib import Path
from datetime import datetime

BASE = Path('data')

# Read paper portfolio
state = {}
pf = BASE / 'paper_portfolio.json'
if pf.exists():
    state = json.loads(pf.read_text())

summary = {
    'bankroll': state.get('bankroll', 0),
    'open_positions': len([p for p in state.get('positions', {}).values() if p.get('status') == 'open']),
    'closed': len([p for p in state.get('positions', {}).values() if p.get('status') == 'closed']),
    'wins': state.get('wins', 0),
    'losses': state.get('losses', 0),
    'exposure': state.get('exposure', 0),
}

# Check last action from trades log
last_action = 'none yet'
trades_log = BASE / 'paper_trades.jsonl'
if trades_log.exists():
    lines = trades_log.read_text().strip().split('\n')
    if lines and lines[-1]:
        try:
            last = json.loads(lines[-1])
            last_action = f"{last.get('action','?')}: {str(last.get('title',''))[:40]} ${last.get('pnl',0):.2f}"
        except:
            pass

# Check whale positions
whale_stats = ''
whale_convictions = ''
whale_file = BASE / 'whale_positions.json'
if whale_file.exists():
    wf = json.loads(whale_file.read_text())
    active = [k for k, v in wf.items() if isinstance(v, dict) and v.get('profile', {}).get('trades', 0) > 0]
    whale_stats = f'{len(active)} whales tracked'
    convictions = []
    for addr, data in wf.items():
        if not isinstance(data, dict):
            continue
        conv = data.get('conviction', {})
        if conv and conv.get('signal'):
            convictions.append(f"{data.get('label','?')}: {conv.get('signal')}")
    if convictions:
        whale_convictions = ' | '.join(convictions[:2])

pnl = summary.get('bankroll', 100) - 100
win_rate = summary['wins'] / max(1, summary['wins'] + summary['losses']) * 100

msg = (
    f'🤖 Paper Bot ALIVE'
    f' | {datetime.now().strftime("%H:%M")}'
    f' | {summary["open_positions"]} open'
    f' | {"+" if pnl >= 0 else ""}${pnl:.2f} PnL'
    f' | {summary["closed"]} closed ({win_rate:.0f}% WR)'
)

if summary['open_positions'] > 0:
    msg += f' | ${summary["exposure"]:.1f} exposure'

if last_action != 'none yet':
    msg += f' | Last: {last_action}'

if whale_stats:
    msg += f' | 🐋 {whale_stats}'
if whale_convictions:
    msg += f' | Conviction: {whale_convictions}'

print(msg)
