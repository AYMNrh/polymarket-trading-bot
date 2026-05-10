"""
Etherscan API V2 client — multi-chain data source for Polymarket on Polygon.

Access Polygon data through Etherscan API V2. Single key works for 60+ EVM chains.
Docs: https://docs.etherscan.io/api-v2
"""
import logging
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Etherscan API V2 base URL (pass chainid as query param)
V2_BASE = "https://api.etherscan.io/v2/api"

# Chain ID for Polygon mainnet
POLYGON_CHAIN = "137"

# Polymarket contracts on Polygon
CTF_EXCHANGE = "0x4bFb41d5B3570C1C6cBb5E7cB3E8d9a0B0a0b0c0"
NEG_RISK_CTF = "0xC5d563A36AE78145c45a50134d48A1215220f80a"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


class EtherscanV2Client:
    """Multi-chain client using Etherscan API V2.
    
    Single API key works for Polygon, Ethereum, and 60+ other chains.
    Free tier: 5 calls/sec, 100k calls/day.
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self._last_call = 0.0
        self._min_interval = 0.25  # 4 calls/sec to stay under limit

    def _rate_limit(self):
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    def _get(self, path: str, params: dict = None) -> dict | None:
        """Generic V2 API call — pass chainid as query param."""
        self._rate_limit()
        url = V2_BASE
        headers = {}
        query_params = {"chainid": POLYGON_CHAIN}
        if params:
            query_params.update(params)
        if self.api_key:
            query_params["apikey"] = self.api_key
        try:
            r = requests.get(url, params=query_params, headers=headers, timeout=15)
            data = r.json()
            # Proxy endpoints (eth_blockNumber etc.) return JSON-RPC format
            # without a "status" field — accept any non-error response.
            if "status" in data:
                if data.get("status") != "1":
                    err = data.get("message", data.get("result", "unknown"))
                    logger.warning("Etherscan V2 error [%s]: %s", path, err)
                    return None
            return data
        except Exception as e:
            logger.error("Etherscan V2 request failed [%s]: %s", path, e)
            return None

    def get_token_transfers(
        self,
        address: str,
        start_block: int = 0,
        end_block: int = 999999999,
        limit: int = 100,
    ) -> list[dict]:
        """Get ERC20 token transfers for an address."""
        data = self._get("address", params={
            "module": "account",
            "action": "tokentx",
            "address": address,
            "startblock": start_block,
            "endblock": end_block,
            "limit": limit,
        })
        return (data or {}).get("result", [])

    def get_logs(
        self,
        address: str,
        topic0: str = "",
        from_block: int = 0,
        to_block: int = 999999999,
        limit: int = 100,
    ) -> list[dict]:
        """Get event logs for a contract address."""
        params = {
            "module": "logs",
            "action": "getLogs",
            "address": address,
            "fromBlock": hex(from_block) if from_block else "0x0",
            "toBlock": hex(to_block) if to_block < 999999999 else "latest",
            "limit": limit,
        }
        if topic0:
            params["topic0"] = topic0
        data = self._get("logs", params=params)
        return (data or {}).get("result", [])

    def get_block_number(self) -> int | None:
        """Get latest block number via proxy (uses JSON-RPC format)."""
        data = self._get("block", params={
            "module": "proxy",
            "action": "eth_blockNumber",
        })
        # Proxy endpoint returns result directly (no status field)
        if data and data.get("result"):
            return int(data["result"], 16)
        return None

    def get_transaction(self, tx_hash: str) -> dict | None:
        """Get transaction details."""
        data = self._get("tx", params={"txhash": tx_hash})
        return (data or {}).get("result")

    def get_token_info(self, contract: str) -> dict | None:
        """Get token info (name, symbol, decimals, total supply)."""
        data = self._get("token", params={
            "contractaddress": contract,
            "action": "tokeninfo",
        })
        return (data or {}).get("result")
