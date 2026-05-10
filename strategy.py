"""
Multi-market strategy engine — monitors weather markets across all cities,
computes EV from forecast vs market price, cross-references whale activity,
and generates trade signals.

Integrated layers:
1. Forecast-based EV (weather data vs market price)
2. Order book analysis (walls, thin asks, skew)
3. Whale conviction signals (adding/trimming/flipping)
4. Portfolio allocation across cities

Strategy: spread across multiple cities/subjects simultaneously,
following whale positioning patterns with order book confirmation.
"""
import json
import logging
import math
import re
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Forecast cache (avoids repeated API calls within a cycle) ──────────────
_FORECAST_CACHE = {}  # key: (city_slug, date_str) -> dict with temp, sigma, ts

# ── Open-Meteo city coordinates (lat, lon) ────────────────────────────────
CITY_COORDS = {
    "new-york":         (40.7128, -74.0060),
    "chicago":          (41.8781, -87.6298),
    "los-angeles":      (34.0522, -118.2437),
    "miami":            (25.7617, -80.1918),
    "houston":          (29.7604, -95.3698),
    "phoenix":          (33.4484, -112.0740),
    "denver":           (39.7392, -104.9903),
    "seattle":          (47.6062, -122.3321),
    "boston":           (42.3601, -71.0589),
    "dallas":           (32.7767, -96.7970),
    "san-francisco":    (37.7749, -122.4194),
    "washington-dc":    (38.9072, -77.0369),
    "philadelphia":     (39.9526, -75.1652),
    "atlanta":          (33.7490, -84.3880),
    "london":           (51.5074, -0.1278),
    "tokyo":            (35.6762, 139.6503),
    "paris":            (48.8566, 2.3522),
    "berlin":           (52.5200, 13.4050),
    "sydney":           (-33.8688, 151.2093),
}

# Forecast standard deviation (degrees F) — typical ECMWF error at 1-3 days
FORECAST_SIGMA_F = 4.0


