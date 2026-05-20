#!/usr/bin/env python3
"""Tail Experiment Performance Report — compute and deliver to Telegram."""
import json, sys, os
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(__file__))
from telegram_alerts import send_telegram

DATA = os.path.join(os.path.dirname(__file__), "data")

# Load portfolio
with open(os.path.join(DATA, "tail_experiment_portfolio.json")) as f:
    pf = json.load(f)

# Load trades log
with open(os.path.join(DATA, "tail_experiment_trades.jsonl")) as f:
    trades = [json.loads(line) for line in f if line.strip()]

# --- 1. Current State ---
bankroll = pf["bankroll"]
starting = pf["starting_bankroll"]
peak_equity = pf.get("peak_equity", 0)

open_positions = {k: v for k, v in pf["positions"].items() if v.get("status") == "open"}
closed_positions = {k: v for k, v in pf["positions"].items() if v.get("status") == "closed"}

open_value = sum(p.get("value", 0) for p in open_positions.values())
equity = bankroll + open_value
open_count = len(open_positions)

# --- 2. Trade Count ---
total_trades = pf["total_trades"]
profitable_wins = pf.get("wins_real", 0)
flat_exits = pf.get("wins_flat", 0)
losses = pf.get("losses", 0)
closed_trades = profitable_wins + flat_exits + losses
win_rate = (profitable_wins / closed_trades * 100) if closed_trades > 0 else 0

# --- 3. PnL ---
# Group opens/closes by condition_id
open_actions = [t for t in trades if t["action"] == "OPEN"]
close_actions = [t for t in trades if t["action"] == "CLOSE"]
partial_actions = [t for t in trades if t["action"] == "PARTIAL_CLOSE"]

open_entries = defaultdict(list)
close_entries = defaultdict(list)
for t in open_actions:
    open_entries[t["condition_id"]].append(t)
for t in close_actions:
    close_entries[t["condition_id"]].append(t)

# Compute realized PnL from all matched closes
total_realized = 0.0
for cid, opens in open_entries.items():
    closes = close_entries.get(cid, [])
    for i, op in enumerate(opens):
        cost = op["entry_price"] * op.get("original_shares", op["shares"])
        if i < len(closes):
            cl = closes[i]
            settlement = cl.get("settlement_value", cost)
            total_realized += settlement - cost

# Add partial exit PnLs
for t in partial_actions:
    pe = t.get("partial_exit", {})
    total_realized += pe.get("pnl", 0)

unrealized_pnl = sum(p.get("pnl", 0) for p in open_positions.values())
total_pnl = total_realized + unrealized_pnl
total_return_pct = (equity - starting) / starting * 100

# --- 4. Best/Worst Trade ---
# Build full trade list from matched pairs (using pnl_pct for comparison)
all_trades_list = []
for cid, opens in open_entries.items():
    closes = close_entries.get(cid, [])
    for i, op in enumerate(opens):
        cost = op["entry_price"] * op.get("original_shares", op["shares"])
        if i < len(closes):
            cl = closes[i]
            ret = cl.get("settlement_value", cost)
            pnl = ret - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
            all_trades_list.append({
                "title": op["title"],
                "entry": op["entry_price"],
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "reason": cl.get("close_reason", "unknown"),
                "outcome": cl.get("outcome_class", "unknown")
            })
        else:
            # Still open - use current value
            op_key = f"{cid}-{op['side']}"
            op_current = pf["positions"].get(op_key, op)
            current_val = op_current.get("value", cost)
            pnl = current_val - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
            all_trades_list.append({
                "title": op["title"],
                "entry": op["entry_price"],
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "reason": "open",
                "outcome": "open"
            })

best = max(all_trades_list, key=lambda x: x["pnl_pct"])
worst = min(all_trades_list, key=lambda x: x["pnl_pct"])

# Count tick TP1/TP2 from partial exits
# In this system, safety_sell = tick_tp_1
tick_tp1_positions = set()
tick_tp2_positions = set()
for pos in pf["positions"].values():
    if pos.get("safety_sold"):
        tick_tp1_positions.add(pos.get("title", ""))
    if pos.get("main_sold"):
        tick_tp2_positions.add(pos.get("title", ""))
# Also check trade log
for t in trades:
    if t["action"] == "PARTIAL_CLOSE":
        pe = t.get("partial_exit", {})
        reason = pe.get("reason", "")
        if reason in ("safety_sell", "tick_tp_1"):
            tick_tp1_positions.add(t.get("title", ""))
        elif reason == "tick_tp_2":
            tick_tp2_positions.add(t.get("title", ""))

