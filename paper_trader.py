"""
Paper Trading Bot - runs weather bot EV strategy as the core trading engine,
uses whale data as a confirmation/denial overlay. Trades many small positions
to learn fast. Self-learning reviews every resolved trade.

Strategy flow:
1. Scan curated US weather markets only
2. Compute EV from forecast vs market price (weather bot logic)
3. Check whale activity in each market (from scraper cache)
4. If whale aligns -> modest confidence + size boost
5. If whale opposes -> reduce confidence or veto weak trades
6. Execute paper trade only after liquidity and risk filters pass
7. Self-learning reviews closed trades daily; parameter changes are opt-in

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
FORECAST_CACHE: dict[tuple[str, str, str], Optional[float]] = {}

DEFAULT_BANKROLL = 100.0
MAX_BET = 2.0
MIN_EV = 0.05
EV_THRESHOLD = 0.05
MIN_VOLUME = 200.0
MAX_SPREAD = 0.08
MAX_OPEN_POSITIONS = 12
MAX_CITY_POSITIONS = 2
EXPERIMENT_DAYS = 3
STOP_LOSS_PCT = 35.0
EV_FLIP_EXIT_BUFFER = 0.02

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
    cache_key = (city, date_str, unit)
    if cache_key in FORECAST_CACHE:
        return FORECAST_CACHE[cache_key]

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
                        result = round(t) if unit == 'F' else round(t, 1)
                        FORECAST_CACHE[cache_key] = result
                        return result
            break
        except Exception:
            if attempt < 1:
                time.sleep(2)
    FORECAST_CACHE[cache_key] = None
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
            "max_positions": MAX_OPEN_POSITIONS,
            "max_city_positions": MAX_CITY_POSITIONS,
            "whale_boost": WHALE_BOOST,
            "max_price": 0.45,
            "kelly_fraction": 0.25,
            "min_volume": MIN_VOLUME,
            "max_spread": MAX_SPREAD,
            "stop_loss_pct": STOP_LOSS_PCT,
            "ev_flip_exit_buffer": EV_FLIP_EXIT_BUFFER,
        },
        "experiment": {
            "scope": "us-weather-paper-v1",
            "duration_days": EXPERIMENT_DAYS,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
        "last_cycle_report": {},
        "last_learning_review_date": None,
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


def _extract_bucket_bounds(title: str) -> tuple[float, float]:
    """Parse the traded temperature bucket from a market title."""
    bucket_low, bucket_high = 0.0, 0.0
    temp_match = re.findall(r'(\d+)\s*[Â°F]', title)
    if len(temp_match) >= 2:
        bucket_low = float(temp_match[-2])
        bucket_high = float(temp_match[-1])
    elif len(temp_match) == 1:
        bucket_low = bucket_high = float(temp_match[0])

    title_lower = title.lower()
    if 'or below' in title_lower:
        bucket_low, bucket_high = -999.0, bucket_low or bucket_high or 0.0
    if 'or higher' in title_lower or 'or above' in title_lower:
        bucket_low, bucket_high = bucket_low or 0.0, 999.0
    return bucket_low, bucket_high


def _extract_market_date(title: str) -> str | None:
    """Extract YYYY-MM-DD market date from the title when available."""
    title_lower = title.lower()
    date_match = re.search(
        r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d+)',
        title_lower,
    )
    if not date_match:
        return None

    month_name = date_match.group(1)
    day = date_match.group(2)
    month_map = {
        'january': '01', 'february': '02', 'march': '03', 'april': '04',
        'may': '05', 'june': '06', 'july': '07', 'august': '08',
        'september': '09', 'october': '10', 'november': '11', 'december': '12',
    }
    year = datetime.now(timezone.utc).year
    return f"{year}-{month_map[month_name]}-{day.zfill(2)}"


def _market_review_id(position: dict) -> str:
    """Stable identifier for deduping reviewed paper trades."""
    parts = [
        str(position.get("condition_id") or ""),
        str(position.get("market_id") or ""),
        str(position.get("side") or ""),
        str(position.get("entry_ts") or ""),
        str(position.get("closed_at") or ""),
    ]
    return "|".join(parts)


def _position_pnl_metrics(side: str, shares: float, entry_price: float, current_price: float,
                          reserved_capital: float | None = None) -> tuple[float, float, float]:
    """Compute mark-to-market value, pnl, and pnl_pct for an open or closed position."""
    if side == "SELL":
        value = shares * (1 - current_price)
        pnl = shares * (entry_price - current_price)
        entry_cost = shares * (1 - entry_price)
    else:
        value = shares * current_price
        pnl = value - shares * entry_price
        entry_cost = shares * entry_price

    capital_base = max(0.01, reserved_capital or entry_cost or 0.01)
    pnl_pct = (pnl / capital_base) * 100
    return round(value, 2), round(pnl, 2), round(pnl_pct, 2)


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

    def _open_positions_for_city(self, city: str) -> int:
        if not city:
            return 0
        return sum(
            1 for p in self._open_positions.values()
            if str(p.get("city", "")).lower() == city.lower()
        )

    def _daily_review_due(self) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.state.get("last_learning_review_date") != today

    def mark_learning_review_complete(self):
        self.state["last_learning_review_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _save_state(self.state)

    def build_cycle_report(self, markets: list[dict], whale_positions: list[dict]) -> dict:
        """Summarize candidate quality before execution."""
        report = {
            "discovered_markets": len(markets),
            "markets_with_whale_signal": 0,
            "unique_us_cities": len({m.get("city") for m in markets if m.get("city")}),
            "liquid_candidates": 0,
            "ev_candidates": 0,
            "tradable_candidates": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        params = self.state.get("parameters", {})
        min_volume = float(params.get("min_volume", MIN_VOLUME))
        max_spread = float(params.get("max_spread", MAX_SPREAD))
        min_ev = float(params.get("min_ev", MIN_EV))
        max_price = float(params.get("max_price", 0.45))

        for market in markets:
            bid = market.get("bestBid")
            ask = market.get("bestAsk")
            volume = float(market.get("volume", 0) or 0)
            if bid is not None and ask is not None and volume >= min_volume:
                spread = float(ask) - float(bid)
                if 0 < float(bid) < 1 and 0 < float(ask) < 1 and spread <= max_spread:
                    report["liquid_candidates"] += 1
                    fair_price = self._estimate_fair_price(
                        market.get("question", "?"),
                        market.get("date"),
                        bucket_low=_extract_bucket_bounds(market.get("question", "?"))[0],
                        bucket_high=_extract_bucket_bounds(market.get("question", "?"))[1],
                    )
                    if fair_price is not None:
                        edge = abs(fair_price - float(bid))
                        if edge >= min_ev and float(bid) < max_price:
                            report["ev_candidates"] += 1
                            overlay = self._check_whale_overlay(
                                market.get("question", "?"),
                                "BUY" if fair_price > float(bid) else "SELL",
                                whale_positions,
                            )
                            if overlay["count"] > 0:
                                report["markets_with_whale_signal"] += 1
                            if overlay["adjustment"] > -0.3:
                                report["tradable_candidates"] += 1
        self.state["last_cycle_report"] = report
        _save_state(self.state)
        return report

    def _risk_exit_reason(self, pos: dict) -> str | None:
        """Return a stop reason for an open position, or None to keep holding."""
        params = self.state.get("parameters", {})
        stop_loss_pct = float(params.get("stop_loss_pct", STOP_LOSS_PCT))
        ev_flip_exit_buffer = max(
            float(params.get("ev_flip_exit_buffer", EV_FLIP_EXIT_BUFFER)),
            float(params.get("min_ev", MIN_EV)) / 2,
        )

        if pos.get("pnl_pct", 0) <= -stop_loss_pct:
            return "stop_loss"

        current_price = pos.get("current_price")
        if current_price is None:
            return None

        fair_price = self._estimate_fair_price(
            pos.get("title", "?"),
            pos.get("market_date"),
            bucket_low=pos.get("bucket_low"),
            bucket_high=pos.get("bucket_high"),
        )
        if fair_price is None:
            return None

        current_edge = fair_price - float(current_price)
        if pos.get("side") == "BUY" and current_edge <= -ev_flip_exit_buffer:
            return "ev_flip_stop"
        if pos.get("side") == "SELL" and current_edge >= ev_flip_exit_buffer:
            return "ev_flip_stop"
        return None

    def _close_position(self, pos_key: str, pos: dict, exit_price: float | None = None,
                        close_reason: str = "manual_close") -> dict:
        """Close a position at a given exit price and release capital back to bankroll."""
        pos["status"] = "closed"
        pos["closed_at"] = datetime.now(timezone.utc).isoformat()

        if exit_price is None:
            exit_price = pos.get("current_price", pos.get("entry_price", 0))
        exit_price = float(exit_price)

        value, pnl, pnl_pct = _position_pnl_metrics(
            pos.get("side", "BUY"),
            float(pos.get("shares", 0)),
            float(pos.get("entry_price", 0)),
            exit_price,
            pos.get("reserved_capital"),
        )
        pos["current_price"] = exit_price
        pos["value"] = value
        pos["pnl"] = pnl
        pos["pnl_pct"] = pnl_pct
        pos["exit_price"] = exit_price
        pos["close_reason"] = close_reason
        pos["settlement_value"] = value
        if close_reason == "resolved":
            pos["settlement_price"] = exit_price
        pos["resolved_outcome"] = "win" if pnl >= 0 else "loss"

        self.state["bankroll"] = round(self.state["bankroll"] + value, 2)
        if pnl >= 0:
            self.state["wins"] += 1
        else:
            self.state["losses"] += 1

        self.state["positions"][pos_key] = pos
        _log_trade({"action": "CLOSE", **pos})
        _save_state(self.state)
        if pos_key in self._open_positions:
            del self._open_positions[pos_key]
        logger.info("CLOSE %s via %s: $%.2f PnL", pos.get("title", "?")[:35], close_reason, pnl)
        return pos

    def apply_risk_stops(self) -> list[dict]:
        """Close open positions when stop-loss or EV-flip rules trigger."""
        closed = []
        for pos_key, pos in list(self._open_positions.items()):
            reason = self._risk_exit_reason(pos)
            if reason:
                closed.append(self._close_position(pos_key, pos, pos.get("current_price"), reason))
        return closed

    def evaluate_and_trade(self, market: dict, whale_positions: list[dict] = None,
                           fair_price: float = None) -> dict | None:
        """Core method: evaluate a market and execute a paper trade if edge exists."""
        title = market.get("question", "?")
        condition_id = market.get("conditionId", "")
        market_id = market.get("id", "")
        city = market.get("city", "")
        market_date = market.get("date")

        # --- Self-learning: extract temp range from title ---
        bucket_low, bucket_high = 0.0, 0.0
        temp_match = re.findall(r'(\d+)\s*[°F]', title)
        if len(temp_match) >= 2:
            bucket_low = float(temp_match[-2])
            bucket_high = float(temp_match[-1])
        elif len(temp_match) == 1:
            bucket_low = bucket_high = float(temp_match[0])
        if 'or below' in title.lower():
            bucket_low, bucket_high = -999.0, bucket_low or bucket_high or 0
        if 'or higher' in title.lower() or 'or above' in title.lower():
            bucket_low, bucket_high = bucket_low or 0, 999.0
        bucket_low, bucket_high = _extract_bucket_bounds(title)

        # --- Pre-compute forecast temp (shared with _estimate_fair_price) ---
        forecast_temp = None
        date_str_forecast = None
        extracted_market_date = _extract_market_date(title)
        if extracted_market_date:
            date_str_forecast = extracted_market_date
        title_lower = title.lower()
        for cn in FORECAST_LOCATIONS:
            if cn.lower() in title_lower:
                if not date_str_forecast:
                    date_match = re.search(r'(may|april|june|july|august|september|october)\s+(\d+)', title_lower)
                    if date_match:
                        month_name = date_match.group(1)
                        day = date_match.group(2)
                        month_map = {'may':'05','april':'04','june':'06','july':'07','august':'08','september':'09','october':'10'}
                        date_str_forecast = f"{datetime.now(timezone.utc).year}-{month_map.get(month_name,'05')}-{day.zfill(2)}"
                    else:
                        date_str_forecast = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
                if not date_str_forecast:
                    date_str_forecast = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
                unit = FORECAST_LOCATIONS[cn]['unit']
                forecast_temp = _get_forecast_temp(cn, date_str_forecast, unit)
                break

        # --- Learned parameters override ---
        params = self.state.get('parameters', {})
        current_min_ev = params.get('min_ev', EV_THRESHOLD)
        current_max_bet = params.get('max_bet', MAX_BET)
        current_max_price = params.get('max_price', 0.45)
        current_kelly_fraction = params.get('kelly_fraction', 0.25)
        current_min_volume = float(params.get('min_volume', MIN_VOLUME))
        current_max_spread = float(params.get('max_spread', MAX_SPREAD))
        current_max_positions = int(params.get('max_positions', MAX_OPEN_POSITIONS))
        current_max_city_positions = int(params.get('max_city_positions', MAX_CITY_POSITIONS))

        best_bid = market.get("bestBid")
        best_ask = market.get("bestAsk")
        volume = float(market.get("volume", 0) or 0)
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
        if spread > current_max_spread:
            return None
        if volume < current_min_volume:
            return None

        # Max price check from learned parameters
        if price >= current_max_price:
            return None

        # Default fair price if not provided
        if fair_price is None:
            fair_price = self._estimate_fair_price(
                title,
                date_str_forecast,
                forecast_temp,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
            )
        if fair_price is None or fair_price <= 0 or fair_price >= 1:
            return None

        # Compute EV
        ev = fair_price - price
        if abs(ev) < current_min_ev:
            return None

        direction = "BUY" if ev > 0 else "SELL"
        base_confidence = min(0.9, 0.5 + abs(ev))

        # Whale overlay
        whale_overlay = self._check_whale_overlay(title, direction, whale_positions or [])
        confidence = base_confidence + whale_overlay["adjustment"]
        confidence = max(0.05, min(0.99, confidence))

        # Whales are an overlay, not the source of truth. Strong opposition vetoes only weak edges.
        if whale_overlay["adjustment"] <= -0.20 and abs(ev) < max(current_min_ev * 2, 0.10):
            return None
        if confidence < 0.3:
            return None

        # Kelly sizing with whale boost
        kelly_raw = _kelly_fraction(fair_price, price) if direction == "BUY" else _kelly_fraction(1-fair_price, 1-price)
        kelly_adjusted = kelly_raw * (1.0 + whale_overlay["size_boost"])
        effective_kelly_fraction = max(0.0, float(current_kelly_fraction))
        allocation = _kelly_size(kelly_adjusted * effective_kelly_fraction, self.state["bankroll"])
        allocation = min(allocation, current_max_bet)

        # Cap exposure at 50% of bankroll and max 15 positions
        current_exposure = sum(p.get("value", 0) for p in self._open_positions.values())
        if current_exposure + allocation > self.state["bankroll"] * 0.5:
            return None
        if len(self._open_positions) >= current_max_positions:
            return None
        if city and self._open_positions_for_city(city) >= current_max_city_positions:
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
            "polymarket_slug": market.get("slug", ""),
            "market_id": market_id,
            "condition_id": condition_id,
            "title": title[:100],
            "city": city,
            "market_volume": volume,
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
            "kelly_fraction": round(kelly_adjusted * effective_kelly_fraction, 4),
            "entry_ts": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "bucket_low": bucket_low,
            "bucket_high": bucket_high,
            "market_date": market_date or date_str_forecast,
            "forecast_temp": forecast_temp,
            "forecast_source": "ecmwf",
            "reserved_capital": round(allocation, 2),
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
            value, pnl, pnl_pct = _position_pnl_metrics(
                pos.get('side', 'BUY'),
                float(pos.get('shares', 0)),
                float(pos.get('entry_price', 0)),
                float(price),
                pos.get('reserved_capital'),
            )
            pos['value'] = value
            pos['pnl'] = pnl
            pos["pnl_pct"] = pnl_pct
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
        return self._close_position(pos_key, pos, resolved_price, "resolved")

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
        city_tokens = [
            "new york city", "new york", "nyc", "chicago", "miami", "dallas", "denver",
            "seattle", "atlanta", "boston", "phoenix", "houston", "los angeles",
            "san francisco",
        ]
        city_pattern = "(" + "|".join(re.escape(token) for token in city_tokens) + ")"
        month_pattern = r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d+'
        a_cities = set(re.findall(city_pattern, a))
        b_cities = set(re.findall(city_pattern, b))
        a_dates = set(re.findall(month_pattern, a))
        b_dates = set(re.findall(month_pattern, b))
        city_match = bool(a_cities & b_cities)
        date_match = bool(a_dates & b_dates)
        return city_match and date_match

    def _make_slug(self, title: str, direction: str) -> str:
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:60]
        return f"{slug}-{direction}"

    def discover_weather_markets(self) -> list[dict]:
        """Discover curated US weather markets for the paper-trading experiment."""
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
            if FORECAST_LOCATIONS.get(city_name, {}).get("region") != "us":
                continue
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

    def _estimate_fair_price(self, title: str, date_str: str = None, forecast_temp: float = None,
                             bucket_low: float = None, bucket_high: float = None) -> Optional[float]:
        """Estimate fair price from real Open-Meteo ECMWF weather forecasts.
        If forecast_temp is provided, skips the API call."""
        title_lower = title.lower()
        city = None
        for city_name in FORECAST_LOCATIONS:
            if city_name.lower() in title_lower:
                city = city_name
                break
        if not city:
            return None

        if not date_str:
            date_str = _extract_market_date(title)
        if not date_str:
            date_str = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        loc = FORECAST_LOCATIONS.get(city)
        if not loc:
            return None

        unit = loc["unit"]
        if forecast_temp is None:
            forecast_temp = _get_forecast_temp(city, date_str, unit)
        if forecast_temp is None:
            return None

        if bucket_low is None or bucket_high is None:
            bucket_low, bucket_high = _extract_bucket_bounds(title)

        sigma = 4.0 if unit == "F" else 2.2
        if bucket_low == -999.0:
            upper = bucket_high + 0.5
            prob = _norm_cdf((upper - forecast_temp) / sigma)
        elif bucket_high == 999.0:
            lower = bucket_low - 0.5
            prob = 1.0 - _norm_cdf((lower - forecast_temp) / sigma)
        else:
            lower = bucket_low - 0.5
            upper = bucket_high + 0.5
            prob = _norm_cdf((upper - forecast_temp) / sigma) - _norm_cdf((lower - forecast_temp) / sigma)
        return round(max(0.01, min(0.99, prob)), 4)

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
        if forecast_temp is None:
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
                value, pnl, pnl_pct = _position_pnl_metrics(
                    pos.get('side', 'BUY'),
                    float(pos.get('shares', 0)),
                    float(pos.get('entry_price', 0)),
                    price,
                    pos.get('reserved_capital'),
                )
                pos['value'] = value
                pos['pnl'] = pnl
                pos['pnl_pct'] = pnl_pct
            except Exception:
                pass
        _save_state(self.state)

    def _get_actual_temp(self, city, date_str, unit='F'):
        """Get actual temperature from Open-Meteo ERA5 reanalysis (free, no key)."""
        loc = FORECAST_LOCATIONS.get(city)
        if not loc: return None
        temp_unit = 'fahrenheit' if unit == 'F' else 'celsius'
        url = (f'https://archive-api.open-meteo.com/v1/archive'
               f'?latitude={loc["lat"]}&longitude={loc["lon"]}'
               f'&start_date={date_str}&end_date={date_str}'
               f'&daily=temperature_2m_max&temperature_unit={temp_unit}')
        try:
            data = requests.get(url, timeout=8).json()
            if 'daily' in data:
                temps = data['daily'].get('temperature_2m_max', [])
                if temps and temps[0] is not None:
                    return round(temps[0]) if unit == 'F' else round(temps[0], 1)
        except Exception:
            pass
        return None

    def export_for_learning(self) -> list[dict]:
        """Export recently closed positions in self_learning format."""
        closed = self.get_closed_positions(50)
        result = []
        for p in closed:
            city = None
            for cn in FORECAST_LOCATIONS:
                if cn.lower() in p.get('title','').lower():
                    city = cn
                    break

            # Build forecast snapshot in the format self_learning expects
            forecast_snapshot = {
                'ts': p.get('closed_at', ''),
                'best_source': p.get('forecast_source', 'ecmwf'),
                'source': p.get('forecast_source', 'ecmwf'),
                'best': p.get('forecast_temp'),
                'temp': p.get('forecast_temp'),
            }

            # Get actual temp from ERA5
            date_str = p.get('market_date')
            if not date_str and p.get('title'):
                date_str = _extract_market_date(p['title'])
            if not date_str and p.get('closed_at'):
                date_str = p['closed_at'][:10]

            actual_temp = None
            if city and date_str:
                unit = FORECAST_LOCATIONS[city]['unit']
                actual_temp = self._get_actual_temp(city, date_str, unit)

            result.append({
                'city': city or '',
                'city_name': city or '',
                'date': date_str or '',
                'unit': FORECAST_LOCATIONS.get(city,{}).get('unit','F'),
                'status': 'resolved',
                'resolved_outcome': p.get('resolved_outcome', 'loss'),
                'pnl': p.get('pnl', 0),
                't_low': p.get('bucket_low', 0),
                't_high': p.get('bucket_high', 0),
                'forecast_snapshots': [forecast_snapshot] if forecast_snapshot['best'] else [],
                'actual_temp': actual_temp,
                'market_snapshots': [{'entry_price': p.get('entry_price', 0)}],
                'all_outcomes': [{
                    'market_id': str(p.get('market_id', '')),
                    'range': (p.get('bucket_low', 0), p.get('bucket_high', 0)),
                }],
                'source_trade_id': _market_review_id(p),
                'position': {
                    'market_id': str(p.get('market_id', '')),
                    'entry_price': p.get('entry_price', 0),
                    'exit_price': p.get('exit_price'),
                    'pnl': p.get('pnl', 0),
                    'status': 'closed',
                    'close_reason': p.get('close_reason', ''),
                },
            })
        return result

    def apply_learned_parameters(self):
        """Apply parameters learned by self_learning engine when explicitly requested."""
        notes_file = Path(__file__).parent / "data" / "strategy_notes.json"
        if not notes_file.exists():
            return {}
        try:
            notes = json.loads(notes_file.read_text())
            learned = notes.get('parameter_adjustments', {})
            if learned:
                self.state.setdefault('parameters', {}).update(learned)
                _save_state(self.state)
                logger.info(f'Applied learned parameters: {learned}')
                return learned
        except Exception:
            pass
        return {}

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
            "experiment": self.state.get("experiment", {}),
            "last_cycle_report": self.state.get("last_cycle_report", {}),
            "last_learning_review_date": self.state.get("last_learning_review_date"),
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
