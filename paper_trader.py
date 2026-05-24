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

from v6_logger import logger as v6log

logger = logging.getLogger(__name__)

PAPER_STATE_FILE = Path(__file__).parent / "data" / "paper_portfolio.json"
PAPER_TRADES_LOG = Path(__file__).parent / "data" / "paper_trades.jsonl"
TAIL_EXP_STATE_FILE = Path(__file__).parent / "data" / "tail_experiment_portfolio.json"
TAIL_EXP_TRADES_LOG = Path(__file__).parent / "data" / "tail_experiment_trades.jsonl"
STRATEGY1_MODE = "strategy-1-middle"
STRATEGY2_MODE = "strategy-2-tail"
STRATEGY3_MODE = "strategy-3-compound-tail"
STRATEGY1_STATE_FILE = Path(__file__).parent / "data" / "strategy1_middle_portfolio.json"
STRATEGY1_TRADES_LOG = Path(__file__).parent / "data" / "strategy1_middle_trades.jsonl"
STRATEGY2_STATE_FILE = Path(__file__).parent / "data" / "strategy2_tail_portfolio.json"
STRATEGY2_TRADES_LOG = Path(__file__).parent / "data" / "strategy2_tail_trades.jsonl"
STRATEGY3_STATE_FILE = Path(__file__).parent / "data" / "strategy3_compound_tail_portfolio.json"
STRATEGY3_TRADES_LOG = Path(__file__).parent / "data" / "strategy3_compound_tail_trades.jsonl"
RUNTIME_LOG = Path(__file__).parent / "data" / "strategy_runtime.jsonl"
FORECAST_CACHE: dict[tuple[str, str, str], Optional[float]] = {}

DEFAULT_BANKROLL = 100.0
MAX_BET = 2.0
MIN_EV = 0.08    # v2: raised from 0.05 — 49.5% WR was a coin flip, filter low-quality entries
EV_THRESHOLD = 0.08  # aligned with MIN_EV raise
MIN_VOLUME = 200.0
MAX_SPREAD = 0.08
MAX_OPEN_POSITIONS = 12
MAX_CITY_POSITIONS = 2
EXPERIMENT_DAYS = 3
STOP_LOSS_PCT = 70.0  # v2: tightened from 90% — 72 stop_loss trades avg'd -$1.63, cut earlier
EV_FLIP_EXIT_BUFFER = 0.02
TAKE_PROFIT_PCT = 100.0
TAKE_PROFIT_MAX_REMAINING_EDGE = 0.15
HARD_TAKE_PROFIT_PCT = 400.0
TRAILING_STOP_PCT = 55.0  # v2: raised from 45% — let winners run more, 45% was catching too early
MIN_EDGE_FLOOR = 0.05  # lowered from 0.08 — tighter edge exhaustion tolerance
MAX_POSITION_DAYS = 3
# ─── V6 Tail Experiment Constants ──────────────────────────────────────────
TAIL_EXP_TRADES_LIMIT: int | None = None  # continuous mode; risk limits still cap exposure
TAIL_EXP_ENTRY_MAX = 0.01         # v6.1: raised from $0.005 to unlock more tradeable markets
TAIL_EXP_HARD_SKIP_PRICE = 0.015  # skip $0.015+ (was $0.006, aligned with entry_max raise)
TAIL_EXP_FIXED_ALLOCATION = 2.0   # $1-$2 per trade default
TAIL_EXP_MIN_VOLUME = 5.0         # v6.1: lowered from 10.0; tail buckets have low volume naturally

# V6 Signal: fair_value / entry_price >= 3x
TAIL_EXP_MIN_EV_RATIO = 3.0

# V6 Profit-taking (flexible percentage ranges)
TAIL_EXP_MFE50_SELL_PCT = 0.15    # sell 10-20% at +50% MFE (optional)
TAIL_EXP_X2_SELL_PCT = 0.40       # sell 25-50% at x2 (recover stake)
TAIL_EXP_X3_SELL_PCT = 0.20       # sell 15-25% at x3
TAIL_EXP_X4_SELL_PCT = 0.30       # sell 25-40% at x4-x6
TAIL_EXP_RUNNER_PCT = 0.15        # keep 10-25% runner

# V6 Trailing
TAIL_EXP_TRAILING_PCT = 30.0      # 25-35% trail from peak, activates at +50% MFE
TAIL_EXP_MFE_PROTECT = 50.0       # MFE threshold for protection logic

# V6 Minimum hold
MIN_HOLD_MINUTES = 60
MIN_HOLD_MINUTES_001 = 120

# ─── V6.1 Tail Experiment Additions ─────────────────────────────────────────
TAIL_EXP_MARKET_COOLDOWN_MINUTES = 360       # 6h cooldown after non-resolution close
TAIL_EXP_MAX_ENTRIES_PER_MARKET_PER_DAY = 2  # max re-entries per market per calendar day
TAIL_EXP_SUSPICIOUS_VOLUME = 25.0            # treat $0.001 markets with volume <$25 as suspicious
EDGE_EXHAUSTED_COOLDOWN_MINUTES = 720         # 12h cooldown after edge_exhausted (was undefined — bug fix)

STRATEGY1_CITIES = {"Miami", "Houston", "Dallas", "Seattle"}
STRATEGY1_MAX_ENTRY = 0.005
STRATEGY1_MIN_VOLUME = 500.0
STRATEGY1_MIN_EV_RATIO = 20.0
STRATEGY1_FIXED_ALLOCATION = 1.0

STRATEGY2_MAX_ENTRY = 0.002
STRATEGY2_MIN_VOLUME = 10.0
STRATEGY2_MIN_EV_RATIO = 5.0
STRATEGY2_MIN_FORECAST_GAP_F = 7.0
STRATEGY2_FIXED_ALLOCATION = 1.0

STRATEGY3_BASE_STAKE = 1.0
STRATEGY3_PROFIT_REINVEST_PCT = 0.50
STRATEGY3_MAX_STAKE = 5.0
STRATEGY3_LIQUIDITY_MULTIPLIER = 20.0

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
    # Denver removed: 50% WR, near-zero PnL
    "Seattle": {"lat": 47.4499, "lon": -122.311, "station": "KSEA", "unit": "F", "region": "us"},
    "Atlanta": {"lat": 33.6407, "lon": -84.4277, "station": "KATL", "unit": "F", "region": "us"},
    "Boston": {"lat": 42.3662, "lon": -71.0621, "station": "KBOS", "unit": "F", "region": "us"},
    "Phoenix": {"lat": 33.4342, "lon": -112.008, "station": "KPHX", "unit": "F", "region": "us"},
    "Houston": {"lat": 29.9901, "lon": -95.3368, "station": "KIAH", "unit": "F", "region": "us"},
    # San Francisco removed: 57% WR, negative PnL
    # Los Angeles removed: 36% WR, -$2.00 PnL
    "London": {"lat": 51.5074, "lon": -0.1278, "station": "EGLL", "unit": "C", "region": "eu"},
    "Paris": {"lat": 48.8534, "lon": 2.3488, "station": "LFPG", "unit": "C", "region": "eu"},
    "Tokyo": {"lat": 35.6895, "lon": 139.6917, "station": "RJTT", "unit": "C", "region": "as"},
    "Berlin": {"lat": 52.5200, "lon": 13.4050, "station": "EDDB", "unit": "C", "region": "eu"},
    "Austin": {"lat": 30.2672, "lon": -97.7431, "station": "KAUS", "unit": "F", "region": "us"},
    "Warsaw": {"lat": 52.2297, "lon": 21.0122, "station": "EPWA", "unit": "C", "region": "eu"},
    "Wellington": {"lat": -41.2865, "lon": 174.7762, "station": "NZWN", "unit": "C", "region": "eu"},
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

