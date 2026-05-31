#!/usr/bin/env python3
"""Fast loop: runs both 5m BTC and 15m BTC/ETH bots every 5 seconds."""
import sys, time
sys.path.insert(0, "/home/aymen/projects/scripts/trading-bot")
from btc_5m_bot import run_once as run_5m
from btc_eth_15m_bot import run_once as run_15m
from datetime import datetime, timezone

start = time.time()
errors_5m = 0
errors_15m = 0

while True:
    now = datetime.now(timezone.utc)
    ts = now.strftime("%H:%M:%S")
    uptime = int(time.time() - start)

    try:
        s5 = run_5m()
        errors_5m = 0
        open_t5 = [t for t in s5.get("trades", []) if t.get("status") == "open"]
        if open_t5:
            t = open_t5[0]
            line5 = f"5m: {t['winner']} @ ${t['buy_price']:.2f} ({t['profit_pct']:.0f}%)"
        else:
            sig5 = s5.get("last_signal", {})
            if sig5.get("executed"):
                line5 = f"5m: BUY {sig5['winner']} @ ${sig5['buy_price']} ({sig5['profit_pct']}%)"
            else:
                reason5 = str(sig5.get("reason", "waiting"))[:40]
                line5 = f"5m: {reason5}"

        bk5 = s5.get("bankroll", 100)
    except Exception as e:
        errors_5m += 1
        line5 = f"5m: ERROR ({e})"
        bk5 = 0
        if errors_5m > 5:
            time.sleep(30)
            errors_5m = 0

    try:
        s15 = run_15m()
        errors_15m = 0
        sig15 = s15.get("last_signal", {})
        if sig15.get("executed"):
            line15 = f"15m: BUY {sig15['winner']} @ ${sig15['buy_price']} ({sig15['profit_pct']}%)"
        else:
            line15 = f"15m: {str(sig15.get('reason', 'waiting'))[:40]}"
        bk15 = s15.get("bankroll", 100)
    except Exception as e:
        errors_15m += 1
        line15 = f"15m: ERROR ({e})"
        bk15 = 0
        if errors_15m > 5:
            time.sleep(30)
            errors_15m = 0

    pnl = s5.get("total_pnl", 0) + s15.get("total_pnl", 0)
    bk = bk5 + bk15
    print(f"[{ts}] BK:${bk:.2f} PnL:${pnl:.2f} | {line5} | {line15}  [{uptime}s]", flush=True)
    time.sleep(5)
