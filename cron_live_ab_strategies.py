#!/usr/bin/env python3
"""Cron entrypoint for the CLOB-priced A/B strategy pilot."""

from live_ab_strategies import main


if __name__ == "__main__":
    raise SystemExit(main())
