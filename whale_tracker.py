"""
Whale trade tracker — polls on-chain Polymarket trades for watched wallets,
enriches with market data, and logs signals.

Strategy insight: the whales trade multiple cities/subjects simultaneously,
spreading across correlated markets. The tracker detects multi-market
positioning patterns and position changes (adding, trimming, flipping).
"""
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import load_config, save_config
from polygonscan_client import PolygonscanClient
from polymarket_clob import PolymarketClobClient
from position_tracker import PositionTracker

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).parent / "whale_trades.jsonl"
SIGNAL_LOG = Path(__file__).parent / "whale_signals.jsonl"


class WhaleTracker:
    def __init__(self):
        self.cfg = load_config()
        self.polygon = PolygonscanClient(self.cfg.get("polygonscan_api_key", ""))
        self.clob = PolymarketClobClient(self.cfg.get("clob_api_endpoint"))
        self._wallet_map = {w["address"].lower(): w for w in self.cfg["watched_wallets"]}
        self.position_tracker = PositionTracker()

    def _log_trade(self, entry: dict):
        """Append a trade observation to the JSONL log."""
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def _log_signal(self, signal: dict):
        """Append a whale signal to the signals log."""
        with open(SIGNAL_LOG, "a") as f:
            f.write(json.dumps(signal, default=str) + "\n")

    def poll_once(self) -> list[dict]:
        """Poll all watched wallets for new Polymarket trades.
        Returns list of new trade events detected this cycle.
        Also processes trades through PositionTracker for conviction scoring.
        """
        new_trades = []
        conviction_signals = []
        latest_block = self.cfg.get("last_scanned_block", 0)
        current_max_block = latest_block

        for wallet in self.cfg["watched_wallets"]:
            addr = wallet["address"]
            label = wallet.get("label", addr[:8])
            transfers = self.polygon.get_token_transfers(addr, start_block=latest_block)
            if not transfers:
                continue

            polymarket_tx = self.polygon.filter_polymarket_trades(
                transfers, from_block=latest_block
            )
            if not polymarket_tx:
                continue

            for tx in polymarket_tx:
                block = int(tx.get("blockNumber", 0))
                if block > current_max_block:
                    current_max_block = block

                # Extract trade details — use tokenDecimal from Etherscan
                token_dec = int(tx.get("tokenDecimal", 18))
                value = float(tx.get("value", 0)) / (10 ** token_dec)
                token = tx.get("tokenSymbol", "UNKNOWN")
                token_name = tx.get("tokenName", "")
                contract = tx.get("contractAddress", "")
                tx_hash = tx.get("hash", "")
                timestamp = int(tx.get("timeStamp", 0))
                direction = "BUY" if tx.get("to", "").lower() == addr.lower() else "SELL"

                trade = {
                    "wallet": label,
                    "address": addr,
                    "tx_hash": tx_hash,
                    "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
                    "direction": direction,
                    "token": token,
                    "token_name": token_name,
                    "value": round(value, 4),
                    "contract": contract,
                    "block": block,
                }
                self._log_trade(trade)
                new_trades.append(trade)

                # Run through position tracker for conviction scoring
                sig = self.position_tracker.process_trade(trade)
                if sig:
                    conviction_signals.append(sig)
                    self._log_signal(sig)
                    logger.info(
                        "🎯 CONVICTION: %s %s (%.0f%%)",
                        sig.get("type", "?"), label,
                        sig.get("confidence", 0) * 100
                    )

                logger.info(
                    "🐋 %s %s %.2f %s via %s",
                    label, direction, value, token, contract[:10]
                )

        # Update last scanned block
        if current_max_block > latest_block:
            self.cfg["last_scanned_block"] = current_max_block
            save_config(self.cfg)

        return new_trades

    def detect_multi_market_positioning(self, recent_trades: list[dict]) -> dict | None:
        """Check if a whale is building positions across multiple related markets.

        The whales' strategy: spread across multiple cities / multiple subjects
        simultaneously to capture correlated moves.
        """
        if len(recent_trades) < 3:
            return None

        # Group trades by wallet
        by_wallet = defaultdict(list)
        for t in recent_trades:
            by_wallet[t["wallet"]].append(t)

        signals = {}
        for wallet_label, trades in by_wallet.items():
            if len(trades) < 2:
                continue

            unique_tokens = set(t["contract"] for t in trades)
            if len(unique_tokens) >= 2:
                total_value = sum(t["value"] for t in trades)
                signal = {
                    "type": "MULTI_MARKET_POSITIONING",
                    "wallet": wallet_label,
                    "num_markets": len(unique_tokens),
                    "num_trades": len(trades),
                    "total_value": round(total_value, 2),
                    "tokens": list(unique_tokens),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                self._log_signal(signal)
                signals[wallet_label] = signal
                logger.info(
                    "📊 SIGNAL: %s positioning across %d markets (%.2f USD)",
                    wallet_label, len(unique_tokens), total_value
                )

        return signals if signals else None

    def get_conviction_summary(self) -> list[dict]:
        """Get all tracked positions with conviction scores."""
        return self.position_tracker.get_whale_positions()

    def summary(self) -> str:
        """Print a summary of recent whale activity from logs."""
        lines = []
        lines.append("🐋 Whale Watch Summary")
        lines.append("=" * 40)

        for w in self.cfg["watched_wallets"]:
            label = w.get("label", w["address"][:8])
            wr = w.get("win_rate")
            tracked = w.get("trades_tracked", 0)
            wr_str = f"{wr*100:.0f}%" if wr is not None else "N/A"
            lines.append(f"  {label:15s}  WR: {wr_str:>4s}  Trades tracked: {tracked}")

        lines.append(f"  Last block scanned: {self.cfg.get('last_scanned_block', 0)}")

        # Add conviction summary
        pos_summary = self.position_tracker.summary()
        lines.append("")
        lines.append(pos_summary)

        return "\n".join(lines)
