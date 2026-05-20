#!/usr/bin/env python3
"""Tail experiment cron: runs PaperTrader in tail-experiment mode, every 5m."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from paper_trader import PaperTrader, _save_state
from config import load_config

config = load_config()

trader = PaperTrader(mode='tail-experiment')

total_trades = trader.state.get("total_trades", 0)
print(f"Tail experiment v6.1 continuous: {total_trades} trades")

# Discover weather markets using PaperTrader's built-in method
markets = trader.discover_weather_markets()
if not markets:
    print("No weather markets found")
    sys.exit(0)

# Load whale positions
try:
    import json
    with open(trader.state_file.parent / "whale_portfolios.json") as f:
        whale_data = json.load(f)
    whale_positions = []
    for wallet_addr, wallet_data in whale_data.items():
        if isinstance(wallet_data, dict):
            positions = wallet_data.get("positions", wallet_data.get("trades", []))
            if isinstance(positions, list):
                for pos in positions:
                    if isinstance(pos, dict):
                        pos["wallet_address"] = wallet_addr
                        whale_positions.append(pos)
except Exception as e:
    print(f"Whale data load failed (non-fatal): {e}")
    whale_positions = []

# Evaluate and trade
trades_opened = 0
for market in markets:
    result = trader.evaluate_and_trade(market, whale_positions)
    if result:
        trades_opened += 1

# Apply risk stops
closed = trader.apply_risk_stops()

# Update prices for MFE/MAE
trader.update_prices()

print(f"  Opened: {trades_opened}, Closed: {len(closed)}")
print(f"  Bankroll: ${trader.state['bankroll']:.2f}, Open: {len(trader._open_positions)}")
