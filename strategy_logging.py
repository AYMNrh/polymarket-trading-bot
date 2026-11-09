"""Structured strategy decision logging and daily summaries."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DECISION_LOG_INTERVAL_SECONDS = 60


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _bucket(value: Any, edges: tuple[float, ...], prefix: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{prefix}:unknown"
    previous = None
    for edge in edges:
        if number < edge:
            if previous is None:
                return f"{prefix}:<{edge:g}"
            return f"{prefix}:{previous:g}-{edge:g}"
        previous = edge
    return f"{prefix}:>={edges[-1]:g}"


def price_bucket(price: Any) -> str:
    return _bucket(price, (0.75, 0.8, 0.85, 0.9, 0.95, 1.0), "price")


def volume_bucket(volume: Any) -> str:
    return _bucket(volume, (100, 250, 500, 1000, 5000, 10000), "volume")


def seconds_bucket(seconds_left: Any) -> str:
    return _bucket(seconds_left, (15, 30, 60, 120, 300, 900), "seconds")


def fill_quality(best_ask: Any, vwap: Any, levels_used: Any) -> dict[str, Any]:
    try:
        best = float(best_ask)
        weighted = float(vwap)
        diff = round(weighted - best, 6)
        bps = round((diff / best) * 10000, 2) if best > 0 else None
    except (TypeError, ValueError):
        diff = None
        bps = None
    try:
        levels = int(levels_used)
    except (TypeError, ValueError):
        levels = None
    return {"vwap_minus_best_ask": diff, "vwap_slippage_bps": bps, "levels_used": levels}


def asset_from_slug(slug: str | None) -> str:
    text = str(slug or "").lower()
    if text.startswith("eth-"):
        return "ETH"
    if text.startswith("btc-"):
        return "BTC"
    if text.startswith("sol-"):
        return "SOL"
    return "unknown"


def _summary_key(record: dict[str, Any]) -> str:
    return "|".join([
        str(record.get("strategy", "unknown")),
        str(record.get("action", "unknown")),
        str(record.get("reason", "")),
        str(record.get("asset", "unknown")),
        str(record.get("side", "none")),
        price_bucket(record.get("vwap") or record.get("best_ask")),
        volume_bucket(record.get("volume")),
        seconds_bucket(record.get("seconds_left")),
    ])


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")


def update_daily_summary(record: dict[str, Any]) -> None:
    path = DATA_DIR / f"strategy_daily_summary_{utc_day()}.json"
    try:
        summary = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        summary = {}
    key = _summary_key(record)
    item = summary.setdefault(key, {
        "strategy": record.get("strategy"),
        "action": record.get("action"),
        "reason": record.get("reason"),
        "asset": record.get("asset"),
        "side": record.get("side"),
        "price_bucket": price_bucket(record.get("vwap") or record.get("best_ask")),
        "volume_bucket": volume_bucket(record.get("volume")),
        "seconds_bucket": seconds_bucket(record.get("seconds_left")),
        "count": 0,
        "trades": 0,
        "pnl": 0.0,
    })
    item["count"] = int(item.get("count", 0)) + 1
    if record.get("action") in {"trade_enter", "trade_resolve"}:
        item["trades"] = int(item.get("trades", 0)) + 1
    if record.get("pnl") is not None:
        item["pnl"] = round(float(item.get("pnl", 0.0)) + float(record.get("pnl", 0.0)), 4)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True))


def should_log_decision(state: dict[str, Any], key: str, interval: int = DECISION_LOG_INTERVAL_SECONDS) -> bool:
    cache = state.setdefault("decision_log_cache", {})
    current = datetime.now(timezone.utc).timestamp()
    last = float(cache.get(key, 0) or 0)
    if current - last < interval:
        return False
    cache[key] = current
    return True


def log_strategy_record(record: dict[str, Any]) -> None:
    enriched = {"ts": now_iso(), **record}
    append_jsonl(DATA_DIR / f"strategy_decisions_{utc_day()}.jsonl", enriched)
    update_daily_summary(enriched)
