#!/usr/bin/env python3
"""Run the simplified production-facing bots once."""

import json

from arb_bot import run_once as run_arb
from btc_5m_bot import run_once as run_btc
from wallet_tracker import scan_wallets


def main() -> int:
    result = {
        "btc_5m": run_btc(),
        "arbitrage": run_arb(),
        "wallets": scan_wallets(),
    }
    print(json.dumps({
        "btc_error": result["btc_5m"].get("last_error"),
        "btc_cycle": bool(result["btc_5m"].get("current_cycle")),
        "arb_error": result["arbitrage"].get("last_error"),
        "arb_opportunities": len(result["arbitrage"].get("opportunities", [])),
        "wallet_error": result["wallets"].get("last_error"),
        "wallet_signals": len(result["wallets"].get("signals", [])),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
