#!/usr/bin/env python3
"""Tail Experiment Performance Report — Telegram delivery (fixed)."""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from telegram_alerts import send_telegram
from collections import Counter

DATA = os.path.join(os.path.dirname(__file__), "data")

# Load portfolio
with open(os.path.join(DATA, "tail_experiment_portfolio.json")) as f:
    pf = json.load(f)

# === 1. Current State ===
bankroll = pf["bankroll"]
starting = pf["starting_bankroll"]
peak_equity = pf.get("peak_equity", 0)

open_positions = {k: v for k, v in pf["positions"].items() if v["status"] == "open"}
closed_positions = {k: v for k, v in pf["positions"].items() if v["status"] == "closed"}

open_value = sum(p.get("value", 0) for p in open_positions.values())
equity = bankroll + open_value
open_count = len(open_positions)

# === 2. Trade Count ===
total_trades = pf["total_trades"]
profitable_wins = pf.get("wins_real", 0)
flat_exits = pf.get("wins_flat", 0)
losses = pf.get("losses", 0)
closed_with_outcome = profitable_wins + flat_exits + losses
win_rate = (profitable_wins / closed_with_outcome * 100) if closed_with_outcome > 0 else 0

# === 3. PnL ===
# All positions in portfolio: compute realized from closed, unrealized from open
realized_pnl = sum(p.get("pnl", 0) for p in closed_positions.values())
unrealized_pnl = sum(p.get("pnl", 0) for p in open_positions.values())
total_pnl = realized_pnl + unrealized_pnl
total_return_pct = (equity - starting) / starting * 100

# === 4. Best/Worst Trade ===
# Use ALL positions in portfolio (open + closed) for best/worst
all_positions = list(pf["positions"].values())
best = max(all_positions, key=lambda p: p.get("pnl_pct", 0))
worst = min(all_positions, key=lambda p: p.get("pnl_pct", 0))

# === 5. Exit Reason Breakdown ===
exit_reasons = Counter()
for p in closed_positions.values():
    r = p.get("close_reason", "unknown")
    exit_reasons[r] += 1

# === 6. Entry Price Distribution ===
entry_dist = Counter()
for p in pf["positions"].values():
    entry_dist[p["entry_price"]] += 1

# === 7. Partial Exit Stats ===
# Count positions that had tick TP1 or TP2 hits
# In this system, safety_sell = tick_tp_1 (first target)
# Main sell = tick_tp_2 (second target)
partial_tp1 = sum(1 for p in pf["positions"].values() if p.get("safety_sold"))
partial_tp2 = sum(1 for p in pf["positions"].values() if p.get("main_sold"))
total_with_partial = partial_tp1  # count positions with at least TP1

# Also check exits for tick_tp_1 / tick_tp_2 reasons specifically
tick_tp1_from_exits = 0
tick_tp2_from_exits = 0
for p in pf["positions"].values():
    for ex in p.get("exits", []):
        if ex.get("reason") == "tick_tp_1" or ex.get("reason") == "safety_sell":
            tick_tp1_from_exits += 1
            break
    for ex in p.get("exits", []):
        if ex.get("reason") == "tick_tp_2":
            tick_tp2_from_exits += 1
            break

# === 8. Open Positions Detail ===
open_lines = []
for pos_id, pos in open_positions.items():
    city = pos.get("city", "?")
    entry = pos["entry_price"]
    current = pos["current_price"]
    pnl_d = pos["pnl"]
    pnl_pct = pos["pnl_pct"]
    peak = pos.get("peak_pnl_pct", 0)
    safety = "🛡️" if pos.get("safety_sold") else ""
    title = pos.get("title", "?")
    if len(title) > 50:
        title = title[:47] + "..."
    open_lines.append(
        f"• {city} — {title}\n"
        f"  Entry: ${entry:.3f} → ${current:.4f}\n"
        f"  PnL: ${pnl_d:.2f} ({pnl_pct:+.1f}%) | Peak MFE: {peak:+.1f}% {safety}"
    )
open_str = "\n\n".join(open_lines) if open_lines else "None"

# === Build Telegram Message ===
entry_lines = []
for price in [0.001, 0.002, 0.003]:
    entry_lines.append(f"  ${price:.3f}: {entry_dist.get(price, 0)} trades")
entry_str = "\n".join(entry_lines)

exit_emoji = {
    "trailing_stop": "🔴", "edge_exhausted": "🟡", "tick_tp_1": "🔵",
    "tick_tp_2": "🟣", "take_profit": "🟢", "stop_loss": "⛔",
    "safety_sell": "🔵",
}
exit_order = ["tick_tp_1", "tick_tp_2", "trailing_stop", "edge_exhausted", "take_profit", "stop_loss"]
exit_lines = []
for r in exit_order:
    count = exit_reasons.get(r, 0)
    emoji = exit_emoji.get(r, "➖")
    if count > 0:
        exit_lines.append(f"  {emoji} {r}: {count}")
if not exit_lines:
    exit_lines.append("  No closed positions with exit reasons")
exit_str = "\n".join(exit_lines)

# Shorten best/worst titles
best_title = best.get("title", "?")
if len(best_title) > 45:
    best_title = best_title[:42] + "..."
worst_title = worst.get("title", "?")
if len(worst_title) > 45:
    worst_title = worst_title[:42] + "..."

msg = f"""🐋 *Tail Experiment — V4 Report*

*📊 Portfolio*
• Bankroll: ${bankroll:.1f}
• Equity: ${equity:.1f}
• Starting: ${starting:.1f}
• Peak Equity: ${peak_equity:.1f}
• Open Positions: {open_count}

*📈 Trade Breakdown*
• Total: {total_trades}
• Closed: {closed_with_outcome} ({profitable_wins}W / {flat_exits}F / {losses}L)
• Win Rate: {win_rate:.0f}%

*💰 PnL*
• Realized: ${realized_pnl:.2f}
• Unrealized: ${unrealized_pnl:.2f}
• Total: ${total_pnl:.2f}
• Return: {total_return_pct:+.2f}%

*📉 Performance*
• Best: {best_title} — {best['pnl_pct']:+.1f}%
• Worst: {worst_title} — {worst['pnl_pct']:+.1f}%

*🚪 Exit Reasons*
{exit_str}

*💰 Entry Prices*
{entry_str}

*🎯 Partial Exits*
• Safety sell (tick TP1) hit: {partial_tp1} positions
• Main sell (tick TP2) hit: {partial_tp2} positions

*📋 Open Positions*
{open_str}

*📌 Milestone*
• {total_trades} / 100 trades"""

print(msg)
print("\n--- SENDING TO TELEGRAM ---")
result = send_telegram(msg)
print(f"Telegram send: {'OK' if result else 'FAILED'}")
