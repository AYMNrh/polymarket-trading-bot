"""
Self-Learning Loop — reviews resolved trades, identifies what worked,
writes strategy notes, and adjusts parameters for next round.

Runs after trade resolution:
1. Load all recently-resolved markets
2. Compare forecast vs actual → compute error
3. Identify win/loss patterns by bucket type, city, source
4. Write strategy notes (what worked, what didn't)
5. Adjust config parameters (min_ev, max_price, kelly_fraction)
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STRATEGY_NOTES_FILE = Path(__file__).parent / "data" / "strategy_notes.json"
ADJUSTMENT_LOG = Path(__file__).parent / "data" / "parameter_adjustments.jsonl"

DEFAULT_PARAMS = {
    "min_ev": 0.05,
    "max_price": 0.45,
    "kelly_fraction": 0.25,
    "min_volume": 200,
    "max_bet": 20.0,
}


class SelfLearningEngine:
    """
    After every resolved trade (or batch at end of day), review performance
    and adjust strategy.

    Strategy notes are persisted as structured observations:
    {
      "date": "2026-05-08",
      "observations": [
        "Tail buckets at $0.05-0.15: 5W/0L (100%) — prioritize at any price",
        "Single-degree NYC >$0.10 entry: 2W/8L (20%) — avoid, raise min_ev for these",
        "HRRR D+1 forecast: 0.8°F avg error — tighten sigma from 2.0 to 1.6",
        "ECMWF D+3 forecast: 2.4°F avg error — widen sigma to 2.5",
      ],
      "parameter_adjustments": {
        "min_ev": 0.08,         // raised from 0.05 after losses on marginal EV
        "kelly_fraction": 0.20  // reduced after volatility
      },
      "performance_today": {
        "trades": 4,
        "wins": 3,
        "losses": 1,
        "pnl": 12.50
      }
    }
    """

    def __init__(self):
        self.notes_file = STRATEGY_NOTES_FILE
        self.notes_file.parent.mkdir(exist_ok=True)
        self.notes = self._load_notes()

    def _load_notes(self) -> dict:
        if self.notes_file.exists():
            try:
                return json.loads(self.notes_file.read_text())
            except (json.JSONDecodeError, Exception):
                pass
        return {
            "version": 1,
            "last_review": None,
            "observations": [],
            "parameter_adjustments": dict(DEFAULT_PARAMS),
            "performance_history": [],
            "pattern_notes": {},
        }

    def _save_notes(self):
        self.notes_file.write_text(json.dumps(self.notes, indent=2, default=str))

    def review_resolved_trades(self, resolved_markets: list[dict]) -> list[str]:
        """
        Review a batch of resolved markets and generate improvement notes.
        Returns a list of new observations written.
        """
        new_observations = []

        if not resolved_markets:
            return new_observations

        wins = [m for m in resolved_markets if m.get("resolved_outcome") == "win"]
        losses = [m for m in resolved_markets if m.get("resolved_outcome") == "loss"]

        logger.info("📝 Reviewing %d resolved markets (%dW/%dL)",
                     len(resolved_markets), len(wins), len(losses))

        # --- Pattern 1: Performance by bucket type ---
        buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0})
        for m in resolved_markets:
            btype = self._bucket_type(m)
            buckets[btype]["count"] += 1
            pnl = m.get("pnl", 0) or 0
            if m.get("resolved_outcome") == "win":
                buckets[btype]["wins"] += 1
            elif m.get("resolved_outcome") == "loss":
                buckets[btype]["losses"] += 1
            buckets[btype]["pnl"] += pnl

        for btype, perf in sorted(buckets.items()):
            if perf["count"] < 3:
                continue
            wr = perf["wins"] / (perf["wins"] + perf["losses"]) * 100 if (perf["wins"] + perf["losses"]) > 0 else 0
            obs = (
                f"{btype} trades: {perf['count']} total, "
                f"{perf['wins']}W/{perf['losses']}L ({wr:.0f}%), "
                f"PnL ${perf['pnl']:.2f}"
            )
            new_observations.append(obs)
            self.notes.setdefault("pattern_notes", {}).setdefault(btype, []).append(obs)
            logger.info("  📊 %s", obs)

            # Auto-adjust: if a bucket type is consistently losing, suggest avoiding
            if wr < 30 and perf["count"] >= 5:
                adj = (
                    f"AVOID {btype} — {wr:.0f}% WR over {perf['count']} trades. "
                    f"Either raise min_ev or skip this bucket type."
                )
                new_observations.append(adj)
                logger.info("  ⚠️  %s", adj)
            elif wr > 70 and perf["count"] >= 5:
                adj = (
                    f"PRIORITIZE {btype} — {wr:.0f}% WR over {perf['count']} trades. "
                    f"Consider higher allocation for this pattern."
                )
                new_observations.append(adj)
                logger.info("  ✅ %s", adj)

        # --- Pattern 2: Forecast source accuracy ---
        sources = defaultdict(lambda: {"count": 0, "total_error": 0.0})
        for m in resolved_markets:
            for snap in reversed(m.get("forecast_snapshots", [])):
                source = snap.get("best_source", snap.get("source", "unknown"))
                forecast = snap.get("temp", snap.get("best"))
                actual = m.get("actual_temp")
                if forecast is not None and actual is not None:
                    sources[source]["count"] += 1
                    sources[source]["total_error"] += abs(float(forecast) - float(actual))
                    break  # Use most recent forecast snapshot

        for source, perf in sorted(sources.items()):
            if perf["count"] < 3:
                continue
            mae = perf["total_error"] / perf["count"]
            obs = f"{source} forecast MAE: {mae:.1f}°F ({perf['count']} samples)"
            new_observations.append(obs)
            self.notes.setdefault("pattern_notes", {}).setdefault("forecast_accuracy", []).append(obs)
            logger.info("  🌡️  %s", obs)

        # --- Parameter Adjustment Logic ---
        adjustments = self._adjust_parameters(wins, losses, resolved_markets)
        if adjustments:
            new_observations.extend(adjustments)
            self.notes["parameter_adjustments"].update(adjustments)

        # --- Save ---
        total_pnl = sum(m.get("pnl", 0) or 0 for m in resolved_markets)
        self.notes.setdefault("performance_history", []).append({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "trades": len(resolved_markets),
            "wins": len(wins),
            "losses": len(losses),
            "pnl": round(total_pnl, 2),
            "observations": new_observations,
        })
        self.notes["last_review"] = datetime.now(timezone.utc).isoformat()
        self.notes["observations"].extend(new_observations)
        self._save_notes()

        return new_observations

    def _adjust_parameters(self, wins: list, losses: list, all_markets: list) -> dict:
        """Adjust strategy parameters based on results."""
        adjustments = {}
        total = len(wins) + len(losses)
        if total < 5:
            return adjustments

        win_rate = len(wins) / total

        # If win rate is high, we can be more aggressive
        if win_rate > 0.65:
            current_kelly = self.notes.get("parameter_adjustments", {}).get("kelly_fraction", 0.25)
            if current_kelly < 0.35:
                adjustments["kelly_fraction"] = round(min(0.35, current_kelly + 0.05), 2)
                logger.info("  📈 Raising kelly_fraction to %.2f (WR=%.0f%%)",
                            adjustments["kelly_fraction"], win_rate * 100)

        # If win rate is low, tighten filters
        if win_rate < 0.35:
            current_min_ev = self.notes.get("parameter_adjustments", {}).get("min_ev", 0.05)
            if current_min_ev < 0.12:
                adjustments["min_ev"] = round(min(0.12, current_min_ev + 0.03), 3)
                logger.info("  📉 Raising min_ev to %.3f (WR=%.0f%%)",
                            adjustments["min_ev"], win_rate * 100)

        # If losses are concentrated on expensive entries, lower max_price
        expensive_losses = [m for m in losses
                            if (m.get("market_snapshots") or [{}])[-1].get("entry_price", 1) > 0.30]
        if len(expensive_losses) >= 3 and len(expensive_losses) / max(1, len(losses)) > 0.5:
            current_max = self.notes.get("parameter_adjustments", {}).get("max_price", 0.45)
            if current_max > 0.30:
                adjustments["max_price"] = round(max(0.25, current_max - 0.05), 2)
                logger.info("  📉 Lowering max_price to %.2f (too many expensive losses)",
                            adjustments["max_price"])

        return adjustments

    def get_recommendations(self) -> list[str]:
        """Get current strategy recommendations from past observations."""
        recs = []
        for btype, notes in self.notes.get("pattern_notes", {}).items():
            if isinstance(notes, list):
                for n in notes[-3:]:
                    if "AVOID" in n or "PRIORITIZE" in n:
                        recs.append(n)
        return recs

    def strategy_report(self) -> str:
        """Full human-readable strategy report."""
        lines = []
        lines.append("🧠 Self-Learning Strategy Report")
        lines.append("=" * 50)

        params = self.notes.get("parameter_adjustments", {})
        lines.append(f"\nCurrent Parameters:")
        for k, v in sorted(params.items()):
            default = DEFAULT_PARAMS.get(k, "?")
            delta = f" ({'up' if v > default else 'down'} from {default})" if v != default else ""
            lines.append(f"  {k:20s} = {v}{delta}")

        perf = self.notes.get("performance_history", [])
        if perf:
            last_3 = perf[-3:]
            lines.append(f"\nLast {len(last_3)} Reviews:")
            for p in last_3:
                lines.append(
                    f"  {p['date']}: {p['trades']} trades ({p['wins']}W/{p['losses']}L) "
                    f"PnL ${p['pnl']:.2f}"
                )

        recs = self.get_recommendations()
        if recs:
            lines.append(f"\nActive Recommendations:")
            for r in recs[-5:]:
                lines.append(f"  • {r}")

        notes = self.notes.get("observations", [])
        if notes:
            lines.append(f"\nRecent Observations ({len(notes)} total):")
            for n in notes[-8:]:
                lines.append(f"  • {n}")

        lines.append(f"\nLast Review: {self.notes.get('last_review', 'Never')}")
        return "\n".join(lines)

    @staticmethod
    def _bucket_type(market: dict) -> str:
        """Classify a market by its bucket type."""
        outcomes = market.get("all_outcomes", [])
        if not outcomes:
            return "unknown"

        # Find the outcome that was traded
        traded = None
        for o in outcomes:
            if market.get("position") and o.get("market_id") == str(market["position"].get("market_id")):
                traded = o
                break

        if not traded:
            return "unknown"

        t_low, t_high = traded.get("range", (0, 0))
        if t_low == -999:
            return "tail_below"
        elif t_high == 999:
            return "tail_above"
        elif t_high - t_low <= 1:
            return "single_degree"
        elif t_high - t_low <= 5:
            return "narrow_range"
        else:
            return "wide_range"
