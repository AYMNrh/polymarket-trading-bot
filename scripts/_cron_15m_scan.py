"""Cron runner for btc_eth_15m_bot. Runs every minute, outputs state summary."""
import sys
sys.path.insert(0, "/home/aymen/projects/scripts/trading-bot")
from btc_eth_15m_bot import run_once
from datetime import datetime, timezone

state = run_once()

open_trades = [t for t in state.get("trades", []) if t.get("status") == "open"]
closed_trades = [t for t in state.get("trades", []) if t.get("status") == "closed"]

lines = []
lines.append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Bankroll: ${state['bankroll']:.2f} | Equity: ${state['equity']:.2f} | PnL: ${state['total_pnl']:.2f}")
if state.get("last_error"):
    lines.append(f"  Error: {state['last_error']}")
sig = state.get("last_signal", {})
if sig:
    if sig.get("executed"):
        lines.append(f"  BUY {sig['winner']} @ ${sig['buy_price']} ({sig['profit_pct']}%) - {sig['seconds_left']}s left")
    else:
        lines.append(f"  Waiting: {sig.get('reason','?')}")
if open_trades:
    for t in open_trades:
        lines.append(f"  OPEN: {t['winner']} @ ${t['buy_price']} -> ${t['expected_profit']} profit")
if closed_trades:
    for t in closed_trades[-2:]:
        lines.append(f"  CLOSED: {t['winner']} @ ${t['buy_price']} -> ${t['actual_pnl']}")

print("\n".join(lines))
