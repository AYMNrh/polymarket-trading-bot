"""
Strategy Extractor — derives trading strategy patterns from whale behavior
without needing per-trade market decoding.

Works with what we have:
- USDC flow to/from CTF Exchange (buy/sell timing + volume)
- CLOB weather market listings (available markets)
- Pattern matching by timing correlation
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median

logger = logging.getLogger(__name__)

CTF_CONTRACTS = {
    "0x4bfb41d5b3570c1c6cbb5e7cb3e8d9a0b0a0b0c0": "CTF Exchange",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a": "NegRiskCTF",
}


class StrategyExtractor:
    """Extracts trading strategies from whale on-chain data."""

    def __init__(self, clob_client=None):
        self.clob = clob_client
        self.weather_markets = []

    def load_weather_markets(self):
        """Fetch all available weather markets from CLOB."""
        if not self.clob:
            logger.warning("No CLOB client available")
            return []
        markets = self.clob.get_weather_markets()
        self.weather_markets = markets or []
        logger.info("Loaded %d weather markets", len(self.weather_markets))
        return self.weather_markets

    def analyze_timing_patterns(self, trades: list[dict]) -> dict:
        """
        Analyze when whales trade — time of day, day of week, frequency.
        """
        if not trades:
            return {}

        timestamps = []
        for t in trades:
            ts = t.get("timestamp", "")
            if ts and "T" in str(ts):
                try:
                    timestamps.append(datetime.fromisoformat(ts))
                except (ValueError, TypeError):
                    pass

        if not timestamps:
            return {}

        hours = [dt.hour for dt in timestamps if dt]
        weekdays = [dt.weekday() for dt in timestamps if dt]

        return {
            "total_sessions": len(timestamps),
            "avg_hour": round(mean(hours), 1),
            "most_active_hour": max(set(hours), key=hours.count) if hours else None,
            "most_active_day": max(set(weekdays), key=weekdays.count) if weekdays else None,
            "weekend_ratio": round(sum(1 for w in weekdays if w >= 5) / len(weekdays), 2) if weekdays else 0,
        }

    def analyze_position_sizing(self, trades: list[dict]) -> dict:
        """
        Analyze position sizes — avg trade, variance, max/min.
        Detects if they use fixed sizing or variable.
        """
        values = [float(t.get("value", 0)) for t in trades if float(t.get("value", 0)) > 0]
        if not values:
            return {}

        return {
            "avg_position": round(mean(values), 2),
            "median_position": round(median(values), 2),
            "min_position": round(min(values), 2),
            "max_position": round(max(values), 2),
            "std_dev": round((sum((v - mean(values)) ** 2 for v in values) / len(values)) ** 0.5, 2),
            "strategy_type": "fixed" if max(values) / min(values) < 2 else "variable",
        }

    def analyze_direction_bias(self, trades: list[dict]) -> dict:
        """
        Analyze buy vs sell bias.
        ColdMath is 37x buys:sells — aggressive accumulator.
        """
        buys = sum(1 for t in trades if t.get("direction", "").upper() == "BUY")
        sells = sum(1 for t in trades if t.get("direction", "").upper() == "SELL")
        total_trades = buys + sells

        if total_trades == 0:
            return {}

        return {
            "buy_ratio": round(buys / total_trades, 3),
            "sell_ratio": round(sells / total_trades, 3),
            "buy_sell_ratio": round(buys / sells, 2) if sells else float("inf"),
            "bias": "accumulator" if buys > sells * 2 else (
                     "distributor" if sells > buys * 2 else "balanced"),
        }

    def estimate_market_preferences(self, trades: list[dict]) -> dict:
        """
        Estimate which markets the whale prefers by analyzing their
        CTF contract usage and timing patterns.
        
        ColdMath sends USDC to both:
        - 0x4bfb41d5 (CTF Exchange) 
        - 0xc5d563a3 (NegRiskCTF)
        
        NegRiskCTF = neg-risk markets (weather with multiple outcomes)
        CTF Exchange = standard markets
        """
        neg_risk_count = 0
        standard_count = 0
        for t in trades:
            contract = t.get("contract", "")
            if "c5d563a3" in str(contract).lower():
                neg_risk_count += 1
            elif "4bfb41d5" in str(contract).lower():
                standard_count += 1

        return {
            "neg_risk_trades": neg_risk_count,
            "standard_trades": standard_count,
            "preferred_contract": "NegRisk" if neg_risk_count > standard_count else "Standard",
        }

    def extract_strategy(self, whale_label: str, trades: list[dict]) -> dict:
        """
        Full strategy extraction for a whale.
        Combines all analyses into a readable strategy profile.
        """
        timing = self.analyze_timing_patterns(trades)
        sizing = self.analyze_position_sizing(trades)
        bias = self.analyze_direction_bias(trades)
        preferences = self.estimate_market_preferences(trades)

        strategy = {
            "whale": whale_label,
            "trades_analyzed": len(trades),
            "profile": {
                "style": bias.get("bias", "unknown"),
                "avg_trade": sizing.get("avg_position", 0),
                "volume_range": f"${sizing.get('min_position',0)}-${sizing.get('max_position',0)}",
                "preferred_contract": preferences.get("preferred_contract", "unknown"),
                "most_active_hour": f"{timing.get('most_active_hour',0):02d}:00" if timing else "unknown",
                "sizing_style": sizing.get("strategy_type", "unknown"),
            },
            "signals": [],
            "confidence": 0.0,
        }

        # Generate actionable signals
        if bias.get("bias") == "accumulator":
            strategy["signals"].append(
                f"{whale_label} is an accumulator ({bias.get('buy_sell_ratio',0)}x buys:sells) — "
                f"follow their buys, fade their sells"
            )
            strategy["confidence"] += 0.3

        if sizing.get("strategy_type") == "fixed":
            strategy["signals"].append(
                f"{whale_label} uses fixed position sizing (~${sizing.get('median_position',0)}) — "
                f"consistent risk management"
            )
            strategy["confidence"] += 0.2
        else:
            strategy["signals"].append(
                f"{whale_label} uses variable sizing (${sizing.get('min_position',0)}-${sizing.get('max_position',0)}) — "
                f"opportunistic"
            )

        if preferences.get("preferred_contract") == "NegRisk":
            strategy["signals"].append(
                f"{whale_label} prefers NegRisk markets (weather with multiple outcomes) — "
                f"likely trading temperature ranges across cities"
            )
            strategy["confidence"] += 0.2

        if timing:
            strategy["signals"].append(
                f"{whale_label} most active around {timing.get('most_active_hour',0):02d}:00 UTC"
            )

        strategy["confidence"] = min(strategy["confidence"], 1.0)
        return strategy

    def compare_whales(self, whale_strategies: list[dict]) -> dict:
        """
        Compare strategies across multiple whales to find consensus signals.
        When multiple whales act similarly, confidence increases.
        """
        if not whale_strategies:
            return {}

        avg_conf = mean(s.get("confidence", 0) for s in whale_strategies)
        accumulators = [s for s in whale_strategies if s.get("profile", {}).get("style") == "accumulator"]
        neg_risk = [s for s in whale_strategies if s.get("profile", {}).get("preferred_contract") == "NegRisk"]

        signals = []
        if len(accumulators) >= 2:
            signals.append(
                f"Multiple whales are accumulating ({len(accumulators)}/{len(whale_strategies)}) — "
                f"bullish signal for weather markets"
            )

        return {
            "whales_analyzed": len(whale_strategies),
            "accumulators": len(accumulators),
            "consensus_confidence": round(avg_conf, 2),
            "signals": signals,
        }
