"""
Whale Finder — scans the CTF Exchange for ALL active wallets by looking at
USDC transfers to/from Polymarket contracts, then scores and discovers whales.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from etherscan_client import EtherscanV2Client
from config import load_config, save_config
from database import save_whale, save_signal

CTF_CONTRACTS = [
    ("0x4bFb41d5B3570C1C6cBb5E7cB3E8d9a0B0a0b0c0", "CTF"),
    ("0xC5d563A36AE78145c45a50134d48A1215220f80a", "NegRisk"),
]


class WhaleFinder:
    def __init__(self):
        cfg = load_config()
        key = cfg.get("polygonscan_api_key", "")
        self.client = EtherscanV2Client(key)
        self.existing_wallets = {w["address"].lower() for w in cfg["watched_wallets"]}

    def scan_recent_activity(self, lookback_blocks: int = 200000) -> list[dict]:
        current_block = self.client.get_block_number()
        if not current_block:
            logger.error("Can't get current block")
            return []
        from_block = current_block - lookback_blocks

        wallet_data = defaultdict(lambda: {
            "trades": 0, "buy_vol": 0.0, "sell_vol": 0.0,
            "total_vol": 0.0, "contracts": set(),
            "first_seen": None, "last_seen": 0,
        })

        for contract_addr, label in CTF_CONTRACTS:
            transfers = self.client.get_token_transfers(
                contract_addr, start_block=from_block, limit=1000
            )
            if not transfers:
                continue
            logger.info("Got %d transfers for %s", len(transfers), label)
            for t in transfers:
                val = float(t.get("value", 0)) / 1e6
                fr = t.get("from", "").lower()
                to = t.get("to", "").lower()
                ts = int(t.get("timeStamp", 0))

                if to == contract_addr.lower():
                    # Trader sends USDC to CTF = buying
                    trader = fr
                    wallet_data[trader]["buy_vol"] += val
                elif fr == contract_addr.lower():
                    # CTF sends USDC to trader = selling/cashing out
                    trader = to
                    wallet_data[trader]["sell_vol"] += val
                else:
                    continue

                w = wallet_data[trader]
                w["trades"] += 1
                w["total_vol"] += val
                w["contracts"].add(label)
                if w["first_seen"] is None or ts < w["first_seen"]:
                    w["first_seen"] = ts
                if ts > w["last_seen"]:
                    w["last_seen"] = ts

        # Score candidates
        candidates = []
        for addr, w in wallet_data.items():
            if addr in self.existing_wallets:
                continue
            if w["trades"] < 3 or w["total_vol"] < 500:
                continue

            score = 0
            reasons = []

            if w["total_vol"] >= 10000:
                score += 2
                reasons.append(f"high vol (${w['total_vol']:.0f})")
            elif w["total_vol"] >= 1000:
                score += 1
                reasons.append(f"moderate vol (${w['total_vol']:.0f})")

            duration = max((w["last_seen"] - w["first_seen"]) / 86400, 1)
            freq = w["trades"] / duration
            if freq >= 10:
                score += 2
                reasons.append(f"active ({freq:.0f}/day)")
            elif freq >= 3:
                score += 1
                reasons.append(f"regular ({freq:.0f}/day)")

            if len(w["contracts"]) >= 2:
                score += 1
                reasons.append("both contracts")

            if w["trades"] >= 50:
                score += 1
                reasons.append(f"{w['trades']} trades")

            if score >= 2:
                candidates.append({
                    "address": addr,
                    "score": score,
                    "trades": w["trades"],
                    "volume": round(w["total_vol"], 2),
                    "buy_vol": round(w["buy_vol"], 2),
                    "sell_vol": round(w["sell_vol"], 2),
                    "freq": round(freq, 1),
                    "contracts": list(w["contracts"]),
                    "reasons": reasons,
                    "last_seen": datetime.fromtimestamp(w["last_seen"], tz=timezone.utc).isoformat(),
                })

        candidates.sort(key=lambda w: (-w["score"], -w["volume"]))
        return candidates

    def auto_add_whales(self, candidates: list[dict], max_add: int = 5) -> int:
        cfg = load_config()
        existing = {w["address"].lower() for w in cfg["watched_wallets"]}
        added = 0
        for w in candidates[:max_add]:
            addr = w["address"].lower()
            if addr in existing:
                continue
            label = f"finder_{addr[:6]}"
            cfg["watched_wallets"].append({
                "address": addr, "label": label,
                "win_rate": None, "trades_tracked": 0,
            })
            existing.add(addr)
            added += 1
            save_whale({
                "address": addr, "label": label,
                "total_volume": w["volume"],
                "num_trades": w["trades"],
                "last_seen": w["last_seen"],
            })
            logger.info("New whale: %s ($%.0f, %d trades)", label, w["volume"], w["trades"])
        if added:
            save_config(cfg)
            save_signal({
                "type": "WHALE_FINDER_DISCOVERY",
                "wallet_label": f"{added} new whales",
                "details": {"count": added},
                "confidence": 0.8,
            })
        return added

    def run_full_scan(self, lookback_blocks: int = 500000) -> dict:
        candidates = self.scan_recent_activity(lookback_blocks)
        return {"candidates": candidates, "total_candidates": len(candidates)}
