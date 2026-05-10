"""
CTF Exchange scanner — monitors ALL Polymarket trades on-chain
and identifies whale wallets by their cumulative activity.

Whale detection logic:
- Track every wallet that trades through the CTF Exchange
- Aggregate total volume per wallet over rolling windows
- Flag wallets that trade $500+ cumulative across multiple markets
- These are your stealth whales — making 5-50 small trades instead of one big one
"""
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from config import load_config
from polygonscan_client import PolygonscanClient

logger = logging.getLogger(__name__)

# Polymarket CTF Exchange contracts on Polygon
CTF_EXCHANGE = "0xe2222d279d744050d28e00520010520000310f59"
NEG_RISK_CTF = "0xC5d563A36AE78145c45a50134d48A1215220f80a"


class WhaleScanner:
    """
    Scans the Polymarket CTF Exchange for all token transfers,
    aggregates per-wallet volume, and discovers potential whales
    based on cumulative activity patterns (not single large trades).
    """

    def __init__(self):
        cfg = load_config()
        self.api_key = cfg.get("polygonscan_api_key", "")
        self.polygon = PolygonscanClient(self.api_key)
        self.min_cumulative_usd = 500  # Flag wallets that move $500+ total
        self.scan_depth_blocks = 50000  # ~3 days of Polygon blocks

    def get_exchange_transfers(self, from_block: int = 0) -> list[dict]:
        """Get all token transfers from the CTF Exchange contracts."""
        transfers = []
        for contract in [CTF_EXCHANGE, NEG_RISK_CTF]:
            tx = self.polygon.get_token_transfers(
                contract, start_block=from_block
            )
            if tx:
                transfers.extend(tx)
        return transfers

    def aggregate_wallet_activity(
        self, transfers: list[dict]
    ) -> dict[str, dict]:
        """
        Group all transfers by wallet address and compute:
        - Total volume (in + out)
        - Number of trades
        - Unique tokens traded
        - Direction (buyer vs seller bias)
        """
        activity: dict[str, dict] = {}

        for tx in transfers:
            # Normalize addresses
            from_addr = tx.get("from", "").lower()
            to_addr = tx.get("to", "").lower()

            # Identify the actual trader (the counterparty, not the exchange)
            contract_addrs = {CTF_EXCHANGE.lower(), NEG_RISK_CTF.lower()}
            trader_addr = None
            if from_addr in contract_addrs and to_addr not in contract_addrs:
                trader_addr = to_addr  # exchange sent to user (withdrawal/payout)
            elif to_addr in contract_addrs and from_addr not in contract_addrs:
                trader_addr = from_addr  # user sent to exchange (deposit/trade)
            else:
                continue  # skip if both or neither involve exchange

            token_decimal = int(tx.get("tokenDecimal", 18))
            value = float(tx.get("value", 0)) / (10 ** token_decimal)
            token = tx.get("tokenSymbol", "UNKNOWN")
            token_name = tx.get("tokenName", "")
            tx_hash = tx.get("hash", "")
            timestamp = int(tx.get("timeStamp", 0))

            side = "buy" if from_addr in contract_addrs else "sell"
            addr = trader_addr
            if addr not in activity:
                activity[addr] = {
                    "address": addr,
                    "total_volume": 0.0,
                    "buy_volume": 0.0,
                    "sell_volume": 0.0,
                    "num_trades": 0,
                    "unique_tokens": set(),
                    "token_names": set(),
                    "first_seen": timestamp,
                    "last_seen": timestamp,
                    "recent_tx_hashes": [],
                }
            a = activity[addr]
            a["total_volume"] += value
            if side == "buy":
                a["buy_volume"] += value
            else:
                a["sell_volume"] += value
            a["num_trades"] += 1
            a["unique_tokens"].add(token)
            if token_name:
                a["token_names"].add(token_name)
            if timestamp < a["first_seen"]:
                a["first_seen"] = timestamp
            if timestamp > a["last_seen"]:
                a["last_seen"] = timestamp
            if len(a["recent_tx_hashes"]) < 5:
                a["recent_tx_hashes"].append(tx_hash)

        # Convert sets to sorted lists for serialization
        for addr, a in activity.items():
            a["unique_tokens"] = sorted(a["unique_tokens"])
            a["token_names"] = sorted(a["token_names"])[:10]
            a["total_volume"] = round(a["total_volume"], 2)
            a["buy_volume"] = round(a["buy_volume"], 2)
            a["sell_volume"] = round(a["sell_volume"], 2)

        return activity

    def discover_whales(self, activity: dict[str, dict]) -> list[dict]:
        """
        Find wallets that match whale patterns:
        - Cumulative volume > $500
        - Multiple trades (not just 1-2)
        - Trading across multiple tokens/markets
        """
        whales = []
        for addr, a in activity.items():
            score = 0
            reasons = []

            # Criterion 1: Total volume
            if a["total_volume"] >= self.min_cumulative_usd:
                score += 1
                reasons.append(f"volume ${a['total_volume']:.0f}")

            # Criterion 2: Multiple trades (whales spread across many small trades)
            if a["num_trades"] >= 5:
                score += 1
                reasons.append(f"{a['num_trades']} trades")
            elif a["num_trades"] >= 3:
                score += 1
                reasons.append(f"{a['num_trades']} trades")

            # Criterion 3: Multiple tokens (spreading across markets)
            if len(a["unique_tokens"]) >= 3:
                score += 1
                reasons.append(f"{len(a['unique_tokens'])} tokens")
            elif len(a["unique_tokens"]) >= 2:
                score += 1
                reasons.append(f"{len(a['unique_tokens'])} tokens")

            # Criterion 4: Both buying and selling (active trader, not one-off)
            if a["buy_volume"] > 100 and a["sell_volume"] > 100:
                score += 1
                reasons.append("active both sides")

            if score >= 2:
                whales.append({
                    "address": addr,
                    "total_volume": a["total_volume"],
                    "buy_volume": a["buy_volume"],
                    "sell_volume": a["sell_volume"],
                    "num_trades": a["num_trades"],
                    "unique_tokens": a["unique_tokens"],
                    "score": score,
                    "reasons": reasons,
                    "recent_tx_hashes": a["recent_tx_hashes"],
                    "last_seen": datetime.fromtimestamp(
                        a["last_seen"], tz=timezone.utc
                    ).isoformat(),
                })

        # Sort by score (desc), then volume (desc)
        whales.sort(key=lambda w: (-w["score"], -w["total_volume"]))
        return whales

    def scan(self, from_block: int = 0) -> list[dict]:
        """Full scan: get transfers → aggregate → discover whales."""
        last_block = self.polygon.get_block_number()
        if last_block:
            scan_from = max(from_block, last_block - self.scan_depth_blocks)
        else:
            scan_from = from_block

        logger.info(
            "Scanning CTF Exchange from block %d (current: %s)",
            scan_from, last_block or "?"
        )
        transfers = self.get_exchange_transfers(from_block=scan_from)
        if not transfers:
            logger.warning("No transfers retrieved (API key needed?)")
            return []

        logger.info("Got %d transfer events", len(transfers))
        activity = self.aggregate_wallet_activity(transfers)
        logger.info("Found %d unique wallets trading", len(activity))

        whales = self.discover_whales(activity)
        logger.info("Discovered %d potential whale wallets", len(whales))

        return whales
