#!/usr/bin/env python3
"""Snapshot paper trading state every 20min for trend analysis."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).parent
SNAPSHOT_DIR = PROJECT / "data" / "snapshots"
PORTFOLIO_FILE = PROJECT / "data" / "paper_portfolio.json"

os.makedirs(SNAPSHOT_DIR, exist_ok=True)

if not PORTFOLIO_FILE.exists():
    print("No portfolio file found")
    sys.exit(0)

with open(PORTFOLIO_FILE) as f:
    p = json.load(f)

now = datetime.now(timezone.utc)

open_positions = {k: v for k, v in p.get("positions", {}).items() if v.get("status") == "open"}
closed_positions = {k: v for k, v in p.get("positions", {}).items() if v.get("status") == "closed"}
total_closed_pnl = sum(v.get("pnl", 0) for v in closed_positions.values())
open_value = sum(v.get("value", 0) for v in open_positions.values())
bankroll = p.get("bankroll", 0)
starting = p.get("starting_bankroll", 100)
current_pnl = bankroll - starting

snapshot = {
    "ts": now.isoformat(),
    "ts_unix": now.timestamp(),
    "bankroll": round(bankroll, 2),
    "starting_bankroll": starting,
    "current_pnl": round(current_pnl, 2),
    "closed_pnl": round(total_closed_pnl, 2),
    "open_exposure": round(open_value, 2),
    "total_value": round(bankroll + open_value, 2),
    "num_open": len(open_positions),
    "num_closed": len(closed_positions),
    "total_trades": p.get("total_trades", len(closed_positions)),
    "wins": p.get("wins", 0),
    "losses": p.get("losses", 0),
    "parameters": {k: v for k, v in p.get("parameters", {}).items()},
    "open_positions": [
        {
            "title": v.get("title", "?")[:60],
            "city": v.get("city", "?"),
            "side": v.get("side", "?"),
            "entry_price": v.get("entry_price", 0),
            "current_price": v.get("current_price", 0),
            "shares": v.get("shares", 0),
            "value": round(v.get("value", 0), 2),
            "pnl": round(v.get("pnl", 0), 2),
            "pnl_pct": round(v.get("pnl_pct", 0), 1),
            "ev": v.get("ev", 0),
            "fair_price": v.get("fair_price", 0),
            "entry_ts": v.get("entry_ts", ""),
        }
        for v in sorted(open_positions.values(), key=lambda x: abs(x.get("pnl_pct", 0)), reverse=True)
    ],
}

# Append to daily log
daily_log = SNAPSHOT_DIR / f"trades_{now.strftime('%Y-%m-%d')}.jsonl"
with open(daily_log, "a") as f:
    f.write(json.dumps(snapshot) + "\n")

# Also maintain a compact summary log
summary_log = SNAPSHOT_DIR / "summary.csv"
if not summary_log.exists():
    with open(summary_log, "w") as f:
        f.write("ts,bankroll,pnl,closed_pnl,exposure,total_value,open,closed,wins,losses\n")
with open(summary_log, "a") as f:
    f.write(f"{snapshot['ts']},{snapshot['bankroll']},{snapshot['current_pnl']},{snapshot['closed_pnl']},"
            f"{snapshot['open_exposure']},{snapshot['total_value']},{snapshot['num_open']},"
            f"{snapshot['num_closed']},{snapshot['wins']},{snapshot['losses']}\n")

print(f"Snapshot saved to {daily_log} ({snapshot['num_open']} open, {snapshot['num_closed']} closed, "
      f"pnl=${snapshot['current_pnl']:.2f})")
