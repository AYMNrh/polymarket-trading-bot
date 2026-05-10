"""
Orchestrator — ties together scanning, monitoring, strategy, order book analysis,
position tracking, self-learning, and dashboard.
Runs as a continuous background process.
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from config import load_config, save_config
from database import (
    save_trade, save_whale, save_signal, get_stats,
    get_recent_trades, get_whale_summary,
)
from whale_tracker import WhaleTracker
from whale_scanner import WhaleScanner
from strategy import StrategyEngine
from polymarket_clob import PolymarketClobClient
from orderbook_analyzer import analyze_order_book, order_book_to_signal
from self_learning import SelfLearningEngine
from onchain_decoder import OnChainTradeDecoder
from telegram_alerts import alert_conviction, alert_orderbook, alert_strategy, check_and_signal

ORDERBOOK_CACHE_PATH = Path(__file__).parent / "data" / "orderbook_cache.json"


class WhaleOrchestrator:
    """Central coordinator for all bot subsystems — now with 4-layer edge."""

    def __init__(self):
        self.cfg = load_config()
        self.tracker = WhaleTracker()
        self.scanner = WhaleScanner()
        self.clob = PolymarketClobClient(self.cfg.get("clob_api_endpoint"))
        self.strategy = StrategyEngine()
        self.strategy.clob = self.clob
        self.strategy.position_tracker = self.tracker.position_tracker
        self.learner = SelfLearningEngine()
        self.decoder = OnChainTradeDecoder(
            etherscan_key=self.cfg.get("polygonscan_api_key", "")
        )
        self._running = False
        self._stats_cache = {}
        self._order_book_cache = []

    def run_scan_cycle(self) -> int:
        """Run whale discovery scan. Returns number of new whales found."""
        whales = self.scanner.scan()
        new_count = 0
        existing = {w["address"].lower() for w in self.cfg["watched_wallets"]}

        for w in whales[:10]:
            addr = w["address"].lower()
            if addr not in existing:
                label = f"scan_{addr[:6]}"
                self.cfg["watched_wallets"].append({
                    "address": addr, "label": label,
                    "win_rate": None, "trades_tracked": 0,
                })
                existing.add(addr)
                new_count += 1
                save_whale({
                    "address": addr, "label": label,
                    "total_volume": w["total_volume"],
                    "num_trades": w["num_trades"],
                    "last_seen": w.get("last_seen", datetime.now(timezone.utc).isoformat()),
                })
                logger.info("New whale discovered: %s ($%.0f)", label, w["total_volume"])

        if new_count:
            save_config(self.cfg)
            save_signal({
                "type": "NEW_WHALE_DISCOVERY",
                "wallet_label": f"{new_count} new whales",
                "details": {"count": new_count},
                "confidence": 0.9,
            })

        return new_count

    def run_monitor_cycle(self) -> list[dict]:
        """Monitor known whales for new trades + position changes + conviction scoring."""
        trades = self.tracker.poll_once()

        for t in trades:
            save_trade(t)
            addr = t.get("address", "")
            label = t.get("wallet", t.get("wallet_label", ""))
            if addr:
                # Only update last_seen — don't overwrite total_volume/trades_tracked
                save_whale({
                    "address": addr, "label": label,
                    "last_seen": t.get("timestamp", datetime.now(timezone.utc).isoformat()),
                })

        # Multi-market positioning signals
        signals = self.tracker.detect_multi_market_positioning(trades)
        if signals:
            for label, s in signals.items():
                save_signal({
                    "type": "MULTI_MARKET_POSITIONING",
                    "wallet_label": label,
                    "details": s,
                    "confidence": 0.7,
                })

        # Load whale activity into strategy engine
        all_recent = get_recent_trades(100)
        self.strategy.load_whale_activity(all_recent)

        # Resolve market questions for trades without them (batch of 10 max)
        unresolved = [t for t in trades if len(t) > 0][:10]
        if unresolved:
            try:
                for t in unresolved:
                    tx_hash = t.get("tx_hash", "")
                    if not tx_hash:
                        continue
                    decoded = self.decoder.decode_transaction(tx_hash, wallet=t.get("address", ""))
                    questions = []
                    for d in decoded:
                        cid = d.get("condition_id", "")
                        if cid:
                            info = self.decoder.get_market_info(cid)
                            if info:
                                questions.append(info.get("question", "?"))
                    if questions:
                        q = "; ".join(set(q for q in questions if q != "?"))
                        t["market_question"] = q
                        save_trade(t)  # Updates via ON CONFLICT
            except Exception as e:
                logger.debug("Market question resolution skipped: %s", e)

        return trades

    def run_strategy_cycle(self) -> list[dict]:
        """Evaluate all weather markets with full 4-layer edge:
        1. Order book (walls, thin asks, skew)
        2. Whale conviction (adding/trimming/flipping)
        3. Forecast EV
        4. Combined score for allocation
        """
        markets = self.clob.get_weather_markets()
        if not markets:
            logger.warning("No weather markets from CLOB")
            return []

        signals = self.strategy.scan_all_markets(markets)
        portfolio = self.strategy.build_portfolio(signals, capital=1000)

        # Cache order book data for dashboard
        self._cache_order_books()

        if portfolio:
            logger.info("Strategy recommends %d positions:", len(portfolio))
            for p in portfolio:
                logger.info(
                    "  %s %s @ %.2f (conf: %.0f%%, alloc: $%.0f, EV: %.2f, book_score: %+.2f)",
                    p["city"], p["direction"], p["entry_price"],
                    p["confidence"] * 100, p["allocation"],
                    p["expected_value"],
                    p.get("order_book", {}).get("wall_score", 0),
                )

            save_signal({
                "type": "STRATEGY_PORTFOLIO",
                "wallet_label": "system",
                "details": {
                    "num_positions": len(portfolio),
                    "total_allocation": sum(p["allocation"] for p in portfolio),
                    "positions": portfolio,
                    "order_book_signals": {
                        p["city"]: p.get("order_book", {})
                        for p in portfolio[:5]
                        if p.get("order_book", {}).get("wall_score", 0) != 0
                    },
                },
                "confidence": portfolio[0]["confidence"] if portfolio else 0,
            })

            # Alert on high-confidence order book signals
            for p in portfolio[:3]:
                ob = p.get("order_book", {})
                if abs(ob.get("wall_score", 0)) > 0.5:
                    alert_orderbook(ob)

        return portfolio

    def run_learning_cycle(self) -> list[str]:
        """Run self-learning review on resolved trades.
        Returns new observations/adjustments made.
        """
        # Load all stored market data (from weather bot data/ dir)
        weather_data_dir = Path(__file__).parent.parent.parent / "weatherbot" / "data" / "markets"
        resolved_markets = []

        if weather_data_dir.exists():
            for f in sorted(weather_data_dir.glob("*.json"))[-50:]:
                try:
                    mkt = json.loads(f.read_text())
                    if mkt.get("status") == "resolved" and mkt.get("resolved_outcome"):
                        resolved_markets.append(mkt)
                except Exception:
                    continue

        if resolved_markets:
            observations = self.learner.review_resolved_trades(resolved_markets)
            if observations:
                logger.info("Self-learning: %d new observations", len(observations))
                alert_strategy(self.learner.strategy_report())
            return observations
        return []

    def run_alerts_cycle(self):
        """Check for new signals and send Telegram alerts."""
        sent = check_and_signal()
        if sent:
            logger.info("Sent %d Telegram alerts: %s", len(sent), sent)

    def _cache_order_books(self):
        """Persist order book analysis for dashboard."""
        cache = []
        for token_id, sig in self.strategy._order_book_cache.items():
            cache.append(order_book_to_signal(sig))
        self._order_book_cache = cache
        ORDERBOOK_CACHE_PATH.parent.mkdir(exist_ok=True)
        ORDERBOOK_CACHE_PATH.write_text(json.dumps(cache, indent=2))

    def run_dashboard(self, port: int = 9091):
        """Start the web dashboard in a background thread."""
        try:
            from dashboard import app
            import uvicorn
            cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
            server = uvicorn.Server(cfg)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            logger.info("Dashboard started on http://0.0.0.0:%d", port)
        except ImportError as e:
            logger.warning("Dashboard unavailable: %s", e)

    def full_cycle(self):
        """Run one complete cycle of all subsystems with 4-layer edge."""
        logger.info("=" * 50)
        logger.info("Starting full cycle — 4-layer edge")
        logger.info("=" * 50)

        # 1. Monitor known whales (Layer 2: position changes + conviction)
        try:
            trades = self.run_monitor_cycle()
            logger.info("Layer 2: Monitored %d wallet(s), %d new trade(s)",
                        len(self.cfg["watched_wallets"]), len(trades))
            # Check for conviction signals
            conviction_sigs = self.tracker.position_tracker.get_conviction_signals(
                min_score=0.7
            )
            if conviction_sigs:
                logger.info("Layer 2: %d high-conviction signals active",
                            len(conviction_sigs))
        except Exception as e:
            logger.error("Monitor failed: %s", e)

        # 2. Run strategy with order book analysis (Layer 1 + Layer 3)
        try:
            portfolio = self.run_strategy_cycle()
            if portfolio:
                logger.info("Layer 1+3: %d portfolio positions with order book analysis",
                            len(portfolio))
        except Exception as e:
            logger.error("Strategy failed: %s", e)

        # 3. Self-learning review (Layer 4)
        try:
            observations = self.run_learning_cycle()
            if observations:
                logger.info("Layer 4: %d self-learning observations", len(observations))
        except Exception as e:
            logger.error("Learning cycle failed: %s", e)

        # 4. Send alerts
        try:
            self.run_alerts_cycle()
        except Exception as e:
            logger.error("Alerts failed: %s", e)

        # 5. Scan for new whales (less frequent)
        try:
            new = self.run_scan_cycle()
            if new:
                logger.info("Found %d new whales", new)
        except Exception as e:
            logger.error("Scan failed: %s", e)

        logger.info("Cycle complete")
        logger.info("=" * 50)
