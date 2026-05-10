#!/usr/bin/env python3
"""Watchdog: check paper trading bot health every 3h."""
import json, os, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT = Path(__file__).parent
STATE_FILE = PROJECT / "data" / "paper_portfolio.json"
TRADES_LOG = PROJECT / "data" / "paper_trades.jsonl"
MAX_STALE_HOURS = 2

issues = []

# 1. Check state file freshness (did the hourly cron run?)
if STATE_FILE.exists():
    mtime = datetime.fromtimestamp(os.path.getmtime(STATE_FILE), tz=timezone.utc)
    age = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
    if age > MAX_STALE_HOURS:
        issues.append(f"State file stale: {age:.1f}h since last update (threshold: {MAX_STALE_HOURS}h)")
else:
    issues.append("State file missing — bot may have never run")

# 2. Check dashboard on port 9091
try:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    result = s.connect_ex(('localhost', 9091))
    s.close()
    if result != 0:
        issues.append("Dashboard NOT listening on port 9091")
except Exception:
    issues.append("Dashboard port check failed")

# 3. Check for recent trades
if TRADES_LOG.exists():
    mtime = datetime.fromtimestamp(os.path.getmtime(TRADES_LOG), tz=timezone.utc)
    age = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
else:
    issues.append("No trade log found")

# Report
if issues:
    print(f"⚠️  Bot health check — {len(issues)} issue(s):")
    for i in issues:
        print(f"  • {i}")
else:
    # Silent - everything OK
    pass
