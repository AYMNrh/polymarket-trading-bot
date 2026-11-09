"""Pure complete-set pricing with explicit rejection reasons.

Only the YES and NO tokens of one binary condition are eligible. A shared
event id, similar title, or negative-risk flag does not prove a complete set.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
import json
import re


D = Decimal
MICRO = D("0.000001")


class Rejected(ValueError):
    pass


def number(value):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise Rejected("invalid_number") from None
    if not result.is_finite():
        raise Rejected("nonfinite_number")
    return result


def array(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise Rejected("invalid_array") from None
    if not isinstance(result, list):
        raise Rejected("invalid_array")
    return result


def tokens(market):
    labels = [str(x).lower() for x in array(market.get("outcomes"))]
    ids = [str(x) for x in array(market.get("clobTokenIds"))]
    if len(labels) != 2 or set(labels) != {"yes", "no"} or len(ids) != 2:
        raise Rejected("not_binary_yes_no")
    if len(set(ids)) != 2 or not all(x.isdigit() for x in ids):
        raise Rejected("invalid_token_ids")
    return dict(zip(labels, ids))


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.timestamp()
    except (ValueError, TypeError, OverflowError):
        raise Rejected("invalid_date") from None


@dataclass(frozen=True)
class Limits:
    bankroll: float = 100.0
    basket_cap: float = 10.0
    exposure_cap: float = 20.0
    max_open: int = 10
    duration_days: int = 3
    min_profit: float = 0.02
    friction_buffer: float = 0.01
    book_max_age: float = 60.0
    book_max_skew: float = 5.0


def fee_rate(market):
    if market.get("feesEnabled") is False:
        return D(0)
    schedule = market.get("feeSchedule")
    if market.get("feesEnabled") is not True or not isinstance(schedule, dict):
        raise Rejected("fee_schedule_unknown")
    # Current documented fee curve. Other versions must be implemented explicitly.
    if number(schedule.get("exponent")) != 1:
        raise Rejected("unsupported_fee_curve")
    rate = number(schedule.get("rate"))
    if not 0 <= rate <= 1:
        raise Rejected("invalid_fee_rate")
    return rate


def levels(book, side):
    rows = book.get(side)
    if not isinstance(rows, list):
        raise Rejected("missing_book_side")
    combined = {}
    for row in rows:
        if not isinstance(row, dict):
            raise Rejected("invalid_book_level")
        price, size = number(row.get("price")), number(row.get("size"))
        if not 0 < price < 1 or size < 0:
            raise Rejected("invalid_book_level")
        if size:
            combined[price] = combined.get(price, D(0)) + size
    return sorted(combined.items(), reverse=side == "bids")


def validate_book(book, token, condition, now, limits):
    if not isinstance(book, dict):
        raise Rejected("missing_book")
    if str(book.get("asset_id")) != token or book.get("market") != condition:
        raise Rejected("book_identity_mismatch")
    stamp = float(number(book.get("timestamp"))) / 1000
    if now - stamp > limits.book_max_age or stamp - now > 5:
        raise Rejected("stale_book")
    asks, bids = levels(book, "asks"), levels(book, "bids")
    if not asks:
        raise Rejected("no_asks")
    if bids and bids[0][0] >= asks[0][0]:
        raise Rejected("crossed_book")
    minimum = number(book.get("min_order_size"))
    if minimum <= 0:
        raise Rejected("minimum_order_unknown")
    return stamp, asks, minimum


def fill(rows, units, rate):
    """Return gross cost/proceeds and conservatively rounded per-level fees."""
    remaining, gross, fees = units, D(0), D(0)
    for price, available in rows:
        quantity = min(remaining, available)
        gross += quantity * price
        fees += (quantity * rate * price * (1 - price)).quantize(D("0.00001"), rounding=ROUND_UP)
        remaining -= quantity
        if remaining <= 0:
            return gross, fees
    raise Rejected("insufficient_depth")


def plan_basket(market, books, now, limits=Limits()):
    condition = str(market.get("conditionId", ""))
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition):
        raise Rejected("invalid_condition")
    if not (market.get("active") is True and market.get("closed") is False
            and market.get("acceptingOrders") is True and market.get("enableOrderBook") is True):
        raise Rejected("market_not_tradable")
    end = timestamp(market.get("endDate"))
    if end <= now or end > now + 7 * 86400:
        raise Rejected("outside_market_horizon")
    ids = tokens(market)
    minimum_cash = number(market.get("orderMinSize"))
    if minimum_cash <= 0:
        raise Rejected("minimum_order_unknown")
    # Gamma currently documents orderMinSize in cash. Also enforce each book's
    # minimum as a share floor, conservatively covering older CLOB responses.
    if minimum_cash * 2 > number(limits.basket_cap):
        raise Rejected("minimum_order_exceeds_budget")
    rate = fee_rate(market)
    validated = [validate_book(books.get(ids[s]), ids[s], condition, now, limits) for s in ("yes", "no")]
    if abs(validated[0][0] - validated[1][0]) > limits.book_max_skew:
        raise Rejected("book_timestamp_skew")
    rows = [v[1] for v in validated]
    # Stop before unprofitable deep levels can hide a smaller valid basket.
    indices, left = [0, 0], [rows[0][0][1], rows[1][0][1]]
    profitable_depth = D(0)
    while all(indices[i] < len(rows[i]) for i in (0, 1)):
        prices = [rows[i][indices[i]][0] for i in (0, 1)]
        if sum(p + rate * p * (1 - p) for p in prices) >= 1:
            break
        quantity = min(left)
        profitable_depth += quantity
        for i in (0, 1):
            left[i] -= quantity
            if left[i] == 0:
                indices[i] += 1
                if indices[i] < len(rows[i]):
                    left[i] = rows[i][indices[i]][1]
    if profitable_depth == 0:
        raise Rejected("no_edge_after_costs")
    low, high = D(0), profitable_depth
    # Even free liquidity cannot require more units than the cap / first ask sum.
    high = min(high, number(limits.basket_cap) / sum(r[0][0] for r in rows))
    def costs(units):
        legs = [fill(r, units, rate) for r in rows]
        cost = sum(g + f for g, f in legs) + number(limits.friction_buffer)
        return legs, cost.quantize(MICRO, rounding=ROUND_UP)
    for _ in range(70):
        middle = (low + high) / 2
        if costs(middle)[1] <= number(limits.basket_cap):
            low = middle
        else:
            high = middle
    units = low.quantize(MICRO, rounding=ROUND_DOWN)
    if units <= 0 or units < max(v[2] for v in validated):
        raise Rejected("minimum_shares_or_depth")
    legs, total = costs(units)
    if any(g < minimum_cash for g, _ in legs):
        raise Rejected("minimum_leg_notional")
    profit = units - total
    if profit < number(limits.min_profit):
        raise Rejected("no_edge_after_costs")
    return {
        "condition_id": condition, "market_id": str(market.get("id", "")),
        "title": str(market.get("question", "")), "slug": str(market.get("slug", "")),
        "market_date": market.get("endDate"), "tokens": ids,
        "units": str(units), "cost": str(total), "payout_if_complete": str(units),
        "expected_profit": str(profit), "fee_rate": str(rate),
        "friction_buffer": str(limits.friction_buffer), "quoted_at": now,
        "book_timestamps": [v[0] for v in validated],
        "legs": [{"outcome": s, "token_id": ids[s], "gross": str(g), "fee": str(f)}
                 for s, (g, f) in zip(("yes", "no"), legs)],
    }


def liquidation_value(plan, books, now, limits=Limits()):
    values = []
    for token in plan["tokens"].values():
        book = books.get(token)
        validate_book(book, token, plan["condition_id"], now, limits)
        gross, fees = fill(levels(book, "bids"), number(plan["units"]), number(plan["fee_rate"]))
        values.append(gross - fees)
    return max(D(0), sum(values)).quantize(MICRO, rounding=ROUND_DOWN)


def settlement_value(plan, market):
    if str(market.get("id")) != plan["market_id"] or market.get("conditionId") != plan["condition_id"]:
        raise Rejected("settlement_identity_mismatch")
    if tokens(market) != plan["tokens"]:
        raise Rejected("settlement_token_mismatch")
    # Closed alone can mean suspended trading, not a final payout.
    if market.get("closed") is not True or market.get("umaResolutionStatus") != "resolved":
        raise Rejected("awaiting_official_resolution")
    prices = [number(p) for p in array(market.get("outcomePrices"))]
    if len(prices) != 2 or any(p < 0 or p > 1 for p in prices) or sum(prices) != 1:
        raise Rejected("invalid_settlement_payout")
    return number(plan["units"])
