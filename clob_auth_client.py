"""
CLOB API client with EIP-712 auth using the relayer private key.
"""
import base64
import hashlib
import hmac
import json
import logging
import time

import requests
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"

# EIP-712 domain for CLOB API
CLOB_DOMAIN = {
    "name": "Polymarket CLOB",
    "version": "1",
    "chainId": 137,
}


class ClobAuthClient:
    """Authenticated CLOB client using EIP-712 signatures."""

    def __init__(self, private_key: str, address: str,
                 relayer_api_key: str = "",
                 endpoint: str = CLOB_BASE):
        self.private_key = private_key
        self.address = address.lower()
        self.relayer_api_key = relayer_api_key
        self.endpoint = endpoint.rstrip("/")
        self.account = Account.from_key(private_key)

    def _sign_typed_data(self, message: dict) -> str:
        """Sign EIP-712 typed data and return the signature."""
        signed = self.account.sign_typed_data(message)
        return signed.signature.hex()

    def _sign_message(self, message: str) -> str:
        """Sign a raw message (personal_sign format)."""
        msg = encode_defunct(text=message)
        signed = self.account.sign_message(msg)
        return signed.signature.hex()

    def authenticate(self) -> str | None:
        """Authenticate and get a CLOB API key/session token.
        
        This uses EIP-712 to sign an auth message and create a session.
        """
        # Try signing a CLOB auth message
        ts = str(int(time.time() * 1000))
        msg = f"Polymarket CLOB auth - {ts}"
        sig = self._sign_message(msg)
        
        # POST the signature to create a session
        try:
            r = requests.post(
                f"{self.endpoint}/auth",
                json={
                    "address": self.address,
                    "timestamp": ts,
                    "signature": f"0x{sig}",
                },
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                logger.info("CLOB auth successful")
                return data.get("token", data.get("apiKey", ""))
            else:
                logger.warning("CLOB auth failed: %s", r.text[:100])
                return None
        except Exception as e:
            logger.error("CLOB auth error: %s", e)
            return None

    def _sign_hmac(self, ts: int, method: str, path: str, secret: str, body: str = "") -> str:
        """HMAC signature for authenticated requests (using session secret)."""
        msg = str(ts) + method.upper() + path
        if body:
            msg += body
        raw = base64.b64decode(secret.replace('-', '+').replace('_', '/') + "==")
        sig = hmac.new(raw, msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(sig).decode().replace("+", "-").replace("/", "_")

    def get_whale_trades(self, wallet_address: str, limit: int = 50,
                         session_key: str = "", session_secret: str = "",
                         session_passphrase: str = "") -> list[dict]:
        """Get trades for a specific wallet using CLOB API."""
        path = f"/trades?maker_address={wallet_address}&limit={limit}"
        
        if session_key and session_secret:
            # Use HMAC auth with session credentials
            ts = int(time.time() * 1000)
            sig = self._sign_hmac(ts, "GET", path, session_secret)
            headers = {
                "POLY_API_KEY": session_key,
                "POLY_ADDRESS": self.address,
                "POLY_SIGNATURE": sig,
                "POLY_PASSPHRASE": session_passphrase or "",
                "POLY_TIMESTAMP": str(ts),
            }
            try:
                r = requests.get(f"{self.endpoint}{path}", headers=headers, timeout=30)
                if r.status_code == 200:
                    return r.json() if isinstance(r.json(), list) else r.json().get("data", [])
                else:
                    logger.warning("CLOB trades failed: %d %s", r.status_code, r.text[:100])
            except Exception as e:
                logger.error("CLOB trades error: %s", e)
        
        return []
