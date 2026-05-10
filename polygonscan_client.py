"""
Polygonscan API client — monitors on-chain token transfers for watched wallets.
Polymarket uses the CTF Exchange contract on Polygon.
"""
import time
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Use Etherscan V2 API (single key for all chains)
BASE_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = "137"

# Polymarket CTF Exchange (old) — check the latest deployed address
# Most activity flows through the CTF ERC1155 proxy
CTF_EXCHANGE = "0xe2222d279d744050d28e00520010520000310f59"
NEG_RISK_CTF = "0xC5d563A36AE78145c45a50134d48A1215220f80a"


class PolygonscanClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._last_call = 0.0
        # Free tier: 5 calls/sec — we pace to 4/sec
        self._min_interval = 0.25

    def _rate_limit(self):
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    def _get(self, params: dict) -> dict | list | None:
        self._rate_limit()
        params.setdefault("apikey", self.api_key)
        params.setdefault("chainid", CHAIN_ID)
        try:
            r = requests.get(BASE_URL, params=params, timeout=15)
            data = r.json()
            # Proxy endpoints use JSON-RPC format (no "status" field)
            if "status" in data:
                if data.get("status") != "1":
                    logger.warning("Polygonscan API error: %s", data.get("message"))
                    return None
            return data
        except Exception as e:
            logger.error("Polygonscan request failed: %s", e)
            return None

    def get_token_transfers(
        self,
        address: str,
        start_block: int = 0,
        end_block: int = 999999999,
        sort: str = "desc",
    ) -> list[dict]:
        """Get ERC20 token transfers for an address (in/out)."""
        params = {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "startblock": start_block,
            "endblock": end_block,
            "sort": sort,
        }
        data = self._get(params)
        return (data or {}).get("result", []) if isinstance(data, dict) else []

    def get_transaction_history(
        self,
        address: str,
        start_block: int = 0,
        end_block: int = 999999999,
        sort: str = "desc",
    ) -> list[dict]:
        """Get normal (MATIC) transactions for an address."""
        params = {
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": start_block,
            "endblock": end_block,
            "sort": sort,
        }
        data = self._get(params)
        return (data or {}).get("result", []) if isinstance(data, dict) else []

    def get_block_number(self) -> int | None:
        """Get the latest block number on Polygon."""
        params = {"module": "proxy", "action": "eth_blockNumber"}
        data = self._get(params)
        if data and isinstance(data, dict) and data.get("result"):
            return int(data["result"], 16)
        return None

    def filter_polymarket_trades(
        self, transfers: list[dict], from_block: int = 0
    ) -> list[dict]:
        """Filter token transfers that involve Polymarket CTF contracts.
        Only returns USDC/USDC.e transfers (real money, not pUSD)."""
        trades = []
        polymarket_contracts = {
            CTF_EXCHANGE.lower(),
            NEG_RISK_CTF.lower(),
        }
        valid_tokens = {"usdc.e", "usdc", "usdc.e (pos)", "usdc (pos)"}
        for tx in transfers:
            if int(tx.get("blockNumber", 0)) < from_block:
                continue
            to = tx.get("to", "").lower()
            contract = tx.get("contractAddress", "").lower()
            token = (tx.get("tokenSymbol") or "").lower()
            # Only real money tokens, not pUSD
            if token not in valid_tokens and "usdc" not in token:
                continue
            # Polymarket trades interact with the CTF contracts
            if to in polymarket_contracts or contract in polymarket_contracts:
                trades.append(tx)
        return trades
