#!/usr/bin/env python3
"""Fast loop: calls btc_eth_15m_bot.run_once() every 5 seconds.
Keeps same state file, same schema — dashboard just works."""
import sys, time
sys.path.insert(0, "/home/aymen/projects/scripts/trading-bot")
from btc_eth_15m_bot import run_once, load_state
from datetime import datetime, timezone

start = time.time()
error_count = 0

while True:
    try:
        state = run_once()
        error_count = 0
        bk = state["bankroll"]
        eq = state["equity"]
        pnl = state["total_pnl"]
        sig = state.get("last_signal", {})
        if sig.get("executed"):
            s = f"BUY {sig['winner']} @ ${sig['buy_price']} ({sig['profit_pct']}%)"
        else:
            s = str(sig.get("reason", "waiting"))[:45]

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        uptime = int(time.time() - start)
        open_t = sum(1 for t in state.get("trades",[]) if t.get("status")=="open")
        print(f"[{ts}] BK:${bk:.2f} EQ:${eq:.2f} PnL:${pnl:.2f} Open:{open_t} | {s}  [{uptime}s]", flush=True)

    except Exception as e:
        error_count += 1
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] ERROR: {e}", flush=True)
        if error_count > 5:
            print("Too many errors, sleeping 30s", flush=True)
            time.sleep(30)
            error_count = 0

    time.sleep(5)
