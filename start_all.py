#!/usr/bin/env python3
"""Start dashboard and fast loop in background."""
import subprocess, sys, time
from pathlib import Path

DIR = Path("/home/aymen/projects/scripts/trading-bot")

dash = subprocess.Popen(
    [sys.executable, "-c", "from dashboard import start_dashboard; start_dashboard()"],
    cwd=DIR,
    stdout=open(DIR / "dashboard.log", "a"),
    stderr=subprocess.STDOUT,
)
print(f"Dashboard started: PID {dash.pid}")

loop = subprocess.Popen(
    [sys.executable, "scripts/fast_loop.py"],
    cwd=DIR,
    stdout=open(DIR / "fast_loop.log", "a"),
    stderr=subprocess.STDOUT,
)
print(f"Fast loop started: PID {loop.pid}")

# Quick health check
time.sleep(2)
import urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:9091/", timeout=5)
    print(f"Dashboard OK: {r.status}")
except Exception as e:
    print(f"Dashboard check: {e}")

print(f"Both running. Logs: {DIR}/dashboard.log, {DIR}/fast_loop.log")
