#!/usr/bin/env python3
"""
Polymarket Whale Trading Platform — run.py
"""
import argparse
import logging
import os
import signal
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("whale-platform")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def cmd_finder(args):
    from whale_finder import WhaleFinder
    f = WhaleFinder()
    print(f"🐋 Scanning for new whales (last 500k blocks)...")
    r = f.scan_recent_activity(lookback_blocks=500000)
    print(f"\nFound {r['total_candidates']} candidate wallets")
    if r:
        print(f"\n{'Score':>5} {'Volume':>10} {'Trades':>6}  Wallet")
        print(f"{'-'*5} {'-'*10} {'-'*6}  {'-'*42}")
        for w in r[:15]:
            print(f"{w['score']:>5} ${w['volume']:>8.0f} {w['trades']:>6}  {w['address'][:10]}...")
    print(f"\nUse --finder-add to auto-add top candidates")


def cmd_finder_add(args):
    from whale_finder import WhaleFinder
    f = WhaleFinder()
    r = f.scan_recent_activity(lookback_blocks=500000)
    added = f.auto_add_whales(r, max_add=args.max_add or 5)
    if added:
        print(f"✅ Added {added} new whales to watchlist")
    else:
        print("No new whales to add")


def cmd_scan(args):
    from orchestrator import WhaleOrchestrator
    WhaleOrchestrator().run_scan_cycle()
    print("Scan complete")


def cmd_monitor(args):
    from orchestrator import WhaleOrchestrator
    trades = WhaleOrchestrator().run_monitor_cycle()
    print(f"Monitored, {len(trades)} new trades")


def cmd_strategy(args):
    from database import get_recent_trades
    from orchestrator import WhaleOrchestrator
    orch = WhaleOrchestrator()
    trades = get_recent_trades(100)
    orch.strategy.load_whale_activity(trades)
    portfolio = orch.run_strategy_cycle()
    if portfolio:
        print(f"Strategy: {len(portfolio)} positions recommended")


def cmd_dashboard(args):
    from dashboard import start_dashboard
    port = args.port or 9091
    print(f"Dashboard: http://0.0.0.0:{port}")
    start_dashboard(port=port)


def cmd_full_cycle(args):
    from orchestrator import WhaleOrchestrator
    WhaleOrchestrator().full_cycle()


def cmd_daemon(args):
    from orchestrator import WhaleOrchestrator
    orch = WhaleOrchestrator()
    orch.run_dashboard(port=args.dashboard_port or 9091)
    logger.info("Daemon starting")
    running = True
    signal.signal(signal.SIGINT, lambda *a: exit())
    while running:
        try:
            orch.run_monitor_cycle()
            time.sleep(args.monitor_interval or 120)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("Cycle error: %s", e)
            time.sleep(60)


def cmd_summary(args):
    from database import get_stats
    s = get_stats()
    print(f"Whales: {s['total_whales']}  Trades: {s['total_trades']}  Volume: ${s['total_volume']:,.2f}  Signals: {s['total_signals']}")


def main():
    p = argparse.ArgumentParser(description="🐋 Whale Platform")
    p.add_argument("cmd", nargs="?", help="finder | finder-add | scan | monitor | strategy | dashboard | full-cycle | daemon | summary")
    p.add_argument("--port", type=int, default=9091)
    p.add_argument("--dashboard-port", type=int, default=9091)
    p.add_argument("--max-add", type=int, default=5)
    p.add_argument("--monitor-interval", type=int, default=120)
    args = p.parse_args()
    
    cmds = {
        "finder": cmd_finder,
        "finder-add": cmd_finder_add,
        "scan": cmd_scan,
        "monitor": cmd_monitor,
        "strategy": cmd_strategy,
        "dashboard": cmd_dashboard,
        "full-cycle": cmd_full_cycle,
        "daemon": cmd_daemon,
        "summary": cmd_summary,
    }
    fn = cmds.get(args.cmd)
    if fn:
        fn(args)
    else:
        print("Commands: finder | finder-add | scan | monitor | strategy | dashboard | full-cycle | daemon | summary")
        print("\n  finder       — scan for new whales")
        print("  finder-add   — scan + auto-add to watchlist")
        print("  monitor      — check known whales for new trades")
        print("  strategy     — analyze weather markets")
        print("  dashboard    — web UI on port 9091")


if __name__ == "__main__":
    main()
