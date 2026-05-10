"""
Paper Trading Bot - runs weather bot EV strategy as the core trading engine,
uses whale data as a confirmation/denial overlay. Trades many small positions
to learn fast. Self-learning reviews every resolved trade.

Strategy flow:
1. Scan all weather markets (20+ cities, temp buckets)
2. Compute EV from forecast vs market price (weather bot logic)
3. Check whale activity in each market (from scraper cache)
4. If whale aligns -> boost confidence + size
5. If whale opposes -> reduce confidence + size
6. Execute paper trade if confidence > threshold
7. Self-learning reviews closed trades, adjusts parameters

No wallet, no API keys, no real money needed.
"""
import json
import logging
import math
import re
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PAPER_STATE_FILE = Path(__file__).parent / "data" / "paper_portfolio.json"
PAPER_TRADES_LOG = Path(__file__).parent / "data" / "paper_trades.jsonl"

DEFAULT_BANKROLL = 100.0
MAX_BET = 2.0
MIN_EV = 0.05
EV_THRESHOLD = 0.05

WHALE_BOOST = 0.15
WHALE_PENALTY = -0.20
CONSENSUS_BOOST = 0.10

# =============================================================================
# FORECAST LOCATIONS
# =============================================================================

FORECAST_LOCATIONS = {
    "Chicago": {"lat": 41.8781, "lon": -87.6298, "station": "KORD", "unit": "F", "region": "us"},
    "New York City": {"lat": 40.7772, "lon": -73.8726, "station": "KLGA", "unit": "F", "region": "us"},
    "NYC": {"lat": 40.7772, "lon": -73.8726, "station": "KLGA", "unit": "F", "region": "us"},
    "Miami": {"lat": 25.7932, "lon": -80.2906, "station": "KMIA", "unit": "F", "region": "us"},
    "Dallas": {"lat": 32.8479, "lon": -96.8518, "station": "KDAL", "unit": "F", "region": "us"},
    "Denver": {"lat": 39.8617, "lon": -104.673, "station": "KDEN", "unit": "F", "region": "us"},
    "Seattle": {"lat": 47.4499, "lon": -122.311, "station": "KSEA", "unit": "F", "region": "us"},
    "Atlanta": {"lat": 33.6407, "lon": -84.4277, "station": "KATL", "unit": "F", "region": "us"},
    "Boston": {"lat": 42.3662, "lon": -71.0621, "station": "KBOS", "unit": "F", "region": "us"},
    "Phoenix": {"lat": 33.4342, "lon": -112.008, "station": "KPHX", "unit": "F", "region": "us"},
    "Houston": {"lat": 29.9901, "lon": -95.3368, "station": "KIAH", "unit": "F", "region": "us"},
    "Los Angeles": {"lat": 33.9425, "lon": -118.408, "station": "KLAX", "unit": "F", "region": "us"},
    "San Francisco": {"lat": 37.6188, "lon": -122.375, "station": "KSFO", "unit": "F", "region": "us"},
    "London": {"lat": 51.5074, "lon": -0.1278, "station": "EGLL", "unit": "C", "region": "eu"},
    "Paris": {"lat": 48.8534, "lon": 2.3488, "station": "LFPG", "unit": "C", "region": "eu"},
    "Tokyo": {"lat": 35.6895, "lon": 139.6917, "station": "RJTT", "unit": "C", "region": "as"},
    "Berlin": {"lat": 52.5200, "lon": 13.4050, "station": "EDDB", "unit": "C", "region": "eu"},
}

# =============================================================================
# FORECAST FUNCTIONS
# =============================================================================

def _get_forecast_temp(city: str, date_str: str, unit: str = 'F') -> Optional[float]:
    """Fetch forecast temperature from Open-Meteo ECMWF API."""
    loc = FORECAST_LOCATIONS.get(city)
    if not loc:
        return None
    temp_unit = 'fahrenheit' if unit == 'F' else 'celsius'
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&models=ecmwf_ifs025&bias_correction=true"
    )
    for attempt in range(2):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if 'error' not in data:
                for d, t in zip(data['daily']['time'], data['daily']['temperature_2m_max']):
                    if d == date_str and t is not None:
                        return round(t) if unit == 'F' else round(t, 1)
            break
        except Exception:
            if attempt < 1:
                time.sleep(2)
    return None

# =============================================================================
# STATE MANAGEMENT
# =============================================================================

