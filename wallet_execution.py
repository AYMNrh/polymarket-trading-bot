"""Wallet-gated Polymarket execution adapter.

The strategy code calls this adapter for every order. Dry-run is the default.
Real execution requires explicit env flags plus wallet credentials; this keeps
analysis and dashboard runs from ever placing orders accidentally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


class ExecutionDisabled(RuntimeError):
    pass


@dataclass
class ExecutionConfig:
    dry_run: bool = True
    live_enabled: bool = False
    private_key: str | None = None
    funder: str | None = None
    chain_id: int = 137

    @classmethod
    def from_env(cls, prefix: str = "POLYMARKET") -> "ExecutionConfig":
        return cls(
            dry_run=_env_bool(f"{prefix}_DRY_RUN", True),
            live_enabled=_env_bool(f"{prefix}_LIVE_TRADING", False),
            private_key=os.getenv(f"{prefix}_PRIVATE_KEY"),
            funder=os.getenv(f"{prefix}_FUNDER") or os.getenv(f"{prefix}_PROXY_ADDRESS"),
            chain_id=_env_int(f"{prefix}_CHAIN_ID", 137),
        )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class PolymarketExecutionAdapter:
    def __init__(self, config: ExecutionConfig | None = None):
        self.config = config or ExecutionConfig.from_env()

    def place_limit_buy(self, *, token_id: str, price: float, stake: float, metadata: dict[str, Any]) -> dict[str, Any]:
        shares = round(float(stake) / max(0.0001, float(price)), 4)
        if self.config.dry_run or not self.config.live_enabled:
            return {
                "dry_run": True,
                "status": "accepted",
                "side": "BUY",
                "token_id": token_id,
                "price": round(float(price), 6),
                "stake": round(float(stake), 2),
                "shares": shares,
                "metadata": metadata,
            }

        self._require_live_ready()
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.constants import POLYGON
        except ImportError as exc:
            raise ExecutionDisabled(
                "Install py-clob-client before enabling real Polymarket orders."
            ) from exc

        client = ClobClient(
            "https://clob.polymarket.com",
            key=self.config.private_key,
            chain_id=POLYGON if self.config.chain_id == 137 else self.config.chain_id,
            funder=self.config.funder,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        order = client.create_order(
            OrderArgs(
                price=float(price),
                size=shares,
                side="BUY",
                token_id=str(token_id),
            )
        )
        result = client.post_order(order)
        return {
            "dry_run": False,
            "status": "submitted",
            "side": "BUY",
            "token_id": token_id,
            "price": round(float(price), 6),
            "stake": round(float(stake), 2),
            "shares": shares,
            "metadata": metadata,
            "exchange_result": result,
        }

    def place_limit_sell(self, *, token_id: str, price: float, shares: float, metadata: dict[str, Any]) -> dict[str, Any]:
        if self.config.dry_run or not self.config.live_enabled:
            return {
                "dry_run": True,
                "status": "accepted",
                "side": "SELL",
                "token_id": token_id,
                "price": round(float(price), 6),
                "shares": round(float(shares), 4),
                "metadata": metadata,
            }

        self._require_live_ready()
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.constants import POLYGON
        except ImportError as exc:
            raise ExecutionDisabled(
                "Install py-clob-client before enabling real Polymarket orders."
            ) from exc

        client = ClobClient(
            "https://clob.polymarket.com",
            key=self.config.private_key,
            chain_id=POLYGON if self.config.chain_id == 137 else self.config.chain_id,
            funder=self.config.funder,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        order = client.create_order(
            OrderArgs(
                price=float(price),
                size=float(shares),
                side="SELL",
                token_id=str(token_id),
            )
        )
        result = client.post_order(order)
        return {
            "dry_run": False,
            "status": "submitted",
            "side": "SELL",
            "token_id": token_id,
            "price": round(float(price), 6),
            "shares": round(float(shares), 4),
            "metadata": metadata,
            "exchange_result": result,
        }

    def _require_live_ready(self) -> None:
        if not self.config.live_enabled:
            raise ExecutionDisabled("POLYMARKET_LIVE_TRADING must be true for real orders.")
        if not self.config.private_key:
            raise ExecutionDisabled("POLYMARKET_PRIVATE_KEY is required for real orders.")
