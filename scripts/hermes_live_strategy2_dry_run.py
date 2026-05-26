#!/usr/bin/env python3
"""Hermes wrapper for the Strategy 2 live-money dry-run pilot."""

from pathlib import Path
import sys

ROOT_CANDIDATES = (
    Path.cwd(),
    Path("/home/aymen/projects/scripts/trading-bot"),
    Path(__file__).resolve().parents[1],
)
ROOT = next((path for path in ROOT_CANDIDATES if (path / "live_strategy2.py").exists()), Path.cwd())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_strategy2 import main


if __name__ == "__main__":
    raise SystemExit(main())
