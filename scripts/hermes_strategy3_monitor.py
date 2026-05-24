#!/usr/bin/env python3
"""Hermes no-agent wrapper for Strategy 3 position monitoring."""

import sys

PROJECT = "/home/aymen/projects/scripts/trading-bot"
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from paper_trader import STRATEGY3_MODE
from strategy_bot_runner import run_position_monitor


if __name__ == "__main__":
    print(run_position_monitor(STRATEGY3_MODE))
