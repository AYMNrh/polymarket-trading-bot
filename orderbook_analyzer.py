"""
Order Book Analyzer — reads Polymarket order books for weather markets
and detects the 3 key signals a human trader watches:

1. Big wall on bid → someone's holding the level (support)
2. Thin ask → price is about to jump (liquidity gap)
3. Skew → capital imbalance (direction bias)

Also computes a combined order book score that feeds into EV computation.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

WALL_DEPTH_FACTOR = 2.5       # A wall is Nx thicker than the average level (Polymarket books are thin)
THIN_ASK_DEPTH_THRESHOLD = 1.0  # Thin = less than $1.00 in dollar depth at the ask
SKEW_RATIO_THRESHOLD = 0.3    # Skew >30% imbalance is significant
MIN_WALL_SIZE = 300            # Ignore walls smaller than $300 (Polymarket liquidity)


@dataclass
class OrderBookSignal:
    token_id: str
    bid_price: float = 0.0
    ask_price: float = 0.0
    spread: float = 0.0
    bid_depth: float = 0.0       # Total $ at bid levels
    ask_depth: float = 0.0       # Total $ at ask levels
    bid_wall: Optional[float] = None   # Price level of a big bid wall
    bid_wall_size: float = 0.0
    ask_wall: Optional[float] = None   # Price level of a big ask wall
    ask_wall_size: float = 0.0
    is_ask_thin: bool = False
    is_bid_thin: bool = False
    skew: float = 0.0            # -1.0 to 1.0 (negative = bid-heavy, positive = ask-heavy)
    wall_score: float = 0.0      # 0-1 combined order book confidence
    mid_price: float = 0.0


def _as_float(val) -> float:
    """Safely convert order book price/size to float."""
    if val is None:
        return 0.0
    return float(val)


def analyze_order_book(book: dict, token_id: str) -> OrderBookSignal:
    """
    Parse a Polymarket CLOB order book response and return signals.

    Polymarket book format:
    {
      "bids": [{"price": "0.45", "size": "1000"}, ...],
      "asks": [{"price": "0.55", "size": "800"}, ...]
    }
    """
    sig = OrderBookSignal(token_id=token_id)
    bids_raw = book.get("bids", [])
    asks_raw = book.get("asks", [])

    if not bids_raw or not asks_raw:
        return sig

    # Parse top levels
    best_bid = _as_float(bids_raw[0].get("price", 0))
    best_ask = _as_float(asks_raw[0].get("price", 0))
    sig.bid_price = best_bid
    sig.ask_price = best_ask
    sig.spread = best_ask - best_bid
    sig.mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else 0

    # Compute total depth (top 10 levels)
    bid_levels = []
    ask_levels = []
    for b in bids_raw[:10]:
        p = _as_float(b.get("price", 0))
        s = _as_float(b.get("size", 0))
        dollar_depth = p * s
        bid_levels.append((p, s, dollar_depth))
        sig.bid_depth += dollar_depth

    for a in asks_raw[:10]:
        p = _as_float(a.get("price", 0))
        s = _as_float(a.get("size", 0))
        dollar_depth = p * s
        ask_levels.append((p, s, dollar_depth))
        sig.ask_depth += dollar_depth

    # 1. WALL DETECTION: Look for levels with Nx the average depth
    if bid_levels:
        avg_bid_level = sig.bid_depth / len(bid_levels) if bid_levels else 1
        for price, size, depth in bid_levels:
            if depth >= avg_bid_level * WALL_DEPTH_FACTOR and depth >= MIN_WALL_SIZE:
                sig.bid_wall = price
                sig.bid_wall_size = round(depth, 0)
                logger.info("🧱 Bid wall at %.4f ($%.0f)", price, depth)
                break  # Only report the most aggressive wall

    if ask_levels:
        avg_ask_level = sig.ask_depth / len(ask_levels) if ask_levels else 1
        for price, size, depth in ask_levels:
            if depth >= avg_ask_level * WALL_DEPTH_FACTOR and depth >= MIN_WALL_SIZE:
                sig.ask_wall = price
                sig.ask_wall_size = round(depth, 0)
                logger.info("🧱 Ask wall at %.4f ($%.0f)", price, depth)
                break

    # 2. THIN SIDE DETECTION: Top ask level has very little liquidity
    if ask_levels and ask_levels[0][2] < THIN_ASK_DEPTH_THRESHOLD:
        sig.is_ask_thin = True
        logger.info("📈 Thin ask — $%.2f at %.4f", ask_levels[0][2], ask_levels[0][0])

    if bid_levels and bid_levels[0][2] < THIN_ASK_DEPTH_THRESHOLD:
        sig.is_bid_thin = True
        logger.info("📉 Thin bid — $%.2f at %.4f", bid_levels[0][2], bid_levels[0][0])

    # 3. SKEW CALCULATION: -1.0 to 1.0
    total_depth = sig.bid_depth + sig.ask_depth
    if total_depth > 0:
        sig.skew = round((sig.ask_depth - sig.bid_depth) / total_depth, 3)

    # 4. COMBINED WALL SCORE (0-1)
    # Measures how favorable the book is for a BUYER
    # Positive = good for buyers (big bid wall protecting downside, thin ask above)
    # Negative = bad for buyers (big ask wall overhead, thin bid below)
    score = 0.0

    # Bid wall protects downside → bullish for buyers
    if sig.bid_wall is not None:
        score += 0.3
    if sig.is_ask_thin:
        score += 0.3  # Price likely to jump up through thin ask

    # Skew favors buyers when more capital on the bid side
    if sig.skew < -SKEW_RATIO_THRESHOLD:
        score += 0.2  # More bid than ask — capital waiting to buy
    elif sig.skew > SKEW_RATIO_THRESHOLD:
        score -= 0.2  # More ask than bid — capital waiting to sell

    # Ask wall overhead → resistance, bad for buyers
    if sig.ask_wall is not None:
        score -= 0.25

    sig.wall_score = max(-0.5, min(1.0, score))
    return sig


def order_book_to_signal(book_sig: OrderBookSignal) -> dict:
    """Convert an OrderBookSignal to a dict for storage/display."""
    return {
        "token_id": book_sig.token_id,
        "mid_price": book_sig.mid_price,
        "spread": book_sig.spread,
        "bid_price": book_sig.bid_price,
        "ask_price": book_sig.ask_price,
        "bid_depth": round(book_sig.bid_depth, 2),
        "ask_depth": round(book_sig.ask_depth, 2),
        "bid_wall": book_sig.bid_wall,
        "bid_wall_size": book_sig.bid_wall_size,
        "ask_wall": book_sig.ask_wall,
        "ask_wall_size": book_sig.ask_wall_size,
        "is_ask_thin": book_sig.is_ask_thin,
        "is_bid_thin": book_sig.is_bid_thin,
        "skew": book_sig.skew,
        "wall_score": book_sig.wall_score,
    }