def _load_state(path: Path = None) -> dict:
    state_file = path or PAPER_STATE_FILE
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, Exception):
            pass
    return {
        "bankroll": DEFAULT_BANKROLL,
        "starting_bankroll": DEFAULT_BANKROLL,
        "positions": {},
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "wins_real": 0,
        "wins_flat": 0,
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
            "take_profit_pct": TAKE_PROFIT_PCT,
            "take_profit_max_remaining_edge": TAKE_PROFIT_MAX_REMAINING_EDGE,
            "hard_take_profit_pct": HARD_TAKE_PROFIT_PCT,
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


def _save_state(state: dict, path: Path = None):
    save_path = path or PAPER_STATE_FILE
    save_path.parent.mkdir(exist_ok=True)
    save_path.write_text(json.dumps(state, indent=2, default=str))


def _log_trade(entry: dict, log_path: Path = None):
    log_file = log_path or PAPER_TRADES_LOG
    log_file.parent.mkdir(exist_ok=True)
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    strategy = _mode_for_log(log_file)
    if strategy in {STRATEGY1_MODE, STRATEGY2_MODE}:
        _log_runtime_event(
            strategy,
            f"TRADE_{entry.get('action', 'EVENT')}",
            entry.get("title", "trade_event"),
            condition_id=entry.get("condition_id"),
            market_id=entry.get("market_id"),
            side=entry.get("side"),
            price=entry.get("current_price", entry.get("entry_price")),
            entry_price=entry.get("entry_price"),
            value=entry.get("value"),
            pnl=entry.get("pnl"),
            pnl_pct=entry.get("pnl_pct"),
            close_reason=entry.get("close_reason"),
        )


def _mode_for_log(log_path: Path) -> str:
    if log_path == STRATEGY1_TRADES_LOG:
        return STRATEGY1_MODE
    if log_path == STRATEGY2_TRADES_LOG:
        return STRATEGY2_MODE
    if log_path == STRATEGY3_TRADES_LOG:
        return STRATEGY3_MODE
    if log_path == TAIL_EXP_TRADES_LOG:
        return "tail-experiment"
    return "normal"


def _log_runtime_event(strategy: str, event_type: str, message: str, **details):
    """Append a structured event for dashboard/realtime analysis."""
    RUNTIME_LOG.parent.mkdir(exist_ok=True)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "event_type": event_type,
        "message": message,
        "details": details,
    }
    with open(RUNTIME_LOG, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")

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


def _forecast_gap_f(forecast_temp: float | None, bucket_low: float, bucket_high: float) -> float | None:
    """Distance from forecast to the traded bucket; zero means forecast is inside."""
    if forecast_temp is None:
        return None
    ft = float(forecast_temp)
    if bucket_low > -900 and bucket_high < 900:
        if bucket_low <= ft <= bucket_high:
            return 0.0
        return min(abs(ft - bucket_low), abs(ft - bucket_high))
    if bucket_low <= -900 and bucket_high < 900:
        return abs(ft - bucket_high)
    if bucket_high >= 900 and bucket_low > -900:
        return abs(ft - bucket_low)
    return None


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


def _position_base_key(condition_id: str, direction: str, slug: str) -> str:
    """Stable market/side key used only to detect an already-open position."""
    return f"{condition_id}-{direction}" if condition_id else slug


def _new_position_key(condition_id: str, direction: str, slug: str, entry_ts: str, sequence: int) -> str:
    """Immutable trade key; every entry must keep its own history record."""
    base = _position_base_key(condition_id, direction, slug)
    safe_ts = re.sub(r"[^0-9A-Za-z]+", "", entry_ts)
    return f"{base}-{sequence:06d}-{safe_ts}"


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


def _position_net_pnl(position: dict) -> float:
    exits = position.get("exits", []) or []
    return float(position.get("pnl", 0) or 0) + sum(
        float(exit_row.get("pnl", 0) or 0)
        for exit_row in exits
        if isinstance(exit_row, dict)
    )


class PaperTrader:
    """Paper trading engine: EV-driven with whale overlay.
    Self-learning adjusts parameters as trades resolve."""

    def __init__(self, bankroll: float = None, mode: str = 'normal'):
        self.profile_mode = mode
        self.mode = mode
        if mode == STRATEGY1_MODE:
            self.state_file = STRATEGY1_STATE_FILE
            self.trades_log = STRATEGY1_TRADES_LOG
        elif mode in {STRATEGY2_MODE, STRATEGY3_MODE}:
            if mode == STRATEGY2_MODE:
                self.state_file = STRATEGY2_STATE_FILE
                self.trades_log = STRATEGY2_TRADES_LOG
            else:
                self.state_file = STRATEGY3_STATE_FILE
                self.trades_log = STRATEGY3_TRADES_LOG
            self.mode = 'tail-experiment'
        elif mode == 'tail-experiment':
            self.state_file = TAIL_EXP_STATE_FILE
            self.trades_log = TAIL_EXP_TRADES_LOG
        else:
            self.state_file = PAPER_STATE_FILE
            self.trades_log = PAPER_TRADES_LOG
        self.state = _load_state(self.state_file)
        self.state.setdefault("parameters", {})
        self.state["parameters"].setdefault("stop_loss_pct", STOP_LOSS_PCT)
        self.state["parameters"].setdefault("ev_flip_exit_buffer", EV_FLIP_EXIT_BUFFER)
        self.state["parameters"].setdefault("take_profit_pct", TAKE_PROFIT_PCT)
        self.state["parameters"].setdefault("take_profit_max_remaining_edge", TAKE_PROFIT_MAX_REMAINING_EDGE)
        self.state["parameters"].setdefault("hard_take_profit_pct", HARD_TAKE_PROFIT_PCT)
        self.state["parameters"].setdefault("trailing_stop_pct", TRAILING_STOP_PCT)
        self.state["parameters"].setdefault("min_edge_floor", MIN_EDGE_FLOOR)
        self.state["parameters"].setdefault("max_position_days", MAX_POSITION_DAYS)
        if mode == 'tail-experiment':
            self.state["parameters"]["trailing_stop_pct"] = TAIL_EXP_TRAILING_PCT
            self.state['parameters']['hard_take_profit_pct'] = HARD_TAKE_PROFIT_PCT
        if mode == STRATEGY1_MODE:
            self.state["parameters"].update({
                "min_ev": 0.05,
                "max_bet": STRATEGY1_FIXED_ALLOCATION,
                "max_price": 0.006,
                "min_volume": STRATEGY1_MIN_VOLUME,
                "max_positions": 8,
                "max_city_positions": 2,
                "kelly_fraction": 0.0,
            })
            self.state["experiment"] = {
                "scope": STRATEGY1_MODE,
                "name": "Cheap Middle Bucket Spike",
                "rules": "BUY middle buckets only; entry <=0.5c; volume >=500; fair/entry >=20x; Miami/Houston/Dallas/Seattle.",
                "started_at": self.state.get("created_at") or datetime.now(timezone.utc).isoformat(),
            }
        if mode == STRATEGY2_MODE:
            self.state["parameters"].update({
                "trailing_stop_pct": TAIL_EXP_TRAILING_PCT,
                "hard_take_profit_pct": HARD_TAKE_PROFIT_PCT,
                "max_positions": 6,
                "max_city_positions": 2,
                "min_volume": STRATEGY2_MIN_VOLUME,
                "max_price": STRATEGY2_MAX_ENTRY,
            })
            self.state["experiment"] = {
                "scope": STRATEGY2_MODE,
                "name": "Ultra-Cheap Tail Spike Capture",
                "rules": "BUY tails only; entry <=0.2c; fair/entry >=5x; forecast gap >=7F; volume >=10; staged exits at x2/x4.",
                "started_at": self.state.get("created_at") or datetime.now(timezone.utc).isoformat(),
            }
        if mode == STRATEGY3_MODE:
            self.state["parameters"].update({
                "trailing_stop_pct": TAIL_EXP_TRAILING_PCT,
                "hard_take_profit_pct": HARD_TAKE_PROFIT_PCT,
                "max_positions": 6,
                "max_city_positions": 2,
                "min_volume": STRATEGY2_MIN_VOLUME,
                "max_price": STRATEGY2_MAX_ENTRY,
                "base_stake": STRATEGY3_BASE_STAKE,
                "profit_reinvest_pct": STRATEGY3_PROFIT_REINVEST_PCT,
                "max_stake": STRATEGY3_MAX_STAKE,
                "liquidity_multiplier": STRATEGY3_LIQUIDITY_MULTIPLIER,
            })
            self.state.setdefault("stake_state", {
                "base_stake": STRATEGY3_BASE_STAKE,
                "current_stake": STRATEGY3_BASE_STAKE,
                "max_stake": STRATEGY3_MAX_STAKE,
                "profit_reinvest_pct": STRATEGY3_PROFIT_REINVEST_PCT,
                "liquidity_multiplier": STRATEGY3_LIQUIDITY_MULTIPLIER,
                "last_trade_pnl": 0.0,
                "last_update_reason": "initialized",
            })
            self.state["experiment"] = {
                "scope": STRATEGY3_MODE,
                "name": "Compound Tail Stake",
                "rules": "Strategy 2 tail entries; base $1 stake; reinvest 50% of winning profit into next stake; reset to base after losses; cap stake at $5; skip markets with volume below 20x planned stake.",
                "started_at": self.state.get("created_at") or datetime.now(timezone.utc).isoformat(),
            }
        self.state.setdefault('wins_real', 0)
        self.state.setdefault('wins_flat', 0)
        self.state.setdefault('losses', 0)
        self.state.setdefault('recently_edge_exhausted', {})
        # V6.1: market-level cooldown + daily entry tally
        self.state.setdefault('market_close_cooldowns', {})
        self.state.setdefault('market_entry_counts', {})
        if bankroll is not None:
            self.state["bankroll"] = bankroll
            self.state["starting_bankroll"] = bankroll
        # Reconcile counters from actual position data on every load
        self.reconcile_counters(self.state)
        reconciled = float(self.state.get("bankroll", 0))
        if self.profile_mode not in {STRATEGY1_MODE, STRATEGY2_MODE, STRATEGY3_MODE}:
            reconciled = self.reconcile_bankroll(self.state)
        if self.profile_mode not in {STRATEGY1_MODE, STRATEGY2_MODE, STRATEGY3_MODE} and abs(reconciled - float(self.state.get("bankroll", 0))) > 0.02:
            logger.warning("Bankroll drifted by $%.2f — reconciling from positions", 
                          reconciled - float(self.state.get("bankroll", 0)))
            self.state["bankroll"] = reconciled
        self.state.setdefault("peak_equity", float(self.state.get("bankroll", 100.0)))
        self._update_peak_equity()
        self._open_positions = {k: v for k, v in self.state.get("positions", {}).items()
                                if v.get("status") == "open"}
        if self.profile_mode in {STRATEGY1_MODE, STRATEGY2_MODE, STRATEGY3_MODE}:
            _save_state(self.state, self.state_file)

    def _reload(self):
        """Reload state from disk - keeps dashboard in sync with cron jobs."""
        disk_state = _load_state(self.state_file)
        if 'positions' in disk_state:
            self.state = disk_state
            self._open_positions = {k: v for k, v in self.state.get("positions", {}).items()
                                    if v.get("status") == "open"}

    @staticmethod
    def reconcile_counters(state: dict) -> dict:
        """Recompute all aggregate counters from actual position data.

        Returns the state dict with corrected wins/losses/wins_real/wins_flat.
        total_trades is preserved (it counts entries, not positions in dict).
        """
        wins = 0
        losses = 0
        wins_real = 0
        wins_flat = 0

        for pos in state.get("positions", {}).values():
            if pos.get("status") != "closed":
                continue
            exits = pos.get("exits", []) or []
            pnl = float(pos.get("pnl", 0)) + sum(
                float(e.get("pnl", 0) or 0)
                for e in exits
                if isinstance(e, dict)
            )
            outcome = pos.get("outcome_class", "")
            if pnl > 0.05 or outcome == "real_win":
                wins += 1
                wins_real += 1
            elif pnl >= -0.05 or outcome == "flat":
                wins_flat += 1
            else:
                losses += 1

        state["wins"] = wins
        state["losses"] = losses
        state["wins_real"] = wins_real
        state["wins_flat"] = wins_flat
        return state

    @staticmethod
    def reconcile_bankroll(state: dict) -> float:
        """Recompute bankroll from first principles using all positions.
        
        Uses the safe formula: starting + sum(closed PnL) - sum(open allocation).
        Caps result to [starting*0.5, starting*4] to reject impossible values
        from corrupted states (positions added by repair without corresponding
        bankroll debits).
        """
        starting = float(state.get("starting_bankroll", 100.0))
        total_closed_pnl = 0.0
        open_allocation = 0.0
        
        for pos in state.get("positions", {}).values():
            status = pos.get("status", "open")
            if status == "open":
                rc = pos.get("reserved_capital")
                if rc is not None:
                    alloc = float(rc)
                else:
                    side = pos.get("side", "BUY")
                    shares = float(pos.get("shares", 0))
                    entry = float(pos.get("entry_price", 0))
                    alloc = shares * entry if side == "BUY" else shares * (1 - entry)
                open_allocation += alloc
            elif status == "closed":
                total_closed_pnl += float(pos.get("pnl", 0))
        
        reconciled = starting + total_closed_pnl - open_allocation
        # Sanity check: reject values outside [50% starting, 1000% starting]
        # (allows for tail strategy winners while catching real corruption)
        max_allowed = starting * 10.0
        min_allowed = starting * 0.5
        if reconciled > max_allowed or reconciled < min_allowed:
            # Fall back to recent-trades estimate: just WARN and return starting
            logger.warning(
                "Bankroll reconciliation gave $%.2f (out of bounds [%.2f, %.2f]) — "
                "likely from state repair artifacts. Using current bankroll.",
                reconciled, min_allowed, max_allowed,
            )
            return float(state.get("bankroll", starting))
        return round(reconciled, 2)

    def equity(self) -> float:
        """Current equity = bankroll + sum of open position values."""
        bankroll = float(self.state.get("bankroll", 0))
        open_value = sum(
            float(p.get("value", 0))
            for p in self.state.get("positions", {}).values()
            if p.get("status") == "open"
        )
        return round(bankroll + open_value, 2)

    def _update_peak_equity(self):
        """Update peak_equity in state if current equity is higher."""
        current_eq = self.equity()
        peak = float(self.state.get("peak_equity", 0))
        if current_eq > peak:
            self.state["peak_equity"] = current_eq

    def drawdown_pct(self) -> float:
        """Drawdown from peak equity as a percentage (0 = no drawdown)."""
        peak = float(self.state.get("peak_equity", 0))
        if peak <= 0:
            return 0.0
        current_eq = self.equity()
        return round((peak - current_eq) / peak * 100, 1)

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
        _save_state(self.state, self.state_file)

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
                            if overlay["aligned"]:
                                report["tradable_candidates"] += 1
        self.state["last_cycle_report"] = report
        _save_state(self.state, self.state_file)
        return report

    def _risk_exit_reason(self, pos: dict) -> str | None:
        """Return a stop reason for an open position, or None to keep holding.

        V6 Exit priority (flexible partials, restricted edge-exhausted):
          1. x4-x6 sell — sell 25-40%, keep runner
          2. x3 sell — sell 15-25% (if not already x4 exited)
          3. x2 sell — sell 25-50% (if not already x4 exited)
          4. +50% MFE sell — optional sell 10-20%
          5. Trailing stop — activates at +50% MFE, trails 25-35% from peak
          6. Profit protection — close if MFE>=+50% and PnL<=0 (NOT if partial profit taken)
          7. Edge exhausted — heavily restricted (no partial profit, never hit +50%, min hold passed)
          8. Stop-loss 90% — only for trades that NEVER hit +50%
          9. Hard TP 400% — unconditional
        """
        params = self.state.get("parameters", {})
        stop_loss_pct = float(params.get("stop_loss_pct", STOP_LOSS_PCT))
        hard_take_profit_pct = float(params.get("hard_take_profit_pct", HARD_TAKE_PROFIT_PCT))
        trailing_stop_pct = float(params.get("trailing_stop_pct", TAIL_EXP_TRAILING_PCT))
        min_edge_floor = float(params.get("min_edge_floor", MIN_EDGE_FLOOR))
        mfe_protect = float(params.get("mfe_protect", TAIL_EXP_MFE_PROTECT))
        max_position_days = int(params.get("max_position_days", MAX_POSITION_DAYS))

        current_pnl = pos.get("pnl_pct", 0)
        current_price = pos.get("current_price")
        entry_price = float(pos.get("entry_price", 0))
        peak_pnl = pos.get("peak_pnl_pct", current_pnl)

        # Track MFE as percentage of entry price
        mfe_price = pos.get("mfe_price")
        mfe_pct = ((mfe_price - entry_price) / entry_price * 100) if mfe_price and entry_price > 0 else 0.0

        # V6.1: Real-bid MFE validation — in low-volume markets, price spikes are fake
        if self.mode == 'tail-experiment' and entry_price > 0 and mfe_price and entry_price > 0:
            market_volume = float(pos.get("market_volume", 0) or 0)
            if market_volume < TAIL_EXP_SUSPICIOUS_VOLUME and mfe_price >= entry_price * 2:
                # MFE spike is likely from the bot's own order in thin book — don't trust it
                mfe_pct = 0.0

        hit_mfe_protect = mfe_pct >= mfe_protect  # MFE reached +50%

        # --- Compute age in minutes ---
        entry_ts = pos.get("entry_ts") or pos.get("opened_at")
        age_minutes = 0
        if entry_ts:
            try:
                opened = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                age_minutes = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
            except Exception:
                pass

        min_hold = MIN_HOLD_MINUTES_001 if entry_price <= 0.001 else MIN_HOLD_MINUTES

        # Flags for what's already been sold
        x2_sold = pos.get("x2_sold", False)
        x3_sold = pos.get("x3_sold", False)
        x4_exit = pos.get("x4_exit", False)
        mfe50_sold = pos.get("mfe50_sold", False)
        has_partial_profit = pos.get("has_partial_profit", False)

        # Priority 1: x4-x6 — sell 25-40% of original, keep runner
        if self.mode == 'tail-experiment' and current_price is not None:
            x2_level = pos.get("x2_level")
            x3_level = pos.get("x3_level")
            x4_level = pos.get("x4_level")

            # Priority 1: x4-x6 — sell most, keep runner (V6: no x2/x3 prerequisite)
            if not x4_exit and x4_level is not None and current_price >= x4_level:
                return "x4_exit"

            # Priority 2: x3 — sell 15-25% (only if x4 hasn't already handled it)
            if not x4_exit and not x3_sold and x3_level is not None and current_price >= x3_level:
                return "x3_sell"

            # Priority 3: x2 — sell 25-50% (only if x4 hasn't already handled it)
            if not x4_exit and not x2_sold and x2_level is not None and current_price >= x2_level:
                return "x2_sell"

            # Priority 4: +50% MFE optional sell 10-20%
            if hit_mfe_protect and not mfe50_sold and not x4_exit:
                return "mfe50_sell"

        # Priority 5: Trailing stop — activates at +50% MFE, trails from peak
        if hit_mfe_protect:
            if peak_pnl > 0 and current_pnl < peak_pnl * (1 - trailing_stop_pct / 100):
                return "trailing_stop"

        # Priority 6: Profit protection — close if MFE>=+50% AND PnL<=0 (NOT if partial profit taken)
        if hit_mfe_protect and current_pnl <= 0 and not has_partial_profit:
            return "profit_protect"

        # Priority 7: Edge exhausted — heavily restricted
        # ONLY if: min hold passed AND PnL <= 0 AND MFE never hit +50% AND no partial profit taken
        if not hit_mfe_protect and current_price is not None and not has_partial_profit:
            fair_price = self._estimate_fair_price(
                pos.get("title", "?"),
                pos.get("market_date"),
                bucket_low=pos.get("bucket_low"),
                bucket_high=pos.get("bucket_high"),
            )
            if fair_price is not None:
                current_edge = fair_price - float(current_price)
                abs_edge = abs(current_edge)
                if abs_edge <= min_edge_floor:
                    if age_minutes >= min_hold and current_pnl <= 0:
                        return "edge_exhausted"
                    else:
                        # Log blocked edge exit
                        if hasattr(self, 'logger') and self.logger:
                            self.logger.log_signal("EDGE_EXIT_BLOCKED",
                                f"Edge={abs_edge:.4f}, age={age_minutes:.0f}m, PnL={current_pnl:.1f}%, "
                                f"hold_ok={age_minutes >= min_hold}, pnl_ok={current_pnl <= 0}, "
                                f"partial={has_partial_profit}, mfe={hit_mfe_protect}",
                                pos.get("title", "?")[:40])

        # Priority 8: Emergency stop-loss — only for non-protected trades
        if not hit_mfe_protect and current_pnl <= -stop_loss_pct:
            return "stop_loss"

        # Priority 9: Hard TP (unconditional)
        if current_pnl >= hard_take_profit_pct:
            return "hard_take_profit"

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
        # Classify flat exits separately (tail-experiment tracks real wins vs flat vs losses)
        if pnl > 0.05:
            pos["outcome_class"] = "real_win"
        elif pnl >= -0.05:
            pos["outcome_class"] = "flat"
        else:
            pos["outcome_class"] = "loss"

        self.state["bankroll"] = round(self.state["bankroll"] + value, 2)
        if self.profile_mode == STRATEGY3_MODE:
            self._update_strategy3_stake(pos)
        # Reconcile all counters from actual position data (prevents drift)
        self.reconcile_counters(self.state)

        # Track edge_exhausted for market cooldown
        if close_reason == "edge_exhausted":
            market_id = pos.get("market_id")
            if market_id:
                self.state.setdefault("recently_edge_exhausted", {})
                self.state["recently_edge_exhausted"][market_id] = datetime.now(timezone.utc).isoformat()

        # V6.1: cooldown for any non-resolution close (stops re-entry loop)
        if (self.mode == 'tail-experiment' or self.profile_mode == STRATEGY1_MODE) and close_reason != "resolved":
            market_id = pos.get("market_id")
            if market_id:
                self.state.setdefault("market_close_cooldowns", {})
                self.state["market_close_cooldowns"][market_id] = datetime.now(timezone.utc).isoformat()

        self.state["positions"][pos_key] = pos
        _log_trade({"action": "CLOSE", **pos}, self.trades_log)
        _save_state(self.state, self.state_file)
        if pos_key in self._open_positions:
            del self._open_positions[pos_key]
        self._update_peak_equity()

        # V6 event log
        if self.mode == 'tail-experiment':
            v6log.log_full_close(
                position_id=pos_key, market_id=pos.get("market_id", ""),
                reason=close_reason, price=exit_price,
                shares=float(pos.get("shares", 0)),
                cash_delta=value,
                realized_pnl_delta=pnl,
                bankroll_before=round(self.state['bankroll'] - value, 2),
                bankroll_after=round(self.state['bankroll'], 2),
                mfe_pct=pos.get("peak_pnl_pct", 0),
                outcome="win" if pnl >= 0 else "loss",
            )
        logger.info("CLOSE %s via %s: $%.2f PnL", pos.get("title", "?")[:35], close_reason, pnl)
        return pos

    def _update_strategy3_stake(self, pos: dict) -> None:
        stake_state = self.state.setdefault("stake_state", {})
        params = self.state.get("parameters", {})
        base_stake = float(stake_state.get("base_stake", params.get("base_stake", STRATEGY3_BASE_STAKE)))
        max_stake = float(stake_state.get("max_stake", params.get("max_stake", STRATEGY3_MAX_STAKE)))
        reinvest_pct = float(stake_state.get(
            "profit_reinvest_pct",
            params.get("profit_reinvest_pct", STRATEGY3_PROFIT_REINVEST_PCT),
        ))
        current_stake = float(stake_state.get("current_stake", base_stake))
        net_pnl = _position_net_pnl(pos)

        if net_pnl > 0.05:
            next_stake = min(max_stake, current_stake + net_pnl * reinvest_pct)
            reason = "win_reinvest"
        elif net_pnl < -0.05:
            next_stake = base_stake
            reason = "loss_reset"
        else:
            next_stake = current_stake
            reason = "flat_hold"

        stake_state.update({
            "base_stake": round(base_stake, 2),
            "current_stake": round(next_stake, 2),
            "previous_stake": round(current_stake, 2),
            "max_stake": round(max_stake, 2),
            "profit_reinvest_pct": reinvest_pct,
            "last_trade_pnl": round(net_pnl, 2),
            "last_update_reason": reason,
            "last_update_ts": datetime.now(timezone.utc).isoformat(),
        })
        self.state["stake_state"] = stake_state
        _log_runtime_event(
            STRATEGY3_MODE,
            "STAKE_UPDATE",
            reason,
            pnl=round(net_pnl, 2),
            previous_stake=round(current_stake, 2),
            next_stake=round(next_stake, 2),
            position_id=pos.get("position_id"),
        )

    def _partial_close(self, pos_key: str, pos: dict, exit_price: float | None = None,
                       reason: str = "x2_sell") -> dict | None:
        """Sell a portion of shares at multiple-based exit levels (V6).

        x2_sell: sell TAIL_EXP_X2_SELL_PCT of original shares at +100% → free-roll.
        x3_sell: sell TAIL_EXP_X3_SELL_PCT of original shares at +200%.
        x4_exit: sell most remaining, keep TAIL_EXP_RUNNER_PCT runner. Position stays open with runner.
        mfe50_sell: sell TAIL_EXP_MFE50_SELL_PCT of original shares at +50% MFE.

        Returns the exit record or None if position fully closed.
        """
        if exit_price is None:
            exit_price = pos.get("current_price", pos.get("entry_price", 0))
        exit_price = float(exit_price)

        original_shares = float(pos.get("original_shares", pos.get("shares", 0)))
        entry_price = float(pos.get("entry_price", 0))
        remaining = float(pos.get("shares", 0))

        if reason == "x2_sell":
            shares_to_sell = round(original_shares * TAIL_EXP_X2_SELL_PCT, 2)
            new_flag = "x2_sold"
        elif reason == "x3_sell":
            shares_to_sell = round(original_shares * TAIL_EXP_X3_SELL_PCT, 2)
            new_flag = "x3_sold"
        elif reason == "x4_exit":
            # Sell most remaining, keep only the runner
            runner_shares = round(original_shares * TAIL_EXP_RUNNER_PCT, 2)
            shares_to_sell = round(remaining - runner_shares, 2)
            new_flag = "x4_exit"
            if shares_to_sell <= 0:
                return None
        elif reason == "mfe50_sell":
            shares_to_sell = round(original_shares * TAIL_EXP_MFE50_SELL_PCT, 2)
            new_flag = "mfe50_sold"
        else:
            return None

        # Clamp to actual remaining
        shares_to_sell = min(shares_to_sell, remaining)
        if shares_to_sell <= 0:
            return None

        pnl_on_sale = round(shares_to_sell * (exit_price - entry_price), 2)
        proceeds = round(shares_to_sell * exit_price, 2)

        # Record the exit
        exit_record = {
            "shares": round(shares_to_sell, 4),
            "price": exit_price,
            "pnl": pnl_on_sale,
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        pos.setdefault("exits", []).append(exit_record)
        pos[new_flag] = True
        pos["has_partial_profit"] = True

        # Reduce shares
        remaining = round(remaining - shares_to_sell, 4)
        pos["shares"] = remaining

        # Return proceeds to bankroll
        bankroll_before_close = round(self.state["bankroll"], 2)
        self.state["bankroll"] = round(self.state["bankroll"] + proceeds, 2)

        if remaining <= 0 or reason == "x4_exit":
            # x4_exit: runner stays open — don't close position
            if reason == "x4_exit" and remaining > 0:
                pass  # runner stays open
            else:
                self._close_position(pos_key, pos, exit_price, reason)
                return None

        # Update value/pnl for remaining shares
        value, pnl, pnl_pct = _position_pnl_metrics(
            pos.get("side", "BUY"),
            remaining,
            entry_price,
            exit_price,
            pos.get("reserved_capital"),
        )
        pos["current_price"] = exit_price
        pos["value"] = value
        pos["pnl"] = pnl
        pos["pnl_pct"] = pnl_pct

        _save_state(self.state, self.state_file)
        _log_trade({"action": "PARTIAL_CLOSE", **pos, "partial_exit": exit_record}, self.trades_log)

        # V6 event log
        if self.mode == 'tail-experiment':
            peak_pnl = pos.get("peak_pnl_pct", 0)
            mfe_for_log = peak_pnl if peak_pnl else pnl_pct
            v6log.log_partial_close(
                position_id=pos_key, market_id=pos.get("market_id", ""),
                reason=reason, price=exit_price,
                shares_sold=shares_to_sell,
                cash_delta=proceeds,
                realized_pnl_delta=pnl_on_sale,
                remaining_shares=remaining,
                bankroll_before=bankroll_before_close,
                bankroll_after=round(self.state['bankroll'], 2),
                mfe_pct=mfe_for_log,
            )
        logger.info("PARTIAL %s %s: sold %.0f shares at $%.3f (%.1f%%), $%.2f PnL on sale, %.0f remain",
                    reason, pos.get("title", "?")[:30], shares_to_sell, exit_price,
                    ((exit_price - entry_price) / entry_price) * 100, pnl_on_sale, remaining)
        return exit_record

    def apply_risk_stops(self) -> list[dict]:
        """Apply v6 exit rules to open positions.
        Partial exits (x2_sell, x3_sell, x4_exit, mfe50_sell) handled by _partial_close.
        Full closes (trailing_stop, profit_protect, edge_exhausted, stop_loss, hard_take_profit)
        handled by _close_position.
        """
        closed = []
        for pos_key, pos in list(self._open_positions.items()):
            # Update peak PnL tracking
            current_pnl = pos.get("pnl_pct", 0)
            peak = pos.get("peak_pnl_pct")
            if peak is None or current_pnl > peak:
                pos["peak_pnl_pct"] = current_pnl

            reason = self._risk_exit_reason(pos)
            if reason:
                # Multiple-based partial exits
                if self.mode == 'tail-experiment' and reason in ('x2_sell', 'x3_sell', 'x4_exit', 'mfe50_sell'):
                    self._partial_close(pos_key, pos, pos.get("current_price"), reason)
                else:
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

        # --- Edge exhausted / re-entry cooldowns ---
        if self.mode == 'tail-experiment' or self.profile_mode == STRATEGY1_MODE:
            ee = self.state.get('recently_edge_exhausted', {})
            if market_id in ee:
                last_ee_ts = ee[market_id]
                try:
                    last_ee_dt = datetime.fromisoformat(last_ee_ts.replace("Z", "+00:00"))
                    minutes_since = (datetime.now(timezone.utc) - last_ee_dt).total_seconds() / 60.0
                    if minutes_since < EDGE_EXHAUSTED_COOLDOWN_MINUTES:
                        return None
                    else:
                        # Cooldown expired — clean up stale entry
                        del ee[market_id]
                except Exception:
                    pass

            # V6.1: General market cooldown after any non-resolution close
            cooldowns = self.state.get('market_close_cooldowns', {})
            if market_id in cooldowns:
                try:
                    last_close_dt = datetime.fromisoformat(cooldowns[market_id].replace("Z", "+00:00"))
                    minutes_since = (datetime.now(timezone.utc) - last_close_dt).total_seconds() / 60.0
                    if minutes_since < TAIL_EXP_MARKET_COOLDOWN_MINUTES:
                        return None
                    else:
                        del cooldowns[market_id]
                except Exception:
                    pass

            # V6.1: Daily entry count check — max 1-2 entries per market per day
            today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry_counts = self.state.get('market_entry_counts', {})
            market_today = entry_counts.get(market_id, {})
            if not isinstance(market_today, dict):
                market_today = {}
            if market_today.get("date") == today_key:
                if market_today.get("count", 0) >= TAIL_EXP_MAX_ENTRIES_PER_MARKET_PER_DAY:
                    return None

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
        if self.mode == 'tail-experiment':
            if volume < TAIL_EXP_MIN_VOLUME:
                return None
        elif volume < current_min_volume:
            return None

        # Max price check from learned parameters
        if price >= current_max_price:
            return None

        # v2: Skip mid-range entries ($0.005-$0.10) — 86 trades, 64% loss rate
        if self.profile_mode != STRATEGY1_MODE and 0.005 <= price <= 0.10 and self.mode != 'tail-experiment':
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
        if abs(ev) < current_min_ev and self.mode != 'tail-experiment':
            return None

        direction = "BUY" if ev > 0 else "SELL"
        base_confidence = min(0.9, 0.5 + abs(ev))

        # No SELL trades — they're flat-to-negative ($0.72 on 43 trades)
        if direction == 'SELL':
            return None

        if self.profile_mode == STRATEGY1_MODE:
            is_middle_bucket = (
                'between' in title_lower
                and 'or higher' not in title_lower
                and 'or below' not in title_lower
            )
            ev_ratio = fair_price / price if price > 0 else 0.0
            if not is_middle_bucket:
                _log_runtime_event(self.profile_mode, "SKIP", "not_middle_bucket", title=title, city=city)
                return None
            if city not in STRATEGY1_CITIES:
                _log_runtime_event(self.profile_mode, "SKIP", "city_not_in_strategy", title=title, city=city)
                return None
            if price > STRATEGY1_MAX_ENTRY:
                _log_runtime_event(self.profile_mode, "SKIP", "entry_price_too_high", title=title, price=price)
                return None
            if volume < STRATEGY1_MIN_VOLUME:
                _log_runtime_event(self.profile_mode, "SKIP", "volume_too_low", title=title, volume=volume)
                return None
            if ev_ratio < STRATEGY1_MIN_EV_RATIO:
                _log_runtime_event(
                    self.profile_mode, "SKIP", "ev_ratio_too_low",
                    title=title, ev_ratio=round(ev_ratio, 2), fair_price=fair_price, price=price,
                )
                return None

        # Tail-experiment filters
        if self.mode == 'tail-experiment':
            if direction != 'BUY':
                return None  # BUY-only
            title_lower = title.lower()
            if 'or higher' not in title_lower and 'or below' not in title_lower:
                return None  # tail-only
            # Hard skip: $0.006+ never enters
            if price >= TAIL_EXP_HARD_SKIP_PRICE:
                return None
            # Entry max: $0.005 only with very strong EV
            if price > TAIL_EXP_ENTRY_MAX:
                return None
            # V6 Signal: fair_value / entry_price >= 3x
            if fair_price is None or fair_price <= 0 or price <= 0:
                return None
            ev_ratio = fair_price / price
            if ev_ratio < TAIL_EXP_MIN_EV_RATIO:
                return None
            if self.profile_mode in {STRATEGY2_MODE, STRATEGY3_MODE}:
                forecast_gap = _forecast_gap_f(forecast_temp, bucket_low, bucket_high)
                if price > STRATEGY2_MAX_ENTRY:
                    _log_runtime_event(self.profile_mode, "SKIP", "entry_price_too_high", title=title, price=price)
                    return None
                if ev_ratio < STRATEGY2_MIN_EV_RATIO:
                    _log_runtime_event(
                        self.profile_mode, "SKIP", "ev_ratio_too_low",
                        title=title, ev_ratio=round(ev_ratio, 2), fair_price=fair_price, price=price,
                    )
                    return None
                if forecast_gap is None or forecast_gap < STRATEGY2_MIN_FORECAST_GAP_F:
                    _log_runtime_event(
                        self.profile_mode, "SKIP", "forecast_gap_too_small",
                        title=title, forecast_gap=forecast_gap, forecast_temp=forecast_temp,
                        bucket_low=bucket_low, bucket_high=bucket_high,
                    )
                    return None
                if volume < STRATEGY2_MIN_VOLUME:
                    _log_runtime_event(self.profile_mode, "SKIP", "volume_outside_strategy", title=title, volume=volume)
                    return None
            # V6.1: Suspicious volume check — $0.001 markets with volume <$25 may have fake MFE
            if self.profile_mode not in {STRATEGY2_MODE, STRATEGY3_MODE} and price <= 0.001 and volume < TAIL_EXP_SUSPICIOUS_VOLUME:
                return None
            # Optional trade limit; continuous mode leaves this unset.
            total_trades = self.state.get("total_trades", 0)
            if TAIL_EXP_TRADES_LIMIT is not None and total_trades >= TAIL_EXP_TRADES_LIMIT:
                logger.info("Tail experiment: hit %d trade limit, stopping", TAIL_EXP_TRADES_LIMIT)
                return None

        # Whale overlay
        whale_overlay = self._check_whale_overlay(title, direction, whale_positions or [])
        confidence = base_confidence + whale_overlay["adjustment"]
        confidence = max(0.05, min(0.99, confidence))
        if confidence < 0.3:
            return None

        # Whale gate — skipped in tail-experiment mode (pure EV trading)
        if self.mode != 'tail-experiment' and self.profile_mode != STRATEGY1_MODE:
            weather_whale_count = self._count_weather_whales(whale_positions or [])
            if whale_overlay["count"] == 0 and weather_whale_count >= 3:
                return None
            if not whale_overlay["aligned"]:
                return None

        # Sizing
        if self.profile_mode == STRATEGY1_MODE:
            allocation = min(STRATEGY1_FIXED_ALLOCATION, current_max_bet)
            kelly_adjusted = 0.0
            effective_kelly_fraction = 0.0
        elif self.profile_mode == STRATEGY2_MODE:
            allocation = STRATEGY2_FIXED_ALLOCATION
            kelly_adjusted = 0.0
            effective_kelly_fraction = 0.0
        elif self.profile_mode == STRATEGY3_MODE:
            stake_state = self.state.setdefault("stake_state", {})
            base_stake = float(stake_state.get("base_stake", STRATEGY3_BASE_STAKE))
            max_stake = float(stake_state.get("max_stake", STRATEGY3_MAX_STAKE))
            planned_stake = float(stake_state.get("current_stake", base_stake))
            allocation = round(min(max_stake, max(base_stake, planned_stake)), 2)
            liquidity_floor = max(STRATEGY2_MIN_VOLUME, allocation * STRATEGY3_LIQUIDITY_MULTIPLIER)
            if volume < liquidity_floor:
                _log_runtime_event(
                    self.profile_mode,
                    "SKIP",
                    "liquidity_too_low_for_stake",
                    title=title,
                    volume=volume,
                    planned_stake=allocation,
                    required_volume=round(liquidity_floor, 2),
                )
                return None
            kelly_adjusted = 0.0
            effective_kelly_fraction = 0.0
        elif self.mode == 'tail-experiment':
            # Fixed allocation for tail experiment
            allocation = TAIL_EXP_FIXED_ALLOCATION
            kelly_adjusted = 0.0
            effective_kelly_fraction = 0.0
        else:
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
            "mfe_price": round(price, 4),
            "mae_price": round(price, 4),
            "strategy_mode": self.profile_mode,
        }
        if self.profile_mode == STRATEGY3_MODE:
            entry["stake_plan"] = {
                "base_stake": STRATEGY3_BASE_STAKE,
                "planned_stake": round(allocation, 2),
                "max_stake": STRATEGY3_MAX_STAKE,
                "profit_reinvest_pct": STRATEGY3_PROFIT_REINVEST_PCT,
                "liquidity_multiplier": STRATEGY3_LIQUIDITY_MULTIPLIER,
                "required_volume": round(max(STRATEGY2_MIN_VOLUME, allocation * STRATEGY3_LIQUIDITY_MULTIPLIER), 2),
            }

        # Multiple-based exit levels for tail experiment (V6)
        if self.mode == 'tail-experiment':
            entry["original_shares"] = round(shares, 2)
            entry["exits"] = []
            entry["x2_level"] = round(price * 2, 4)
            entry["x3_level"] = round(price * 3, 4)
            entry["x4_level"] = round(price * 4, 4)
            entry["mfe50_sold"] = False
            entry["x2_sold"] = False
            entry["x3_sold"] = False
            entry["x4_exit"] = False
            entry["has_partial_profit"] = False
            min_hold_val = MIN_HOLD_MINUTES_001 if price <= 0.001 else MIN_HOLD_MINUTES
            entry["min_hold_until"] = (datetime.now(timezone.utc) + timedelta(minutes=min_hold_val)).isoformat()
            entry["ev_ratio"] = round(ev_ratio, 2)

        # Dedup only among currently open positions. New entries receive an
        # immutable key so closed history is never overwritten on re-entry.
        base_key = _position_base_key(condition_id, direction, slug)
        existing_key = None
        for open_key, open_pos in self._open_positions.items():
            if (
                open_pos.get("condition_id") == condition_id
                and open_pos.get("side") == direction
            ) or open_key == base_key or open_key == slug:
                existing_key = open_key
                break

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
            # MFE/MAE tracking for experiment mode
            if self.mode == 'tail-experiment':
                current_p = float(price)
                mfe = pos.get('mfe_price')
                mae = pos.get('mae_price')
                if mfe is None or current_p > mfe:
                    pos['mfe_price'] = current_p
                if mae is None or current_p < mae:
                    pos['mae_price'] = current_p
            self.state["positions"][existing_key] = pos
            _log_trade({"action": "UPDATE", **pos}, self.trades_log)
            _save_state(self.state, self.state_file)
            return pos

        # Deduct bankroll on position open
        bankroll_before = round(self.state['bankroll'], 2)
        self.state['bankroll'] = round(self.state['bankroll'] - allocation, 2)
        next_sequence = int(self.state.get("total_trades", 0)) + 1
        pos_key = _new_position_key(condition_id, direction, slug, entry["entry_ts"], next_sequence)
        entry["position_id"] = pos_key
        entry["market_side_key"] = base_key

        # Open new position
        self.state["positions"][pos_key] = entry
        self.state["total_trades"] += 1
        self._open_positions[pos_key] = entry
        _log_trade({"action": "OPEN", **entry}, self.trades_log)
        _save_state(self.state, self.state_file)

        # V6.1: Track daily entry count for this market
        if (self.mode == 'tail-experiment' or self.profile_mode == STRATEGY1_MODE) and market_id:
            today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            entry_counts = self.state.setdefault('market_entry_counts', {})
            current = entry_counts.get(market_id, {})
            if not isinstance(current, dict):
                current = {}
            if current.get("date") == today_key:
                current["count"] = current.get("count", 0) + 1
            else:
                current = {"date": today_key, "count": 1}
            entry_counts[market_id] = current

        # V6 event log
        if self.mode == 'tail-experiment':
            v6log.log_open(
                position_id=pos_key, market_id=market_id,
                entry_price=round(price, 4), shares=shares,
                cost=round(allocation, 2),
                bankroll_before=bankroll_before,
                bankroll_after=round(self.state['bankroll'], 2),
                model_fair_value=round(fair_price, 4),
                ev_ratio=round(ev_ratio, 2) if self.mode == 'tail-experiment' else None,
                forecast_temp=forecast_temp,
                threshold=bucket_high if bucket_high != 999.0 else bucket_low if bucket_low != -999.0 else None,
                city=city,
                bucket_type="below" if bucket_low == -999.0 else "above" if bucket_high == 999.0 else "range",
            )
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

    def _count_weather_whales(self, whale_positions: list[dict]) -> int:
        """Count unique quality whales holding weather positions."""
        weather_addresses = set()
        for wp in whale_positions or []:
            wt = (wp.get("title") or "").lower()
            if "temperature" in wt or "highest" in wt or "lowest" in wt:
                addr = wp.get("wallet_address", "") or wp.get("address", "")
                if addr:
                    weather_addresses.add(addr)
        return len(weather_addresses)

    def _check_whale_overlay(self, title: str, direction: str,
                              whale_positions: list[dict]) -> dict:
        """Check if quality whales are in this market and what direction.
        Only counts positions from quality-weighted wallets.
        Requires 2+ aligned quality whales to signal alignment."""
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
        # Dynamic gate: when ≤2 quality whales hold weather positions,
        # allow EV-only trades (no whale alignment needed).
        # When 3+ weather whales exist in the live scrape, gate on alignment.
        weather_whale_count = self._count_weather_whales(whale_positions)
        if weather_whale_count >= 3:
            aligned_flag = aligned >= 1
        else:
            aligned_flag = True  # fallback to EV-only when weather whales are scarce

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
            "seattle", "atlanta", "boston", "phoenix", "houston",
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
            "houston": "Houston",
            "san-francisco": "San Francisco", "london": "London",
            "paris": "Paris", "tokyo": "Tokyo", "berlin": "Berlin",
            "sydney": "Sydney", "mexico-city": "Mexico City",
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
                # Track peak PnL for trailing stop
                peak = pos.get("peak_pnl_pct")
                if peak is None or pnl_pct > peak:
                    pos["peak_pnl_pct"] = pnl_pct
                # Track MFE/MAE for experiment analysis
                if self.mode == 'tail-experiment':
                    entry_price = float(pos.get('entry_price', 1))
                    current = float(price)
                    mfe = pos.get('mfe_price')
                    mae = pos.get('mae_price')
                    if mfe is None or current > mfe:
                        pos['mfe_price'] = current
                    if mae is None or current < mae:
                        pos['mae_price'] = current
            except Exception:
                pass
        _save_state(self.state, self.state_file)

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
                _save_state(self.state, self.state_file)
                logger.info(f'Applied learned parameters: {learned}')
                return learned
        except Exception:
            pass
        return {}

    def summary(self) -> dict:
        """Get current portfolio summary."""
        self._reload()
        positions = list(self.state.get("positions", {}).values())
        open_positions = [p for p in positions if p.get("status") == "open"]
        closed_positions = [p for p in positions if p.get("status") == "closed"]
        total_value = sum(p.get("value", 0) for p in open_positions)

        def net_pnl(pos: dict) -> float:
            exits = pos.get("exits", []) or []
            return float(pos.get("pnl", 0) or 0) + sum(
                float(e.get("pnl", 0) or 0)
                for e in exits
                if isinstance(e, dict)
            )

        unrealized_pnl = sum(net_pnl(p) for p in open_positions)
        realized_pnl = sum(net_pnl(p) for p in closed_positions)
        total_pnl = realized_pnl + unrealized_pnl

        return {
            "bankroll": round(self.state.get("bankroll", 0), 2),
            "starting_bankroll": round(self.state.get("starting_bankroll", 0), 2),
            "exposure": round(self.state.get("exposure", 0) or total_value, 2),
            "open_positions": len(open_positions),
            "total_trades": self.state.get("total_trades", 0),
            "wins": self.state.get("wins", 0),
            "losses": self.state.get("losses", 0),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
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
