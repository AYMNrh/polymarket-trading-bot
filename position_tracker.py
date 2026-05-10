"""
Position Change Tracker — monitors whale positions over time and detects:

- ADDING → conviction rising (same side, increasing position)
- TRIMMING → doubt creeping in (reducing existing position)
- FLIPPING → they know something (full reversal)
- HOLDING → no change (neutral)

Every trade is compared to the whale's historical position in that token.
Conviction scores are computed from the pattern of changes.
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

POSITION_TRACK_FILE = Path(__file__).parent / "whale_positions.json"

# Minimum trades before we compute conviction
MIN_TRADES_FOR_CONVICTION = 3
# How far back to look for recent trend (hours)
RECENT_WINDOW_HOURS = 48

# Conviction thresholds
STRONG_CONVICTION = 0.8
MODERATE_CONVICTION = 0.5
WEAK_CONVICTION = 0.2


def _load_positions() -> dict:
    """Load persisted position state keyed by (wallet_address, contract_address)."""
    if POSITION_TRACK_FILE.exists():
        try:
            return json.loads(POSITION_TRACK_FILE.read_text())
        except (json.JSONDecodeError, Exception):
            return {}
    return {}


def _save_positions(data: dict):
    POSITION_TRACK_FILE.write_text(json.dumps(data, indent=2, default=str))


class PositionTracker:
    """
    Tracks per-whale, per-market positions and detects changes in conviction.

    State structure:
    {
      "wallet_address:contract_address": {
        "wallet_label": "ColdMath",
        "contract": "0x...",
        "direction": "BUY" | "SELL",    # current net direction
        "net_size": 150.0,              # cumulative USDC
        "num_trades": 5,
        "last_trade": "2026-05-08T...",
        "first_trade": "2026-05-07T...",
        "history": [
          {"ts": "2026-05-08T...", "direction": "BUY", "value": 50.0},
          ...
        ],
        "conviction": 0.0,              # current conviction score
        "conviction_trend": "neutral"    # rising | falling | neutral | flipping
      }
    }
    """

    def __init__(self):
        self.positions = _load_positions()

    def process_trade(self, trade: dict):
        """
        Process a single whale trade and update conviction.
        Returns a signal dict if conviction changed significantly.
        """
        wallet = trade.get("wallet", trade.get("wallet_label", ""))
        address = trade.get("address", "").lower()
        contract = trade.get("contract", "").lower()
        direction = trade.get("direction", "BUY").upper()
        value = float(trade.get("value", 0))
        ts = trade.get("timestamp", datetime.now(timezone.utc).isoformat())

        if not contract or not address:
            return None

        key = f"{address}:{contract}"
        pos = self.positions.get(key)

        if pos is None:
            # New position
            pos = {
                "wallet_label": wallet,
                "address": address,
                "contract": contract,
                "direction": direction,
                "net_size": value,
                "num_trades": 1,
                "last_trade": ts,
                "first_trade": ts,
                "history": [
                    {"ts": ts, "direction": direction, "value": value}
                ],
                "conviction": 0.3,
                "conviction_trend": "neutral",
            }
            self.positions[key] = pos
            logger.info("🔵 NEW POSITION: %s in %s ($%.0f %s)", wallet, contract[:10], value, direction)
            return None

        # Track history
        pos["history"].append({"ts": ts, "direction": direction, "value": value})
        pos["num_trades"] += 1
        pos["last_trade"] = ts

        # Detect the type of change
        prev_direction = pos["direction"]
        prev_net = pos["net_size"]

        # Flipping: direction changed
        if direction != prev_direction:
            if direction == "SELL" and value >= prev_net * 0.5:
                # Selling >= 50% of current position = trimming / exiting
                pos["direction"] = "SELL"
                pos["net_size"] = max(0, prev_net - value)
                signal = self._gen_signal(wallet, contract, "TRIMMING",
                                          f"Sold ${value:.0f} of ${prev_net:.0f} position", 0.6)
                logger.info("✂️ TRIMMING: %s reducing %s (sold $%.0f of $%.0f)",
                            wallet, contract[:10], value, prev_net)
                _save_positions(self.positions)
                return signal

            elif direction == "BUY" and prev_direction == "SELL":
                # Flip from selling to buying
                pos["direction"] = "BUY"
                pos["net_size"] = value
                signal = self._gen_signal(wallet, contract, "FLIPPING",
                                          f"Flipped from SELL to BUY ($%.0f)" % value, 0.9)
                logger.info("🔄 FLIPPING: %s flipped %s ($%.0f)", wallet, contract[:10], value)
                _save_positions(self.positions)
                return signal

        # Adding (same direction, increasing position)
        if direction == prev_direction:
            pos["net_size"] += value
            pos["direction"] = direction

            # Rising conviction: multiple adds in same direction
            recent = [h for h in pos["history"][-5:]
                      if h["direction"] == direction]
            if len(recent) >= 3:
                pos["conviction"] = min(0.95, pos["conviction"] + 0.1)
                pos["conviction_trend"] = "rising"
                signal = self._gen_signal(wallet, contract, "CONVICTION_RISING",
                                          f"Added ${value:.0f} ({len(recent)} adds in a row)", 0.8)
                logger.info("📈 CONVICTION RISING: %s adding %s ($%.0f, conf: %.0f%%)",
                            wallet, contract[:10], pos["net_size"], pos["conviction"] * 100)
                _save_positions(self.positions)
                return signal
            elif len(recent) >= 2:
                pos["conviction"] = min(0.9, pos["conviction"] + 0.05)
                pos["conviction_trend"] = "rising"
            else:
                pos["conviction"] = max(0.1, pos["conviction"])
                pos["conviction_trend"] = "neutral"

        # Check for doubt (dwindling adds or small position relative to usual)
        if pos["num_trades"] >= MIN_TRADES_FOR_CONVICTION:
            recent_trades = pos["history"][-MIN_TRADES_FOR_CONVICTION:]
            avg_size = sum(t["value"] for t in recent_trades) / len(recent_trades)
            if value < avg_size * 0.3 and direction == prev_direction:
                pos["conviction"] = max(0.1, pos["conviction"] - 0.15)
                pos["conviction_trend"] = "falling"

        _save_positions(self.positions)
        return None

    def get_whale_positions(self, address: str = None) -> list[dict]:
        """Get all tracked positions, optionally filtered by wallet address."""
        result = []
        for key, pos in self.positions.items():
            if address and pos.get("address", "").lower() != address.lower():
                continue
            # Compute trend label
            trend = self._compute_trend(pos)
            result.append({
                "wallet": pos.get("wallet_label", "?"),
                "address": pos.get("address", ""),
                "contract": pos.get("contract", ""),
                "direction": pos.get("direction", "?"),
                "net_size": pos.get("net_size", 0),
                "num_trades": pos.get("num_trades", 0),
                "conviction": round(pos.get("conviction", 0), 2),
                "conviction_trend": trend,
                "last_trade": pos.get("last_trade", ""),
            })
        result.sort(key=lambda p: -p["net_size"])
        return result

    def get_conviction_signals(self, min_score: float = 0.6) -> list[dict]:
        """Get all positions with conviction above threshold."""
        return [
            p for p in self.get_whale_positions()
            if p["conviction"] >= min_score
        ]

    def summary(self) -> str:
        """Human-readable summary of all positions and conviction."""
        lines = []
        lines.append("📊 Position Tracker Summary")
        lines.append("=" * 50)

        positions = self.get_whale_positions()
        if not positions:
            return "No positions tracked yet."

        high_conviction = [p for p in positions if p["conviction"] >= MODERATE_CONVICTION]
        flips = [p for p in positions if p["conviction_trend"] in ("flipping", "rising")]

        lines.append(f"  Total positions tracked: {len(positions)}")
        lines.append(f"  High conviction (>{MODERATE_CONVICTION*100:.0f}%): {len(high_conviction)}")
        lines.append(f"  Active flips/signals: {len(flips)}")
        lines.append("")

        for p in positions[:10]:
            trend_icon = {
                "rising": "📈", "falling": "📉",
                "flipping": "🔄", "neutral": "➖"
            }.get(p["conviction_trend"], "➖")
            lines.append(
                f"  {trend_icon} {p['wallet']:15s} {p['direction']:4s} "
                f"${p['net_size']:>6.0f}  conf:{p['conviction']*100:>3.0f}%  "
                f"{p['conviction_trend']:>8s}  {p['contract'][:10]}..."
            )

        return "\n".join(lines)

    def _gen_signal(self, wallet: str, contract: str,
                    signal_type: str, details: str,
                    confidence: float) -> dict:
        return {
            "type": signal_type,
            "wallet_label": wallet,
            "details": details,
            "confidence": confidence,
            "contract": contract,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _compute_trend(self, pos: dict) -> str:
        """Compute overall trend from position history."""
        if not pos.get("history"):
            return "neutral"

        # Check for flip
        directions = set(h["direction"] for h in pos["history"][-3:])
        if len(directions) > 1:
            return "flipping"

        # Check recent conviction changes
        recent_values = [h["value"] for h in pos["history"][-5:]]
        if len(recent_values) >= 3:
            avg_first = sum(recent_values[:2]) / 2
            avg_last = sum(recent_values[-2:]) / 2
            if avg_last > avg_first * 1.3:
                return "rising"
            elif avg_last < avg_first * 0.7:
                return "falling"
        return pos.get("conviction_trend", "neutral")
