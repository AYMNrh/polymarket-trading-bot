#!/usr/bin/env python3
"""Cron entrypoint for Strategy 1: cheap middle bucket spike capture."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from paper_trader import STRATEGY1_MODE
from strategy_bot_runner import run_strategy


if __name__ == "__main__":
    run_strategy(STRATEGY1_MODE)