def _load_state() -> dict:
    if PAPER_STATE_FILE.exists():
        try:
            return json.loads(PAPER_STATE_FILE.read_text())
        except (json.JSONDecodeError, Exception):
            pass
    return {
        "bankroll": DEFAULT_BANKROLL,
        "starting_bankroll": DEFAULT_BANKROLL,
        "positions": {},
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "parameters": {
            "min_ev": MIN_EV,
            "max_bet": MAX_BET,
            "max_positions": 20,
            "whale_boost": WHALE_BOOST,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_state(state: dict):
    PAPER_STATE_FILE.parent.mkdir(exist_ok=True)
    PAPER_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def _log_trade(entry: dict):
    PAPER_TRADES_LOG.parent.mkdir(exist_ok=True)
    with open(PAPER_TRADES_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")

# =============================================================================
# MATH
# =============================================================================

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _kelly_fraction(p: float, price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    q = 1.0 - p
    f = (p * b - q) / b
    return max(0.0, f)


def _kelly_size(kelly: float, bankroll: float) -> float:
    raw = kelly * bankroll
    return min(raw, MAX_BET)


print('Module functions defined OK')


class PaperTrader:
    """Paper trading engine: EV-driven with whale overlay.
    Self-learning adjusts parameters as trades resolve."""

    def __init__(self, bankroll: float = None):
        self.state = _load_state()
        if bankroll is not None:
            self.state["bankroll"] = bankroll
            self.state["starting_bankroll"] = bankroll
        self._open_positions = {k: v for k, v in self.state.get("positions", {}).items()
                                if v.get("status") == "open"}

    def _reload(self):
        """Reload state from disk - keeps dashboard in sync with cron jobs."""
        disk_state = _load_state()
        if 'positions' in disk_state:
            self.state = disk_state
            self._open_positions = {k: v for k, v in self.state.get("positions", {}).items()
                                    if v.get("status") == "open"}

    def evaluate_and_trade(self, market: dict, whale_positions: list[dict] = None,
                           fair_price: float = None) -> dict | None:
        """Core method: evaluate a market and execute a paper trade if edge exists."""
        title = market.get("question", "?")
        condition_id = market.get("conditionId", "")
        market_id = market.get("id", "")
        best_bid = market.get("bestBid")
        best_ask = market.get("bestAsk")
        outcome_prices = market.get("outcomePrices", "[]")
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        # Use best bid as conservative entry price
        price = float(best_bid) if best_bid is not None else (float(best_ask) if best_ask is not None else None)
        if not price or price <= 0 or price >= 1:
            return None

        # Skip if no real bid/ask (no liquidity)
        if best_bid is None or best_ask is None:
            return None
        spread = float(best_ask) - float(best_bid)
        if spread > 0.10:
            return None

        # Default fair price if not provided
        if fair_price is None:
            fair_price = self._estimate_fair_price(title)
        if fair_price is None or fair_price <= 0 or fair_price >= 1:
            return None

        # Compute EV
        ev = fair_price - price
        if abs(ev) < EV_THRESHOLD:
            return None

        direction = "BUY" if ev > 0 else "SELL"
        base_confidence = min(0.9, 0.5 + abs(ev))

        # Whale overlay
        whale_overlay = self._check_whale_overlay(title, direction, whale_positions or [])
        confidence = base_confidence + whale_overlay["adjustment"]
        confidence = max(0.05, min(0.99, confidence))

        if confidence < 0.3:
            return None

        # Kelly sizing with whale boost
        kelly_raw = _kelly_fraction(fair_price, price) if direction == "BUY" else _kelly_fraction(1-fair_price, 1-price)
        kelly_adjusted = kelly_raw * (1.0 + whale_overlay["size_boost"])
        allocation = _kelly_size(kelly_adjusted * 0.25, self.state["bankroll"])
        allocation = min(allocation, MAX_BET)

        # Cap exposure at 50% of bankroll and max 15 positions
        current_exposure = sum(p.get("value", 0) for p in self._open_positions.values())
        if current_exposure + allocation > self.state["bankroll"] * 0.5:
            return None
        if len(self._open_positions) >= 15:
            return None

        # Opposite-position check
        opposite_dir = "SELL" if direction == "BUY" else "BUY"
        for key, p in self._open_positions.items():
            if p.get("condition_id") == condition_id and p.get("side") == opposite_dir:
                return None

        # Correct share count for SELL
        if direction == 'SELL':
            shares = round(allocation / max(0.001, 1 - price), 2)
        else:
            shares = round(allocation / max(0.001, price), 2)

        slug = self._make_slug(title, direction)
        entry = {
            "event_slug": slug,
            "market_id": market_id,
            "condition_id": condition_id,
            "title": title[:100],
            "side": direction,
            "entry_price": round(price, 4),
            "current_price": round(price, 4),
            "shares": shares,
            "value": round(allocation, 2),
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "ev": round(ev, 4),
            "fair_price": round(fair_price, 4),
            "confidence": round(confidence, 2),
            "whale_aligned": whale_overlay["aligned"],
            "whale_count": whale_overlay["count"],
            "whale_adjustment": round(whale_overlay["adjustment"], 3),
            "kelly_fraction": round(kelly_adjusted * 0.25, 4),
            "entry_ts": datetime.now(timezone.utc).isoformat(),
            "status": "open",
        }

        # Dedup by condition_id first
        pos_key = f"{condition_id}-{direction}" if condition_id else slug
        existing_key = None
        if pos_key in self._open_positions:
            existing_key = pos_key
        elif slug in self._open_positions:
            existing_key = slug

        if existing_key:
            pos = self._open_positions[existing_key]
            pos["current_price"] = price
            if pos.get('side') == 'SELL':
                pos['value'] = round(pos['shares'] * (1 - price), 2)
                pos['pnl'] = round(pos['shares'] * (pos['entry_price'] - price), 2)
            else:
                pos['value'] = round(pos['shares'] * price, 2)
                pos['pnl'] = round(pos['value'] - pos['shares'] * pos['entry_price'], 2)
            pos["pnl_pct"] = round((price - pos["entry_price"]) / max(0.001, pos["entry_price"]) * 100, 2)
            self.state["positions"][existing_key] = pos
            _log_trade({"action": "UPDATE", **pos})
            _save_state(self.state)
            return pos

        # Deduct bankroll on position open
        self.state['bankroll'] = round(self.state['bankroll'] - allocation, 2)

        # Open new position
        self.state["positions"][pos_key] = entry
        self.state["total_trades"] += 1
        self._open_positions[pos_key] = entry
        _log_trade({"action": "OPEN", **entry})
        _save_state(self.state)
        logger.info("%s %s: $%.2f at %.1fc (EV:%+.2f, whale:%+.2f, conf:%.0f%%)",
                    direction, title[:35], allocation, price * 100,
                    ev * 100, whale_overlay["adjustment"] * 100, confidence * 100)
        return entry

    def close_if_expired(self, market_title: str = None, resolved_price: float = None,
                         condition_id: str = None):
        """Close a trade when market resolves. Records win/loss."""
        pos = None
        pos_key = None

        # Try condition_id first
        if condition_id:
            for key, p in list(self._open_positions.items()):
                if p.get("condition_id") == condition_id and p.get("status") == "open":
                    pos = p
                    pos_key = key
                    break

        # Fall back to slug-based lookup
        if not pos and market_title:
            for direction in ("BUY", "SELL"):
                slug = self._make_slug(market_title, direction)
                p = self.state["positions"].get(slug)
                if p and p.get("status") == "open":
                    pos = p
                    pos_key = slug
                    break

        if not pos:
            return

        pos["status"] = "closed"
        pos["closed_at"] = datetime.now(timezone.utc).isoformat()
        if resolved_price is not None:
            pos["settlement_price"] = resolved_price
            if pos.get("side") == "SELL":
                pnl = (pos["entry_price"] - resolved_price) * pos["shares"]
            else:
                pnl = (resolved_price - pos["entry_price"]) * pos["shares"]
            pos["pnl"] = round(pnl, 2)

        self.state["bankroll"] += pos.get("pnl", 0)
        if pos.get("pnl", 0) >= 0:
            self.state["wins"] += 1
        else:
            self.state["losses"] += 1
        _log_trade({"action": "CLOSE", **pos})
        _save_state(self.state)
        # Remove from open positions
        if pos_key and pos_key in self._open_positions:
            del self._open_positions[pos_key]
        logger.info("CLOSE %s: $%.2f PnL", (market_title or condition_id or "?")[:35], pos.get("pnl", 0))

    def _check_whale_overlay(self, title: str, direction: str,
                              whale_positions: list[dict]) -> dict:
        """Check if whales are in this market and what direction."""
        title_lower = title.lower()
        aligned = 0
        opposed = 0

        for wp in whale_positions:
            wt = (wp.get("title") or "").lower()
            if not self._title_overlaps(title_lower, wt):
                continue
            w_side = wp.get("side", "")
            if (direction == "BUY" and w_side == "YES") or                (direction == "SELL" and w_side == "NO"):
                aligned += 1
            else:
                opposed += 1

        adjustment = aligned * WHALE_BOOST + opposed * WHALE_PENALTY
        size_boost = aligned * CONSENSUS_BOOST
        count = aligned + opposed
        aligned_flag = aligned > opposed

        return {
            "adjustment": adjustment,
            "size_boost": size_boost,
            "aligned": aligned_flag,
            "count": count,
        }

    def _title_overlaps(self, a: str, b: str) -> bool:
        if not a or not b:
            return False
        a_cities = set(re.findall(r'(new york|chicago|miami|denver|dallas|london|tokyo|paris|berlin|mexico|la|sf|seattle|boston|phoenix|houston|atlanta)', a))
        b_cities = set(re.findall(r'(new york|chicago|miami|denver|dallas|london|tokyo|paris|berlin|mexico|la|sf|seattle|boston|phoenix|houston|atlanta)', b))
        a_dates = set(re.findall(r'(may|april|june|july)\s+\d+', a))
        b_dates = set(re.findall(r'(may|april|june|july)\s+\d+', b))
        city_match = bool(a_cities & b_cities)
        date_match = bool(a_dates & b_dates)
        return city_match and date_match

    def _make_slug(self, title: str, direction: str) -> str:
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:60]
        return f"{slug}-{direction}"

    def discover_weather_markets(self) -> list[dict]:
        """Discover today's weather markets via Gamma event slugs."""
        cities_slugs = {
            "new-york": "NYC", "chicago": "Chicago", "miami": "Miami",
            "dallas": "Dallas", "denver": "Denver", "seattle": "Seattle",
            "atlanta": "Atlanta", "boston": "Boston", "phoenix": "Phoenix",
            "houston": "Houston", "los-angeles": "Los Angeles",
            "san-francisco": "San Francisco", "london": "London",
            "paris": "Paris", "tokyo": "Tokyo", "berlin": "Berlin",
            "sydney": "Sydney", "mexico-city": "Mexico City",
            "austin": "Austin", "lucknow": "Lucknow",
        }
        months = ["january","february","march","april","may","june",
                   "july","august","september","october","november","december"]

        now = datetime.now(timezone.utc)
        markets = []

        for city_slug, city_name in cities_slugs.items():
            for offset in range(3):
                dt = now + timedelta(days=offset)
                month_str = months[dt.month - 1]

                for prefix in ("highest-temperature", "lowest-temperature"):
                    slug = f"{prefix}-in-{city_slug}-on-{month_str}-{dt.day}-{dt.year}"
                    try:
                        r = requests.get(
                            f"https://gamma-api.polymarket.com/events",
                            params={"slug": slug},
                            timeout=8,
                        )
                        if r.status_code != 200:
                            continue
                        events = r.json()
                        if not events or not isinstance(events, list) or len(events) == 0:
                            continue
                        event = events[0]
                        for m in event.get("markets", []):
                            mkt = {
                                "question": m.get("question", "?"),
                                "id": str(m.get("id", "")),
                                "conditionId": m.get("conditionId", ""),
                                "bestBid": m.get("bestBid"),
                                "bestAsk": m.get("bestAsk"),
                                "outcomePrices": m.get("outcomePrices", "[]"),
                                "clobTokenIds": m.get("clobTokenIds", []),
                                "volume": m.get("volume", 0),
                                "slug": slug,
                                "city": city_name,
                                "date": dt.strftime("%Y-%m-%d"),
                            }
                            markets.append(mkt)
                    except Exception:
                        continue

        return markets

    def _estimate_fair_price(self, title: str, date_str: str = None) -> Optional[float]:
        """Estimate fair price from real Open-Meteo ECMWF weather forecasts."""
        title_lower = title.lower()
        city = None
        for city_name in FORECAST_LOCATIONS:
            if city_name.lower() in title_lower:
                city = city_name
                break
        if not city:
            return None

        if not date_str:
            date_match = re.search(r'(may|april|june|july|august|september|october)\s+(\d+)', title_lower)
            if date_match:
                month_name = date_match.group(1)
                day = date_match.group(2)
                month_map = {'may':'05','april':'04','june':'06','july':'07','august':'08','september':'09','october':'10'}
                date_str = f"2026-{month_map.get(month_name,'05')}-{day.zfill(2)}"
            else:
                date_str = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        temp = None
        temp_match = re.search(r'(\d+)\s*[°F]', title)
        if temp_match:
            temp = int(temp_match.group(1))

        loc = FORECAST_LOCATIONS.get(city)
        if not loc:
            return None

        unit = loc["unit"]
        forecast_temp = _get_forecast_temp(city, date_str, unit)
        if forecast_temp is None:
            return None
        if temp is None:
            return 0.5

        sigma = 4.0 if unit == "F" else 2.2
        z = (forecast_temp - temp) / sigma
        prob = _norm_cdf(z)
        return round(max(0.01, min(0.99, prob)), 4)

    def resolve_positions(self):
        """Check all open positions against Gamma API for closed markets."""
        resolved_count = 0
        for key, pos in list(self._open_positions.items()):
            lookup_id = pos.get('market_id') or pos.get('condition_id')
            if not lookup_id:
                continue
            try:
                r = requests.get(
                    f'https://gamma-api.polymarket.com/markets/{lookup_id}',
                    timeout=8
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                if not data.get('closed', False):
                    continue
                outcome_prices_str = data.get('outcomePrices', '[0.5,0.5]')
                try:
                    prices = json.loads(outcome_prices_str)
                    settlement_price = float(prices[0])
                except (json.JSONDecodeError, IndexError, TypeError):
                    settlement_price = None

                if settlement_price is not None:
                    self.close_if_expired(
                        condition_id=pos.get('condition_id'),
                        resolved_price=settlement_price
                    )
                    resolved_count += 1
            except Exception:
                continue
        if resolved_count:
            logger.info("Resolved %d positions via Gamma API", resolved_count)
        return resolved_count

    def update_prices(self):
        """Refresh all open position prices from Gamma API."""
        for key, pos in list(self._open_positions.items()):
            lookup_id = pos.get('market_id') or pos.get('condition_id')
            if not lookup_id:
                continue
            try:
                r = requests.get(
                    f'https://gamma-api.polymarket.com/markets/{lookup_id}',
                    timeout=5
                )
                data = r.json()
                prices_str = data.get('outcomePrices', '[0.5,0.5]')
                prices = json.loads(prices_str)
                price = float(prices[0])
                pos['current_price'] = price
                if pos.get('side') == 'SELL':
                    pos['value'] = round(pos['shares'] * (1 - price), 2)
                    pos['pnl'] = round(pos['shares'] * (pos['entry_price'] - price), 2)
                else:
                    pos['value'] = round(pos['shares'] * price, 2)
                    pos['pnl'] = round(pos['value'] - pos['shares'] * pos['entry_price'], 2)
            except Exception:
                pass
        _save_state(self.state)

    def summary(self) -> dict:
        """Get current portfolio summary."""
        self._reload()
        open_positions = [p for p in self.state.get("positions", {}).values()
                          if p.get("status") == "open"]
        total_value = sum(p.get("value", 0) for p in open_positions)
        total_pnl = sum(p.get("pnl", 0) for p in open_positions)

        return {
            "bankroll": round(self.state.get("bankroll", 0), 2),
            "starting_bankroll": round(self.state.get("starting_bankroll", 0), 2),
            "exposure": round(self.state.get("exposure", 0) or total_value, 2),
            "open_positions": len(open_positions),
            "total_trades": self.state.get("total_trades", 0),
            "wins": self.state.get("wins", 0),
            "losses": self.state.get("losses", 0),
            "total_pnl": round(total_pnl, 2),
            "parameters": self.state.get("parameters", {}),
            "last_sync": self.state.get("last_sync", ""),
        }

    def get_open_positions(self) -> list[dict]:
        self._reload()
        positions = [p for p in self.state.get("positions", {}).values()
                     if p.get("status") == "open"]
        positions.sort(key=lambda p: -abs(p.get("pnl", 0)))
        return positions

    def get_closed_positions(self, limit: int = 20) -> list[dict]:
        self._reload()
        positions = [p for p in self.state.get("positions", {}).values()
                     if p.get("status") == "closed"]
        positions.sort(key=lambda p: p.get("closed_at", ""), reverse=True)
        return positions[:limit]

    def get_portfolio_report(self) -> str:
        summary = self.summary()
        lines = []
        lines.append("Paper Portfolio - EV + Whale Overlay")
        lines.append("=" * 55)
        lines.append(f"  Bankroll:    ${summary['bankroll']:.2f}")
        lines.append(f"  Exposure:    ${summary['exposure']:.2f} "
                     f"({summary['exposure']/max(1,summary['bankroll'])*100:.0f}%)")
        lines.append(f"  PnL:         ${summary['total_pnl']:.2f}")
        lines.append(f"  Open pos:    {summary['open_positions']}")
        lines.append(f"  Total:       {summary['total_trades']} ({summary['wins']}W/{summary['losses']}L)")
        lines.append("")

        for p in self.get_open_positions()[:10]:
            pnl_sym = "+" if p.get("pnl", 0) >= 0 else ""
            lines.append(
                f"  {p.get('side', '?'):3s} {str(p.get('title','?'))[:40]:40s} "
                f"${p.get('value',0):>5.2f}  "
                f"{pnl_sym}${p.get('pnl',0):.2f}  "
                f"EV:{p.get('ev',0):+.2f}"
            )
        return "\n".join(lines)
