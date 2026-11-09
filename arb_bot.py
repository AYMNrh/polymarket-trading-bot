"""Falcon-sourced multi-outcome arbitrage analysis.

The scanner deliberately separates discovery from execution:
- Falcon/Polymarket Analytics supplies market rows and grouping hints.
- CLOB books supply executable bid/ask prices and depth.

Do not treat heuristic groups as guaranteed negative-risk arbitrage. The
dashboard shows confidence and rejection reasons so we can verify candidates
before using real money.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from clob_pricing import fetch_book, quote_from_book
from polymarket_analytics import AnalyticsError, PolymarketAnalyticsClient, first_float, first_str

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "arb_bot_state.json"
EVENT_LOG = DATA_DIR / "arb_events.jsonl"

STARTING_BANKROLL = 100.0
MAX_ROWS = 500
PAGE_SIZE = 100
MAX_GROUPS_TO_PRICE = 40
MAX_CLOB_LEGS = 120
MAX_STAKE_PER_BASKET = 15.0
MIN_EXECUTABLE_UNITS = 1.0
MIN_PROFIT_PCT = 0.25
MIN_GROUP_VOLUME_TO_PRICE = 1.0
SCHEMA_VERSION = 3

NEG_RISK_KEYS = (
    "negRisk",
    "negativeRisk",
    "negative_risk",
    "is_neg_risk",
    "isNegativeRisk",
    "enableNegRisk",
)
NEG_RISK_ID_KEYS = (
    "neg_risk_market_id",
    "negRiskMarketID",
    "negRiskMarketId",
    "negative_risk_market_id",
    "negativeRiskMarketId",
    "negative_risk_id",
    "neg_risk_id",
)
GROUP_ID_KEYS = (
    *NEG_RISK_ID_KEYS,
    "event_slug",
    "eventSlug",
    "event_id",
    "eventId",
    "series_slug",
    "seriesSlug",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round(value: Any, digits: int = 4) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "falcon_arbitrage_analysis",
        "bankroll": STARTING_BANKROLL,
        "equity": STARTING_BANKROLL,
        "total_pnl": 0.0,
        "positions": [],
        "history": [],
        "opportunities": [],
        "candidate_groups": [],
        "rejected_groups": [],
        "scan_stats": {},
        "last_scan": None,
        "last_error": None,
        "source_notes": [
            "Falcon/Polymarket Analytics is used for market discovery and grouping candidates.",
            "CLOB order books are used for executable YES ask, bid, spread, and size.",
            "Heuristic groups are analysis candidates, not guaranteed negative-risk arbitrage.",
        ],
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    try:
        loaded = json.loads(STATE_FILE.read_text())
    except Exception:
        return default_state()
    state = default_state()
    state.update(loaded if isinstance(loaded, dict) else {})
    state["schema_version"] = SCHEMA_VERSION
    return state


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def log_event(event: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    event = {"ts": now_iso(), **event}
    with EVENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def _row_slug(row: dict[str, Any]) -> str:
    return first_str(row, ("slug", "market_slug", "marketSlug"), "")


def _row_title(row: dict[str, Any]) -> str:
    return first_str(row, ("question", "title", "market_title", "name", "slug"), "")


def _row_outcome(row: dict[str, Any]) -> str:
    return first_str(row, ("side_a_outcome", "outcome", "outcome_name", "answer", "name"), "YES")


def _row_token_id(row: dict[str, Any]) -> str:
    return first_str(row, ("side_a_token_id", "token_id", "clob_token_id", "yes_token_id"), "")


def _row_volume(row: dict[str, Any]) -> float:
    return first_float(row, ("volume_total", "volume", "liquidity", "volume24hr"), 0.0) or 0.0


def _neg_risk_id(row: dict[str, Any]) -> str:
    return first_str(row, NEG_RISK_ID_KEYS, "")


def _has_neg_risk_flag(row: dict[str, Any]) -> bool:
    if _neg_risk_id(row):
        return True
    return any(_truthy(row.get(key)) for key in NEG_RISK_KEYS)


def _known_multi_outcome_family(slug: str) -> tuple[str, str] | None:
    """Return (family_type, key) for slug patterns that are mutually exclusive candidates."""
    if not slug:
        return None

    # Example: lol-drxc-hle-2026-01-13-hle-to-win-0-games
    if "-to-win-" in slug:
        return ("slug_to_win_family", slug.split("-to-win-", 1)[0])

    # Common sports exact-score style families often end in a numeric bucket.
    exact_score = re.match(r"(.+)-(?:score|correct-score)-[a-z0-9]+$", slug)
    if exact_score:
        return ("slug_exact_score_family", exact_score.group(1))

    return None


def _group_key(row: dict[str, Any]) -> tuple[str, str] | None:
    neg_id = _neg_risk_id(row)
    if neg_id:
        return ("verified_neg_risk_id", neg_id)

    for key in GROUP_ID_KEYS:
        value = row.get(key)
        if value:
            text = str(value)
            if key in NEG_RISK_ID_KEYS:
                return ("verified_neg_risk_id", text)

    family = _known_multi_outcome_family(_row_slug(row))
    if family:
        return family
    return None


def _confidence(group_type: str, rows: list[dict[str, Any]]) -> tuple[str, bool]:
    if group_type == "verified_neg_risk_id" or any(_has_neg_risk_flag(row) for row in rows):
        return ("verified_neg_risk", True)
    if group_type == "slug_to_win_family":
        return ("heuristic_to_win_family", False)
    if group_type == "slug_exact_score_family":
        return ("heuristic_exact_score_family", False)
    return ("low_confidence", False)


def _common_title(rows: list[dict[str, Any]], group_key: str) -> str:
    titles = [_row_title(row) for row in rows if _row_title(row)]
    if not titles:
        return group_key
    shortest = min(titles, key=len)
    return shortest[:140]


def _market_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": _row_slug(row),
        "title": _row_title(row),
        "outcome": _row_outcome(row),
        "token_id": _row_token_id(row),
        "volume": round(_row_volume(row), 2),
        "condition_id": first_str(row, ("condition_id", "conditionId"), ""),
        "winning_outcome": first_str(row, ("winning_outcome", "winningOutcome"), ""),
    }


def _price_group(
    group_type: str,
    group_key: str,
    rows: list[dict[str, Any]],
    session: requests.Session,
) -> dict[str, Any]:
    confidence, verified = _confidence(group_type, rows)
    details: list[dict[str, Any]] = []
    reasons: list[str] = []
    clob_legs = 0

    unique_by_token: dict[str, dict[str, Any]] = {}
    for row in rows:
        token_id = _row_token_id(row)
        if not token_id:
            reasons.append("missing_token")
            continue
        unique_by_token.setdefault(token_id, row)

    for row in unique_by_token.values():
        token_id = _row_token_id(row)
        quote = quote_from_book(token_id, fetch_book(token_id, session=session), outcome="Yes")
        clob_legs += 1
        detail = {
            **_market_row(row),
            "best_bid": _round(quote.best_bid, 4),
            "best_ask": _round(quote.best_ask, 4),
            "bid_size": _round(quote.bid_size, 2),
            "ask_size": _round(quote.ask_size, 2),
            "spread": _round(quote.spread, 4),
            "bid_depth": _round(quote.bid_depth, 2),
            "ask_depth": _round(quote.ask_depth, 2),
            "quote_error": quote.error,
        }
        if quote.best_ask is None:
            reasons.append("missing_ask")
        if quote.ask_size <= 0:
            reasons.append("no_best_ask_size")
        details.append(detail)

    priced = [d for d in details if d.get("best_ask") is not None]
    yes_sum = round(sum(float(d["best_ask"]) for d in priced), 6) if priced else None
    min_best_size = min((float(d.get("ask_size") or 0) for d in priced), default=0.0)
    total_volume = round(sum(_row_volume(row) for row in rows), 2)

    executable_units = 0.0
    cost = 0.0
    expected_profit = 0.0
    profit_pct = 0.0
    if yes_sum and yes_sum > 0:
        stake_units = MAX_STAKE_PER_BASKET / yes_sum
        executable_units = min(stake_units, min_best_size)
        cost = executable_units * yes_sum
        expected_profit = executable_units - cost
        profit_pct = (expected_profit / cost * 100.0) if cost > 0 else 0.0

    if len(priced) < 2:
        reasons.append("too_few_priced_outcomes")
    if len(details) != len(priced):
        reasons.append("not_all_legs_priced")
    if yes_sum is None:
        reasons.append("no_yes_sum")
    elif yes_sum >= 1.0:
        reasons.append("yes_sum_not_below_one")
    elif profit_pct < MIN_PROFIT_PCT:
        reasons.append("profit_below_threshold")
    if executable_units < MIN_EXECUTABLE_UNITS:
        reasons.append("insufficient_best_ask_depth")
    if not verified:
        reasons.append("not_verified_negative_risk")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    clean_reasons = []
    for reason in reasons:
        if reason and reason not in seen:
            clean_reasons.append(reason)
            seen.add(reason)

    is_executable = (
        verified
        and len(priced) >= 2
        and len(details) == len(priced)
        and yes_sum is not None
        and yes_sum < 1.0
        and profit_pct >= MIN_PROFIT_PCT
        and executable_units >= MIN_EXECUTABLE_UNITS
    )

    return {
        "group_key": group_key,
        "group_type": group_type,
        "title": _common_title(rows, group_key),
        "confidence": confidence,
        "verified": verified,
        "status": "executable" if is_executable else "rejected",
        "reason": ", ".join(clean_reasons) if clean_reasons else "ok",
        "outcomes": len(details),
        "priced_outcomes": len(priced),
        "yes_sum": _round(yes_sum, 6),
        "arb_pct": round(profit_pct, 2),
        "max_stake": MAX_STAKE_PER_BASKET,
        "executable_units": round(executable_units, 4),
        "cost": round(cost, 4),
        "payout": round(executable_units, 4),
        "expected_profit": round(expected_profit, 4),
        "total_volume": total_volume,
        "outcome_details": sorted(details, key=lambda item: (item.get("best_ask") is None, item.get("best_ask") or 9)),
        "clob_legs": clob_legs,
    }


def _pre_reject_group(group_type: str, group_key: str, rows: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    confidence, verified = _confidence(group_type, rows)
    details = [_market_row(row) for row in rows if _row_token_id(row)]
    return {
        "group_key": group_key,
        "group_type": group_type,
        "title": _common_title(rows, group_key),
        "confidence": confidence,
        "verified": verified,
        "status": "rejected",
        "reason": reason,
        "outcomes": len(details),
        "priced_outcomes": 0,
        "yes_sum": None,
        "arb_pct": 0.0,
        "max_stake": MAX_STAKE_PER_BASKET,
        "executable_units": 0.0,
        "cost": 0.0,
        "payout": 0.0,
        "expected_profit": 0.0,
        "total_volume": round(sum(_row_volume(row) for row in rows), 2),
        "outcome_details": details[:12],
        "clob_legs": 0,
    }


def _retrieve_market_rows(client: PolymarketAnalyticsClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset in range(0, MAX_ROWS, PAGE_SIZE):
        batch = client.retrieve_markets({"closed": "False", "min_volume": "0"}, limit=PAGE_SIZE, offset=offset)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return rows[:MAX_ROWS]


def scan_arbitrage() -> dict[str, Any]:
    client = PolymarketAnalyticsClient()
    scanned_at = now_iso()
    rows = _retrieve_market_rows(client)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    ungrouped = 0
    neg_flags = 0
    for row in rows:
        if _has_neg_risk_flag(row):
            neg_flags += 1
        key = _group_key(row)
        if key:
            groups[key].append(row)
        else:
            ungrouped += 1

    candidate_items = [
        (key, value)
        for key, value in groups.items()
        if len({_row_token_id(row) for row in value if _row_token_id(row)}) >= 2
    ]
    candidate_items.sort(key=lambda item: (len(item[1]), sum(_row_volume(row) for row in item[1])), reverse=True)

    session = requests.Session()
    priced_groups: list[dict[str, Any]] = []
    pre_rejected: list[dict[str, Any]] = []
    clob_legs = 0
    for (group_type, group_key), group_rows in candidate_items[:MAX_GROUPS_TO_PRICE]:
        total_volume = sum(_row_volume(row) for row in group_rows)
        if total_volume < MIN_GROUP_VOLUME_TO_PRICE:
            pre_rejected.append(_pre_reject_group(group_type, group_key, group_rows, "zero_or_tiny_falcon_volume"))
            continue
        if clob_legs >= MAX_CLOB_LEGS:
            pre_rejected.append(_pre_reject_group(group_type, group_key, group_rows, "clob_pricing_limit_reached"))
            break
        result = _price_group(group_type, group_key, group_rows, session)
        clob_legs += int(result.get("clob_legs") or 0)
        priced_groups.append(result)

    opportunities = [
        group for group in priced_groups
        if group.get("status") == "executable"
    ]
    opportunities.sort(key=lambda item: (item.get("arb_pct") or 0, item.get("expected_profit") or 0), reverse=True)

    all_analyzed_groups = priced_groups + pre_rejected
    rejected = [group for group in all_analyzed_groups if group.get("status") != "executable"]
    rejected.sort(key=lambda item: (item.get("yes_sum") is None, item.get("yes_sum") or 9))

    return {
        "last_scan": scanned_at,
        "last_error": None,
        "opportunities": opportunities[:40],
        "candidate_groups": all_analyzed_groups[:80],
        "rejected_groups": rejected[:80],
        "scan_stats": {
            "source": client.base_url,
            "rows_scanned": len(rows),
            "groups_found": len(groups),
            "candidate_groups": len(candidate_items),
            "groups_priced": len(priced_groups),
            "groups_pre_rejected": len(pre_rejected),
            "opportunities": len(opportunities),
            "rejected_groups": len(rejected),
            "ungrouped_rows": ungrouped,
            "neg_risk_flags_found": neg_flags,
            "clob_legs_priced": clob_legs,
            "max_rows": MAX_ROWS,
            "max_groups_priced": MAX_GROUPS_TO_PRICE,
            "max_stake_per_basket": MAX_STAKE_PER_BASKET,
        },
    }


def run_once() -> dict[str, Any]:
    state = load_state()
    try:
        scan = scan_arbitrage()
        state.update(scan)
        state["last_error"] = None
        log_event({
            "action": "scan",
            "stats": state.get("scan_stats", {}),
            "top_opportunity": (state.get("opportunities") or [{}])[0],
        })
    except AnalyticsError as exc:
        state["last_scan"] = now_iso()
        state["last_error"] = str(exc)
        log_event({"action": "scan_error", "error": str(exc)})
    except Exception as exc:
        state["last_scan"] = now_iso()
        state["last_error"] = f"arb scan failed: {exc}"
        log_event({"action": "scan_error", "error": state["last_error"]})

    state["equity"] = round(float(state.get("bankroll", STARTING_BANKROLL) or STARTING_BANKROLL), 2)
    state["total_pnl"] = round(state["equity"] - STARTING_BANKROLL, 2)
    save_state(state)
    return state


if __name__ == "__main__":
    result = run_once()
    print(json.dumps(result.get("scan_stats", {}), indent=2, sort_keys=True))
