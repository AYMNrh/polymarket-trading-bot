#!/usr/bin/env python3
"""Cron entrypoint for Strategy 2: ultra-cheap tail spike capture."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from paper_trader import STRATEGY2_MODE
from strategy_bot_runner import run_strategy


if __name__ == "__main__":
    run_strategy(STRATEGY2_MODE)