def _norm_cdf(x: float) -> float:
    """Standard normal CDF — probability that Z ≤ x."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

from orderbook_analyzer import analyze_order_book, OrderBookSignal, order_book_to_signal

# Weather cities to monitor (expandable)
WEATHER_CITIES = [
    "new-york", "chicago", "los-angeles", "miami", "houston",
    "phoenix", "denver", "seattle", "boston", "dallas",
    "san-francisco", "washington-dc", "philadelphia", "atlanta",
    "london", "tokyo", "paris", "berlin", "sydney",
]

# Temperature thresholds that Polymarket uses
TEMP_THRESHOLDS = [70, 75, 80, 85, 90, 95, 100]


class StrategyEngine:
    """Core strategy: 4-layer edge computation."""

    def __init__(self):
        self.clob = None  # Set externally
        self.whale_data = None  # Set externally
        self.position_tracker = None  # Set externally
        self.positions = {}  # token_id -> position info
        self.performance = {"wins": 0, "losses": 0, "pnl": 0.0}
        self._order_book_cache = {}  # token_id -> OrderBookSignal (per cycle)

    def load_whale_activity(self, recent_trades: list[dict]):
        """Load recent whale trades for signal confirmation."""
        self.whale_data = defaultdict(list)
        for t in recent_trades:
            label = t.get("wallet", t.get("wallet_label", ""))
            self.whale_data[label].append(t)

    def evaluate_market(self, market: dict) -> dict | None:
        """
        Evaluate a single weather market using all 4 layers.
        Uses Gamma API data: question, bestBid/bestAsk, conditionId, clobTokenIds.

        Layer 1: Order book (spread, bestBid/bestAsk analysis)
        Layer 2: Whale conviction (adding/trimming/flipping)
        Layer 3: Fair price vs market price (EV)
        Layer 4: Combined score
        """
        title = market.get("question", market.get("title", ""))
        condition_id = market.get("conditionId", market.get("condition_id", ""))
        clob_token_ids = market.get("clobTokenIds", [])
        outcomes = market.get("outcomes", [])

        # Find YES token index (Polymarket convention: YES = index 0, NO = index 1)
        if outcomes:
            try:
                yes_idx = outcomes.index("Yes")
            except ValueError:
                yes_idx = 0  # Fallback to first token
        else:
            yes_idx = 0

        token_id = (
            clob_token_ids[yes_idx]
            if clob_token_ids and yes_idx < len(clob_token_ids) else ""
        )

        # Extract city and temperature from title
        city = self._extract_city(title)
        temp = self._extract_temperature(title)
        if not city or not temp:
            return None

        result = {
            "city": city,
            "title": title,
            "condition_id": condition_id,
            "token_id": token_id or "",
            "current_price": None,
            "fair_price": None,
            "ev": None,
            "whale_signal": None,
            "confidence": 0.0,
            "direction": None,
            "order_book": None,
            "conviction": None,
            "combined_score": 0.0,
        }

        if not self.clob:
            return None

        # Get prices from Gamma API (bestBid/bestAsk)
        best_bid = market.get("bestBid")
        best_ask = market.get("bestAsk")
        last_trade = market.get("lastTradePrice")
        gamma_spread = market.get("spread")
        outcome_prices = market.get("outcomePrices", "[]")

        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except (json.JSONDecodeError, TypeError):
                outcome_prices = []

        yes_price = (
            float(outcome_prices[yes_idx])
            if outcome_prices and yes_idx < len(outcome_prices) else None
        )

        # Use best bid as current price (conservative: what you can sell at)
        if best_bid is not None:
            current_price = float(best_bid)
        elif last_trade is not None:
            current_price = float(last_trade)
        elif yes_price is not None:
            current_price = yes_price
        else:
            # Try CLOB as last resort
            mid = self.clob.get_midpoint(token_id, condition_id)
            if mid:
                current_price = mid
            else:
                return None

        result["current_price"] = round(current_price, 4)

        # LAYER 1: ORDER BOOK SIGNAL from best bid/ask
        book_sig = self._analyze_bid_ask(
            best_bid, best_ask, gamma_spread, token_id, condition_id
        )
        if book_sig:
            result["order_book"] = book_sig
            self._order_book_cache[token_id or condition_id] = book_sig

        # LAYER 3: FAIR PRICE ESTIMATION
        fair_price = self._estimate_fair_price(city, temp, title)
        result["fair_price"] = fair_price

        if not fair_price:
            return None

        # Compute base EV
        ev_buy = fair_price - current_price
        ev_sell = current_price - fair_price
        base_direction = None
        base_ev = 0.0
        base_conf = 0.0

        if ev_buy > 0.05:  # 5% edge minimum
            base_ev = round(ev_buy, 4)
            base_direction = "buy"
            base_conf = min(0.9, 0.5 + ev_buy)
        elif ev_sell > 0.05:
            base_ev = round(-ev_sell, 4)
            base_direction = "sell"
            base_conf = min(0.9, 0.5 + ev_sell)

        # LAYER 1 BOOST: Adjust EV based on order book
        wall_score = book_sig.get("wall_score", 0) if book_sig else 0
        if book_sig and base_direction:
            if base_direction == "buy" and wall_score > 0.3:
                base_conf = min(0.99, base_conf + 0.15)
                base_ev = round(base_ev * 1.2, 4)
                logger.debug("📗 Book confirms BUY %s (wall_score=%.2f)", city, wall_score)
            elif base_direction == "buy" and wall_score < -0.3:
                base_conf = max(0.1, base_conf - 0.2)
                base_ev = round(base_ev * 0.7, 4)
                logger.debug("📕 Book opposes BUY %s (wall_score=%.2f)", city, wall_score)
            elif base_direction == "sell" and wall_score < -0.3:
                base_conf = min(0.99, base_conf + 0.15)
                base_ev = round(base_ev * 1.2, 4)
                logger.debug("📗 Book confirms SELL %s (wall_score=%.2f)", city, wall_score)
            elif base_direction == "sell" and wall_score > 0.3:
                base_conf = max(0.1, base_conf - 0.2)
                base_ev = round(base_ev * 0.7, 4)

        result["ev"] = base_ev
        result["direction"] = base_direction
        result["confidence"] = base_conf

        # LAYER 2: WHALE CONVICTION
        if self.whale_data and base_direction:
            whale_conf = self._check_whale_signal(city, temp, base_direction)
            if whale_conf:
                result["whale_signal"] = whale_conf
                result["confidence"] = min(0.99, result["confidence"] + 0.2)

        if self.position_tracker:
            conviction_sigs = self.position_tracker.get_conviction_signals(min_score=0.5)
            if conviction_sigs:
                for cs in conviction_sigs:
                    risk = f"highest-temperature-in-{city}"
                    if risk in cs.get("contract", "") or cs["conviction"] > 0.7:
                        result["conviction"] = {
                            "score": cs["conviction"],
                            "trend": cs["conviction_trend"],
                            "whale": cs["wallet"],
                        }
                        result["confidence"] = min(0.99, result["confidence"] + 0.15)
                        break

        # LAYER 4: COMBINED SCORE
        result["combined_score"] = round(
            result["confidence"] * 0.5 + abs(result.get("ev", 0) or 0) * 50 * 0.3
            + (0.2 if wall_score > 0.3 else 0),
            3
        )

        return result

    def _analyze_bid_ask(self, best_bid, best_ask, spread, token_id, condition_id) -> dict | None:
        """
        Analyze best bid/ask from Gamma API to generate order book signal.
        This is a simplified version that works with top-of-book only.
        
        Returns a dict matching order_book_to_signal() format.
        """
        if best_bid is None or best_ask is None:
            return None

        bid = float(best_bid)
        ask = float(best_ask)
        sp = float(spread) if spread else (ask - bid)
        mid = (bid + ask) / 2

        signal = {
            "token_id": token_id or condition_id or "",
            "mid_price": round(mid, 4),
            "spread": round(sp, 4),
            "bid_price": bid,
            "ask_price": ask,
            "bid_depth": 0.0,
            "ask_depth": 0.0,
            "bid_wall": None,
            "bid_wall_size": 0.0,
            "ask_wall": None,
            "ask_wall_size": 0.0,
            "is_ask_thin": False,
            "is_bid_thin": False,
            "skew": 0.0,
            "wall_score": 0.0,
        }

        # Thin ask: spread is widening because ask is high
        # In Polymarket, a spread > 0.05 for a $0.50 asset is wide
        if sp > 0.05:
            signal["is_ask_thin"] = True
            signal["wall_score"] = -0.2
            logger.debug("  Wide spread %.4f — thin liquidity", sp)

        # Bid heavy: bid is close to mid (tight spread, high bid confidence)
        if sp < 0.01 and bid > 0.1:
            signal["wall_score"] = 0.2
            logger.debug("  Tight spread %.4f — bid heavy", sp)

        # For weather markets with typical bid/ask structure:
        # bid close to mid = somebody wants to buy
        # ask far from mid = sellers asking a premium
        # This gives us directional bias
        skew_val = (ask - mid) - (mid - bid)
        if abs(skew_val) > 0.01:
            signal["skew"] = round(skew_val, 3)
            if skew_val > 0:
                # Ask is further from mid than bid → bid side has more weight
                signal["wall_score"] = max(signal["wall_score"], 0.1)
            else:
                signal["wall_score"] = min(signal["wall_score"], -0.1)

        return signal

    def scan_all_markets(self, markets: list[dict]) -> list[dict]:
        """Scan all available weather markets and return actionable signals."""
        signals = []
        for market in markets:
            try:
                result = self.evaluate_market(market)
                if result and result["confidence"] >= 0.5 and result["ev"]:
                    signals.append(result)
            except Exception as e:
                logger.warning("Error evaluating market %s: %s",
                               market.get("title", "?"), e)

        # Sort by combined score
        signals.sort(key=lambda s: -s.get("combined_score", 0))
        return signals

    def build_portfolio(self, signals: list[dict], capital: float = 1000) -> list[dict]:
        """
        Build a multi-market portfolio from signals.
        Spread across multiple cities simultaneously.
        Allocates capital proportionally to combined score.
        """
        if not signals:
            return []

        total_score = sum(s.get("combined_score", 0.01) for s in signals)
        portfolio = []

        for s in signals:
            alloc_pct = (s.get("combined_score", 0.01) / total_score)
            allocation = alloc_pct * capital
            book_info = s.get("order_book", {})

            portfolio.append({
                "city": s["city"],
                "condition_id": s["condition_id"],
                "token_id": s["token_id"],
                "direction": s["direction"],
                "confidence": s["confidence"],
                "allocation": round(allocation, 2),
                "expected_value": s["ev"],
                "entry_price": s["current_price"],
                "combined_score": s.get("combined_score", 0),
                "order_book": {
                    "wall_score": book_info.get("wall_score", 0),
                    "spread": book_info.get("spread", 0),
                    "skew": book_info.get("skew", 0),
                    "bid_wall": book_info.get("bid_wall"),
                    "is_ask_thin": book_info.get("is_ask_thin", False),
                },
                "whale_signal": s.get("whale_signal"),
                "conviction": s.get("conviction"),
            })

        return portfolio

    def _extract_city(self, title: str) -> str | None:
        """Extract city name from market title using word-boundary matching."""
        title_lower = title.lower()
        for city in WEATHER_CITIES:
            # Use word-boundary regex to avoid substring greediness
            #   e.g. "boston" shouldn't match inside "bostonian"
            if re.search(r'\b' + re.escape(city) + r'\b', title_lower):
                return city
        return None

    def _extract_temperature(self, title: str) -> int | None:
        """Extract temperature threshold from market title."""
        # Match patterns like "75°", "75 deg", "75 degree", "75 degrees", "75F"
        match = re.search(r'(\d+)\s*(°|deg|degrees?|F)', title.lower())
        if match:
            return int(match.group(1))
        for t in TEMP_THRESHOLDS:
            if str(t) in title:
                return t
        return None

    def _estimate_fair_price(self, city: str, temp: int, title: str) -> float | None:
        """
        Estimate fair price using real Open-Meteo ECMWF forecast.

        Calls the Open-Meteo forecast API for the city's high temperature on
        the target date, then computes the probability that the actual temp
        exceeds the market's threshold via norm_cdf( (forecast - threshold) / sigma).

        Cached per (city, date_str) to avoid duplicate API calls within a cycle.
        """
        coords = CITY_COORDS.get(city)
        if not coords:
            return None

        # Extract date from title (e.g. "May 15, 2026" or "2026-05-15")
        date_str = self._extract_date(title)
        if not date_str:
            return None

        forecast = self._fetch_open_meteo_forecast(city, coords, date_str)
        if forecast is None:
            # Fallback: use a mild prior centered on the threshold
            logger.debug("  ⚠ No forecast for %s — using prior", city)
            return round(0.5, 3)

        fcst_temp = forecast["temp_max_f"]
        sigma = forecast.get("sigma", FORECAST_SIGMA_F)

        # Probability actual temp > threshold = 1 - CDF(threshold)
        # This is the fair price for the YES side
        z = (fcst_temp - temp) / sigma
        fair = round(1.0 - _norm_cdf(z), 3)

        logger.debug(
            "  🌡 %s forecast=%.0f°F  threshold=%d°F  sigma=%.1f  →  fair=%.3f",
            city, fcst_temp, temp, sigma, fair,
        )
        return fair

    def _extract_date(self, title: str) -> str | None:
        """Extract a date string (YYYY-MM-DD) from a market title.

        Handles formats like:
          - "...2026-05-15..."
          - "...May 15, 2026..."
          - "...15 May 2026..."
        """
        import re
        # ISO: 2026-05-15
        m = re.search(r'(\d{4}-\d{2}-\d{2})', title)
        if m:
            return m.group(1)

        # Human: May 15, 2026 or 15 May 2026
        month_map = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        m = re.search(
            r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),?\s*(\d{4})',
            title.lower()
        )
        if m:
            month = month_map[m.group(1)]
            day = int(m.group(2))
            year = int(m.group(3))
            return f"{year}-{month:02d}-{day:02d}"

        # Non-US: 15 May 2026
        m = re.search(
            r'(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})',
            title.lower()
        )
        if m:
            day = int(m.group(1))
            month = month_map[m.group(2)]
            year = int(m.group(3))
            return f"{year}-{month:02d}-{day:02d}"

        return None

    def _fetch_open_meteo_forecast(
        self, city_slug: str, coords: tuple, date_str: str
    ) -> dict | None:
        """Fetch the max temperature forecast from Open-Meteo for a given date.

        Uses the ECMWF IFS model (default for Open-Meteo).
        Caches results in _FORECAST_CACHE per (city_slug, date_str).
        """
        cache_key = (city_slug, date_str)
        now_ts = datetime.now(timezone.utc).timestamp()

        # Return cached result if fresh (< 5 minutes)
        if cache_key in _FORECAST_CACHE:
            entry = _FORECAST_CACHE[cache_key]
            if now_ts - entry.get("_ts", 0) < 300:
                return entry

        try:
            lat, lon = coords
            # Determine how many days ahead this date is
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            days_ahead = (target_date - today).days
            if days_ahead < 0:
                # Past date — can't forecast
                return None
            # Open-Meteo forecast_days param: max 16, we'll request enough
            forecast_days = max(3, days_ahead + 2)

            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "forecast_days": forecast_days,
                "timezone": "UTC",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            daily = data.get("daily", {})
            dates = daily.get("time", [])
            temps = daily.get("temperature_2m_max", [])

            # Find the target date in the forecast
            for i, d in enumerate(dates):
                if d == date_str and i < len(temps):
                    entry = {
                        "temp_max_f": round(temps[i], 1),
                        "sigma": FORECAST_SIGMA_F,
                        "_ts": now_ts,
                    }
                    _FORECAST_CACHE[cache_key] = entry
                    return entry

            logger.debug(
                "  🔍 %s not in forecast range (got %d days, needed %s)",
                city_slug, len(dates), date_str,
            )
            return None

        except Exception as e:
            logger.debug("  🌐 Open-Meteo API error for %s: %s", city_slug, e)
            return None

    def _check_whale_signal(
        self, city: str, temp: int, direction: str
    ) -> float | None:
        """
        Check if whales are active in this city/temperature market.
        Returns confidence boost (0.0 - 0.3) if whale aligned.
        Now actually checks city, temperature, and direction alignment.
        """
        if not self.whale_data:
            return None

        city_norm = city.lower().replace("-", " ")
        temp_str = str(temp)
        aligned = 0
        opposed = 0

        for label, trades in self.whale_data.items():
            for t in trades:
                tn = (t.get("token_name", "") or t.get("title", "") or "").lower()
                # Must reference weather
                if "weather" not in tn and "temperature" not in tn:
                    continue
                # City match (normalized: "new york" vs "new-york")
                tn_norm = tn.replace("-", " ")
                if city_norm not in tn_norm:
                    continue
                # Temperature threshold match
                if temp_str not in tn:
                    continue

                w_side = t.get("side", "")
                t_direction = t.get("direction", w_side)

                if direction == "buy" and t_direction in ("YES", "BUY", "buy"):
                    aligned += 1
                elif direction == "sell" and t_direction in ("NO", "SELL", "sell"):
                    aligned += 1
                elif direction == "buy" and t_direction in ("NO", "SELL", "sell"):
                    opposed += 1
                elif direction == "sell" and t_direction in ("YES", "BUY", "buy"):
                    opposed += 1

        if aligned > 0:
            return min(0.3, 0.15 * aligned)
        if opposed > 0:
            return -0.15 * opposed
        return None

    def record_trade_result(self, position: dict, pnl: float):
        """Record the result of a closed position for strategy improvement."""
        self.performance["pnl"] += pnl
        if pnl > 0:
            self.performance["wins"] += 1
        else:
            self.performance["losses"] += 1
        logger.info(
            "Trade closed: %s $%.2f (win rate: %d/%d)",
            position.get("city", "?"), pnl,
            self.performance["wins"],
            self.performance["wins"] + self.performance["losses"],
        )

    def get_order_book_summary(self) -> str:
        """Human-readable summary of recent order book analysis."""
        lines = []
        lines.append("📊 Order Book Analysis")
        lines.append("=" * 50)
        for token_id, sig in list(self._order_book_cache.items())[:10]:
            walls = []
            if sig.bid_wall is not None:
                walls.append(f"BID WALL ${sig.bid_wall:.4f} (${sig.bid_wall_size:.0f})")
            if sig.ask_wall is not None:
                walls.append(f"ASK WALL ${sig.ask_wall:.4f} (${sig.ask_wall_size:.0f})")
            thin = "THIN ASK" if sig.is_ask_thin else ("THIN BID" if sig.is_bid_thin else "")
            lines.append(
                f"  {token_id[:12]:12s} spread:{sig.spread:.4f} "
                f"skew:{sig.skew:+.3f} score:{sig.wall_score:+.2f}  "
                f"{' '.join(walls)} {thin}"
            )
        return "\n".join(lines)
