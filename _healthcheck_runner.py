#!/usr/bin/env python3
"""Comprehensive health check for Polymarket weather trading bot."""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).parent
DATA = BASE / "data"

# ── Load portfolio ──
with open(DATA / "paper_portfolio.json") as f:
    pf = json.load(f)

bankroll = pf["bankroll"]
start = pf.get("starting_bankroll", 100.0)
total_pnl = bankroll - start

# Count trades and wins
positions = pf.get("positions", {})
closed = [p for p in positions.values() if p.get("status") == "closed"]
open_pos = [p for p in positions.values() if p.get("status") == "open"]

wins = sum(1 for p in closed if p.get("resolved_outcome") == "win")
losses = sum(1 for p in closed if p.get("resolved_outcome") == "loss")
total_resolved = wins + losses
win_rate = round((wins / total_resolved * 100) if total_resolved > 0 else 0, 1)

# Open position values (current market value)
open_value = sum(p.get("value", 0) for p in open_pos)
equity = round(bankroll + open_value, 2)

# Exposure = sum of reserved_capital for OPEN positions
exposure = sum(p.get("reserved_capital", 0) for p in open_pos)

# Cities in open positions
open_cities = list(set(p.get("city", "?") for p in open_pos))

print(f"bankroll={bankroll}")
print(f"starting_bankroll={start}")
print(f"total_pnl={round(total_pnl, 2)}")
print(f"win_rate={win_rate}")
print(f"open_positions={len(open_pos)}")
print(f"closed_positions={len(closed)}")
print(f"wins={wins}")
print(f"losses={losses}")
print(f"total_resolved={total_resolved}")
print(f"open_value={round(open_value, 2)}")
print(f"equity={equity}")
print(f"exposure={round(exposure, 2)}")
print(f"open_cities={','.join(sorted(set(open_cities)))}")

# ── Load state ──
with open(DATA / "state.json") as f:
    state = json.load(f)

peak_equity = state.get("peak_equity", equity)
print(f"peak_equity={peak_equity}")
print(f"equity={equity}")

if equity > peak_equity:
    print("PEAK_UPDATED")
    drawdown_pct = 0.0
else:
    drawdown_pct = round((1 - equity / peak_equity) * 100, 1)
    print(f"drawdown_pct={drawdown_pct}")

# Stall detection
last_cycle_str = state.get("last_cycle", "")
last_cycle = datetime.fromisoformat(last_cycle_str) if last_cycle_str else datetime.now(timezone.utc)
hours_since_last_cycle = (datetime.now(timezone.utc) - last_cycle).total_seconds() / 3600
print(f"hours_since_last_cycle={round(hours_since_last_cycle, 1)}")
print(f"open_count={len(open_pos)}")
print(f"exposure_small={exposure < 5}")
print(f"bankroll_ge_90={bankroll >= 90}")

# Last report bankroll for performance check
last_report_br = state.get("last_report_bankroll", 0)
print(f"last_report_bankroll={last_report_br}")
print(f"bankroll_moved={abs(bankroll - last_report_br) > 0.01}")

# Open positions for orphan check
for k, p in positions.items():
    if p.get("status") == "open":
        print(f"OPENPOS|{p.get('value', 0)}|{p.get('entry_ts', '')}|{p.get('reserved_capital', 0)}")
