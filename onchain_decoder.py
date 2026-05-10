#!/usr/bin/env python3
"""
On-chain Trade Decoder — reads Polymarket NegRisk CTF events
from on-chain data and resolves market info via Gamma API.

PROVEN (May 2026):
- NegRisk CTF: 0xC5d563A36AE78145c45a50134d48A1215220f80a
- EVENT_POSITION sig: 0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6
  topic[1] = condition_id (maps to Gamma API market)
  topic[2] = maker address
  topic[3] = taker address

Decode strategy:
1. Get USDC transfers from whale → NegRisk CTF (via Etherscan tokentx)
2. For each tx, get receipt and find EVENT_POSITION logs
3. Extract condition_id from topic[1]
4. Look up market info via Gamma API
"""
import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Polymarket contracts on Polygon
NEG_RISK_CTF = "0xC5d563A36AE78145c45a50134d48A1215220f80a"
USDC_TOKEN = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Event signatures
EVENT_POSITION = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"

# APIs
GAMMA_BASE = "https://gamma-api.polymarket.com"
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"


class OnChainTradeDecoder:
    """Decodes whale trades from on-chain Polymarket events."""

    def __init__(self, etherscan_key: str = ""):
        self.etherscan_key = etherscan_key
        self._market_cache = {}  # condition_id -> market info
        self._tx_receipt_cache = {}  # tx_hash -> receipt
        self._session = requests.Session()

    def _etherscan_get(self, params: dict) -> dict | None:
        """Call Etherscan V2 API. Handles proxy endpoints (no status field)."""
        params.setdefault("apikey", self.etherscan_key)
        params.setdefault("chainid", "137")
        try:
            r = self._session.get(ETHERSCAN_V2, params=params, timeout=15)
            data = r.json()
            # Proxy endpoints (eth_getTransactionReceipt, etc.) return JSON-RPC 
            # without a 'status' field. Still valid.
            if "status" in data:
                if data.get("status") == "1":
                    return data
                return None
            # No status field = JSON-RPC format (proxy endpoint)
            if "result" in data:
                return data
            return None
        except Exception as e:
            logger.warning("Etherscan error: %s", e)
            return None

    def get_usdc_transfers(self, wallet: str, from_block: int = 0, to_block: int = 999999999) -> list[dict]:
        """Get USDC transfers between a wallet and NegRisk CTF."""
        params = {
            "module": "account", "action": "tokentx",
            "address": wallet, "contractaddress": USDC_TOKEN,
            "startblock": from_block, "endblock": to_block,
            "sort": "desc",
        }
        data = self._etherscan_get(params)
        txs = (data or {}).get("result", [])
        # Filter: only transfers involving NegRisk CTF
        negrisk_filtered = [
            tx for tx in txs
            if tx.get("to", "").lower() == NEG_RISK_CTF.lower()
        ]
        return negrisk_filtered

    def decode_transaction(self, tx_hash: str, wallet: str = "") -> list[dict]:
        """
        Decode a single transaction's Polymarket events.
        Returns list of decoded trades from this tx.
        """
        # Use cache to avoid re-decoding
        if tx_hash in self._tx_receipt_cache:
            receipt = self._tx_receipt_cache[tx_hash]
        else:
            params = {
                "module": "proxy",
                "action": "eth_getTransactionReceipt",
                "txhash": tx_hash,
            }
            data = self._etherscan_get(params)
            receipt = (data or {}).get("result", {})
            if receipt:
                self._tx_receipt_cache[tx_hash] = receipt

        logs = receipt.get("logs", [])
        trades = []

        for log in logs:
            addr = log.get("address", "").lower()
            topics = log.get("topics", [])
            data_field = log.get("data", "")
            block_hex = log.get("blockNumber", "0x0")

            if addr != NEG_RISK_CTF.lower():
                continue
            if not topics or topics[0].lower() != EVENT_POSITION.lower():
                continue
            if len(topics) < 2:
                continue

            # topic[1] = condition_id
            condition_id = "0x" + topics[1][-64:]

            # Amount from data field (if available)
            amount = 0
            if len(data_field) >= 130:
                amount = int(data_field[66:130], 16)

            # Decode maker/taker from topics
            maker = ("0x" + topics[2][-40:]) if len(topics) > 2 else ""
            taker = ("0x" + topics[3][-40:]) if len(topics) > 3 else ""

            # Track whether this involves our target wallet
            involved = False
            if wallet:
                w = wallet.lower()
                involved = (maker.lower() == w or taker.lower() == w)
            else:
                involved = True

            if not involved:
                continue

            trades.append({
                "condition_id": condition_id,
                "amount": amount,
                "maker": maker,
                "taker": taker,
                "block": int(block_hex, 16),
                "tx_hash": tx_hash[:20],
            })

        return trades

    def get_market_info(self, condition_id: str) -> dict | None:
        """Look up market question via Gamma API with caching."""
        if not condition_id or len(condition_id) < 10:
            return None
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        try:
            r = self._session.get(
                f"{GAMMA_BASE}/markets",
                params={"condition_id": condition_id, "limit": 1},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    self._market_cache[condition_id] = data[0]
                    return data[0]
        except Exception as e:
            logger.warning("Gamma lookup failed for %s: %s", condition_id[:20], e)
        return None

    def poll_latest_trades(self, wallet: str, since_block: int = 0, max_txs: int = 20) -> list[dict]:
        """Quick poll: get USDC transfers since a block and decode them."""
        wallet = wallet.lower()
        transfers = self.get_usdc_transfers(wallet, from_block=since_block)
        if not transfers:
            return []

        trades = []
        for tx in transfers[:max_txs]:
            # Check if we already decoded this tx
            tx_hash = tx.get("hash", "")
            decoded = self.decode_transaction(tx_hash, wallet=wallet)
            for trade in decoded:
                market_info = self.get_market_info(trade["condition_id"])
                question = market_info.get("question", "?") if market_info else "?"
                trades.append({
                    "question": question[:120],
                    "condition_id": trade["condition_id"],
                    "amount": trade["amount"],
                    "usdc_value": round(float(tx.get("value", 0)) / 1e6, 2),
                    "block": trade["block"],
                    "tx_hash": trade["tx_hash"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
        return trades

    def analyze_whale_trades(self, wallet: str, label: str = "",
                             from_block: int = 0, max_txs: int = 100) -> dict:
        """
        Full analysis: get USDC transfers → decode each tx → resolve markets.
        Returns structured data for display and storage.
        """
        wallet = wallet.lower()
        transfers = self.get_usdc_transfers(wallet, from_block=from_block)

        if not transfers:
            logger.info("No USDC transfers found for %s", wallet[:10])
            return {"wallet": label or wallet[:10], "trades": [], "markets": {}}

        logger.info("Analyzing %d USDC transfers for %s", min(len(transfers), max_txs), wallet[:10])

        all_trades = []
        by_market = Counter()
        total_usdc = 0.0

        for i, tx in enumerate(transfers[:max_txs]):
            tx_hash = tx.get("hash", "")
            usdc_value = float(tx.get("value", 0)) / 1e6
            total_usdc += usdc_value

            decoded = self.decode_transaction(tx_hash, wallet=wallet)
            for trade in decoded:
                market_info = self.get_market_info(trade["condition_id"])
                question = market_info.get("question", "?") if market_info else "?"
                outcomes = market_info.get("outcomes", []) if market_info else []
                outcome_prices = market_info.get("outcomePrices", "[]") if market_info else "[]"

                all_trades.append({
                    "condition_id": trade["condition_id"],
                    "question": question[:120],
                    "amount": trade["amount"],
                    "usdc_value": round(usdc_value, 2),
                    "block": trade["block"],
                    "tx_hash": trade["tx_hash"],
                    "outcomes": outcomes,
                    "outcome_prices": outcome_prices,
                })
                by_market[question] += 1

            if (i + 1) % 20 == 0:
                logger.info("  Decoded %d/%d txs...", i + 1, min(len(transfers), max_txs))
            time.sleep(0.15)  # Etherscan rate limit

        return {
            "wallet": label or wallet[:10],
            "address": wallet,
            "total_txs": len(transfers[:max_txs]),
            "total_usdc": round(total_usdc, 2),
            "total_trades": len(all_trades),
            "unique_markets": len(by_market),
            "markets": dict(by_market.most_common(20)),
            "trades": all_trades[:50],  # Only keep recent 50
        }

    def summary(self, wallet: str, label: str = "", from_block: int = 0) -> str:
        """Human-readable trade summary."""
        result = self.analyze_whale_trades(wallet, label, from_block=from_block, max_txs=30)
        lines = []
        lines.append(f"🐋 {result['wallet']} — Trade Summary")
        lines.append("=" * 50)
        lines.append(f"  Txs analyzed: {result['total_txs']}")
        lines.append(f"  Total USDC: ${result['total_usdc']}")
        lines.append(f"  Trades decoded: {result['total_trades']}")
        lines.append(f"  Unique markets: {result['unique_markets']}")
        lines.append("")

        for q, count in list(result.get("markets", {}).items())[:10]:
            lines.append(f"  x{count:3d}  {q[:70]}")
        return "\n".join(lines)