# --- 5. Exit Reason Breakdown ---
reasons = Counter()
for t in all_trades_list:
    r = t["reason"]
    if r != "open":
        reasons[r] += 1
# Also check closed positions for close_reason
for p in closed_positions.values():
    r = p.get("close_reason", "unknown")
    if r:
        reasons[r] += 0  # already counted via all_trades_list

# Make sure we have all the reasons from closed positions
reason_from_positions = Counter()
for p in closed_positions.values():
    r = p.get("close_reason", "unknown")
    if r:
        reason_from_positions[r] += 1

# Use the more accurate from all_trades_list (which counts each open-close pair)
# But also ensure we capture the right mapping
# From data:
# trading_stop appears in: Miami May 17 (loss), Miami lowest (real_win), Seattle May 17 (real_win), Dallas (loss), Chicago May 18 (loss), Atlanta 98F+ (real_win), Miami May 16 (real_win)
# edge_exhausted appears in: Seattle May 16 (flat), Atlanta May 17 (flat)

# Let me just recount from the CLOSE actions in the trades log
reason_from_closes = Counter()
for t in close_actions:
    r = t.get("close_reason", "unknown")
    reason_from_closes[r] += 1

# --- 6. Entry Price Distribution ---
entry_dist = Counter()
for t in open_actions:
    entry_dist[t["entry_price"]] += 1

# --- 7. Open Positions Detail ---
open_lines = []
for pos_id, pos in open_positions.items():
    title = pos.get("title", "?")
    if len(title) > 50:
        title = title[:47] + "..."
    city = pos.get("city", "")
    entry = pos["entry_price"]
    current = pos["current_price"]
    pnl_d = pos["pnl"]
    pnl_pct = pos["pnl_pct"]
    peak = pos.get("peak_pnl_pct", 0)
    safety = "🛡️" if pos.get("safety_sold") else ""
    open_lines.append(
        f"• {city} — {title}\n"
        f"  Entry: ${entry:.3f} → ${current:.4f}\n"
        f"  PnL: ${pnl_d:.2f} ({pnl_pct:+.1f}%) | Peak MFE: {peak:+.1f}% {safety}"
    )
open_str = "\n\n".join(open_lines) if open_lines else "None"

# --- Build Message ---
exit_emoji = {
    "trailing_stop": "🔴", "edge_exhausted": "🟡", "tick_tp_1": "🔵",
    "tick_tp_2": "🟣", "take_profit": "🟢", "stop_loss": "⛔",
}
exit_order = ["tick_tp_1", "tick_tp_2", "trailing_stop", "edge_exhausted", "take_profit", "stop_loss"]
exit_lines = []
for r in exit_order:
    count = reason_from_closes.get(r, 0)
    emoji = exit_emoji.get(r, "➖")
    exit_lines.append(f"  {emoji} {r}: {count}")
exit_str = "\n".join(exit_lines)

entry_lines = []
for price in [0.001, 0.002, 0.003]:
    count = entry_dist.get(price, 0)
    entry_lines.append(f"  ${price:.3f}: {count} trades")
entry_str = "\n".join(entry_lines)

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
• Total Trades: {total_trades}
• Closed: {closed_trades} ({profitable_wins}W / {flat_exits}F / {losses}L)
• Win Rate: {win_rate:.0f}%

*💰 PnL*
• Realized: ${total_realized:.2f}
• Unrealized: ${unrealized_pnl:.2f}
• Total PnL: ${total_pnl:.2f}
• Total Return: {total_return_pct:+.2f}%

*📉 Performance*
• Best: {best_title} — {best['pnl_pct']:+.1f}%
• Worst: {worst_title} — {worst['pnl_pct']:+.1f}%

*🚪 Exit Reasons*
{exit_str}

*💰 Entry Prices*
{entry_str}

*🎯 Partial Exits*
• Tick TP1 (safety_sell): {len(tick_tp1_positions)} positions
• Tick TP2 (main sell): {len(tick_tp2_positions)} positions

*📋 Open Positions*
{open_str}

*📌 Milestone*
• {total_trades} / 100 trades"""

print(msg)
print("\n--- SENDING TO TELEGRAM ---")
result = send_telegram(msg)
print(f"Telegram send: {'OK' if result else 'FAILED'}")
