"""
Polymarket Profile Scraper — fetches whale portfolio data directly from Polymarket's
Next.js SSR page. No API key or auth needed.

URL: https://polymarket.com/profile/{address}?tab=activity
Extracts __NEXT_DATA__ → React Query cache → all available data points.

Extracted metrics per whale:
  - Positions (current portfolio): title, shares, entry, current, PnL, slug
  - Volume: total traded volume ($10.5M for ColdMath)
  - PnL: total realized + unrealized ($143K for ColdMath)
  - Trade count: 7,424 for ColdMath
  - Markets traded: unique market count
  - Join date + trade frequency
  - Biggest win: $12.4K for ColdMath
  - Portfolio value: $27.1K current
  - PnL history: time series (1D, 1W, 1M, ALL)
"""
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "data" / "whale_portfolios.json"
CACHE_TTL_SECONDS = 300  # 5 minutes


class PolymarketScraper:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        if CACHE_FILE.exists():
            try:
                return json.loads(CACHE_FILE.read_text())
            except (json.JSONDecodeError, Exception):
                pass
        return {}

    def _save_cache(self):
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(self._cache, indent=2, default=str))

    def _extract_queries(self, wallet_address: str) -> dict:
        """Scrape and extract all useful queries from __NEXT_DATA__."""
        url = f"https://polymarket.com/profile/{wallet_address}?tab=activity"
        logger.info("Scraping %s...", url[:60])

        try:
            r = self._session.get(url, timeout=15)
            if r.status_code != 200:
                logger.warning("Polymarket returned %d for %s", r.status_code, wallet_address[:10])
                return {}

            html = r.text
            match = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                html, re.DOTALL
            )
            if not match:
                logger.warning("No __NEXT_DATA__ for %s", wallet_address[:10])
                return {}

            data = json.loads(match.group(1))
            queries = data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
            return {"queries": queries, "raw_data": data}
        except Exception as e:
            logger.warning("Scrape failed for %s: %s", wallet_address[:10], e)
            return {}

    def get_positions(self, wallet_address: str, force_refresh: bool = False) -> list[dict]:
        """Get current Polymarket positions for a wallet."""
        addr = wallet_address.lower()

        if not force_refresh and addr in self._cache:
            entry = self._cache[addr]
            age = time.time() - entry.get("cached_at", 0)
            has_profile = bool(entry.get("profile", {}).get("trades"))
            if age < CACHE_TTL_SECONDS and has_profile:
                return entry.get("positions", [])

        # Scrape and extract everything
        result = self._extract_queries(addr)
        if not result:
            return self._cache.get(addr, {}).get("positions", [])

        queries = result["queries"]
        positions = []
        profile_data = {}

        for q in queries:
            qkey = q.get("queryKey", [])
            qkey_str = json.dumps(qkey)
            state = q.get("state", {})
            d = state.get("data")

            # Current positions
            if "positions" in qkey_str and "CURRENT" in qkey_str:
                pages = d.get("pages", []) if isinstance(d, dict) else []
                if pages and isinstance(pages[0], list):
                    for p in pages[0]:
                        positions.append({
                            "title": p.get("title", "?"),
                            "slug": p.get("slug", ""),
                            "event_slug": p.get("eventSlug", ""),
                            "side": "YES",
                            "shares": round(p.get("size", 0), 1),
                            "entry_price": round(p.get("avgPrice", 0), 4),
                            "current_price": round(p.get("curPrice", 0), 4),
                            "value": round(p.get("currentValue", 0), 2),
                            "initial_value": round(p.get("initialValue", 0), 2),
                            "pnl": round(p.get("cashPnl", 0), 2),
                            "pnl_pct": round(p.get("percentPnl", 0) * 100, 2),
                            "condition_id": p.get("conditionId", ""),
                            "asset": p.get("asset", ""),
                            "redeemable": p.get("redeemable", False),
                        })

            # Volume & PnL
            elif "/api/profile/volume" in qkey_str:
                if isinstance(d, dict):
                    profile_data["total_volume"] = round(d.get("amount", 0), 2)
                    profile_data["total_pnl"] = round(d.get("pnl", 0), 2)

            # User stats: trades, biggest win, join date
            elif qkey_str.startswith('["user-stats"'):
                if isinstance(d, dict):
                    profile_data["trades"] = d.get("trades", 0)
                    profile_data["biggest_win"] = round(d.get("largestWin", 0), 2)
                    profile_data["views"] = d.get("views", 0)
                    profile_data["join_date"] = d.get("joinDate", "")

            # Markets traded count
            elif "/api/profile/marketsTraded" in qkey_str:
                if isinstance(d, dict):
                    profile_data["markets_traded"] = d.get("traded", 0)

            # Current portfolio value
            elif qkey_str.startswith('["positions", "value"'):
                if isinstance(d, (int, float)):
                    profile_data["portfolio_value"] = round(d, 2)

            # PnL history (ALL time)
            elif "portfolio-pnl" in qkey_str and '"ALL"' in qkey_str:
                if isinstance(d, list) and len(d) > 0:
                    profile_data["pnl_history"] = d
                    # First data point = earliest PnL snapshot
                    first_pnl = d[0].get("p", 0) if isinstance(d[0], dict) else 0
                    profile_data["first_pnl"] = round(first_pnl, 2)
                    profile_data["first_pnl_ts"] = d[0].get("t", 0) if isinstance(d[0], dict) else 0

            # Biggest wins details
            elif "profile-biggest-wins" in qkey_str:
                if isinstance(d, dict) and "biggestWins" in d:
                    wins = d["biggestWins"]
                    if wins:
                        profile_data["biggest_win_market"] = wins[0].get("marketTitle", "?")
                        profile_data["biggest_win_value"] = round(
                            wins[0].get("finalValue", 0) - wins[0].get("initialValue", 0), 2
                        )

        # Compute derived metrics
        if profile_data.get("join_date"):
            try:
                join_ts = datetime.fromisoformat(
                    profile_data["join_date"].replace("Z", "+00:00")
                )
                days_active = max(1, (datetime.now(timezone.utc) - join_ts).days)
                profile_data["days_active"] = days_active
                profile_data["trades_per_day"] = round(
                    profile_data.get("trades", 0) / days_active, 1
                )
            except Exception:
                pass

        # Save to cache
        self._cache[addr] = {
            "positions": positions,
            "profile": profile_data,
            "cached_at": time.time(),
            "address": addr,
        }
        self._save_cache()

        logger.info("Cached %d positions + profile for %s", len(positions), addr[:10])
        return positions

    def get_profile(self, wallet_address: str) -> dict:
        """Get whale profile data (stats, not positions)."""
        addr = wallet_address.lower()
        # Ensure cache is populated
        self.get_positions(addr)
        if addr in self._cache:
            return self._cache[addr].get("profile", {})
        return {}

    def portfolio_summary(self, wallet_address: str) -> dict:
        """Compute summary stats for a wallet's portfolio."""
        positions = self.get_positions(wallet_address)
        profile = self.get_profile(wallet_address)

        if not positions:
            return {
                "positions": 0, "value": 0, "pnl": 0, "total_shares": 0,
                "trades": profile.get("trades", 0),
                "total_volume": profile.get("total_volume", 0),
                "total_pnl": profile.get("total_pnl", 0),
                "biggest_win": profile.get("biggest_win", 0),
                "days_active": profile.get("days_active", 0),
                "trades_per_day": profile.get("trades_per_day", 0),
                "join_date": profile.get("join_date", ""),
                "markets_traded": profile.get("markets_traded", 0),
            }

        total_value = sum(p.get("value", 0) for p in positions)
        total_pnl_positions = sum(p.get("pnl", 0) for p in positions)
        total_shares = sum(p.get("shares", 0) for p in positions)

        return {
            "positions": len(positions),
            "value": round(total_value, 2),
            "position_pnl": round(total_pnl_positions, 2),
            "total_shares": round(total_shares, 1),
            "avg_pnl_pct": round(total_pnl_positions / total_value * 100, 1) if total_value > 0 else 0,
            # From profile
            "trades": profile.get("trades", 0),
            "total_volume": profile.get("total_volume", 0),
            "total_pnl": profile.get("total_pnl", 0),
            "biggest_win": profile.get("biggest_win", 0),
            "biggest_win_market": profile.get("biggest_win_market", "?"),
            "days_active": profile.get("days_active", 0),
            "trades_per_day": profile.get("trades_per_day", 0),
            "join_date": profile.get("join_date", ""),
            "markets_traded": profile.get("markets_traded", 0),
            "portfolio_value": profile.get("portfolio_value", total_value),
            "first_pnl": profile.get("first_pnl", 0),
        }

    def get_all_portfolios(self, wallets: list[dict], force_refresh: bool = False) -> dict[str, list[dict]]:
        """Get portfolios for all watched wallets."""
        result = {}
        for w in wallets:
            addr = w.get("address", "")
            if not addr:
                continue
            pos = self.get_positions(addr, force_refresh)
            if pos:
                result[addr] = pos
            time.sleep(0.5)
        return result
