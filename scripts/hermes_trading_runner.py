#!/usr/bin/env python3
"""Hermes wrapper for the simplified trading runner."""

from pathlib import Path
import sys

ROOT = Path("/home/aymen/projects/scripts/trading-bot")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
