#!/usr/bin/env python3
"""Discover quality whale wallets from SQLite profile data.

Analyzes whale profiles in whale_data.db for wallets with:
  - Positive total PnL (real edge)
  - Reasonable avg entry price (<$0.85, not washing)
  - Active trading (100+ trades)
  - Current weather positions (bonus)

Outputs ranked candidates to data/whale_candidates.json.

Usage:
    python3 discover_whales.py                          # full analysis
    python3 discover_whales.py --min-pnl 10000           # only wallets with $10k+ PnL
    python3 discover_whales.py --weather-only            # only wallets with weather positions
    python3 discover_whales.py --add-quality             # add quality candidates to whale_watch.json
"""

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT = Path(__file__).parent
DB_PATH = PROJECT / "whale_data.db"
OUT_PATH = PROJECT / "data" / "whale_candidates.json"

# Cache for display names to avoid re-scraping
_DISPLAY_NAME_CACHE: dict[str, str | None] = {}


def fetch_display_name(address: str) -> str | None:
    """Scrape Polymarket display name for a wallet address."""
    if address in _DISPLAY_NAME_CACHE:
        return _DISPLAY_NAME_CACHE[address]
    
    import re
    try:
        import requests
        r = requests.get(
            f"https://polymarket.com/profile/{address}",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        match = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if match:
            import json as _json
            data = _json.loads(match.group(1))
            username = data.get("props", {}).get("pageProps", {}).get("username")
            # Reject garbage: hex-like names, names with addresses, long garbage
            if username and username != "undefined":
                if len(username) > 35 or username.startswith("0x") or username.count("-") > 3:
                    _DISPLAY_NAME_CACHE[address] = None
                    return None
                _DISPLAY_NAME_CACHE[address] = username
                return username
    except Exception:
        pass
    
    _DISPLAY_NAME_CACHE[address] = None
    return None


def load_whale_profiles() -> list[dict]:
    """Load all wallet profiles from SQLite."""
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        return []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT address, label, total_volume, trades_tracked, win_rate, profile_json
        FROM whales
        WHERE profile_json IS NOT NULL
        ORDER BY total_volume DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def analyze_wallet(row: dict) -> dict:
    """Score a wallet based on its profile data."""
    try:
        profile = json.loads(row["profile_json"])
    except (json.JSONDecodeError, TypeError):
        return None

    summary = profile.get("summary", {})
    positions = profile.get("positions", [])

    total_pnl = float(summary.get("total_pnl", 0) or 0)
    trades = int(summary.get("trades", 0) or 0)
    volume = float(summary.get("total_volume", 0) or 0)
    avg_pnl_pct = float(summary.get("avg_pnl_pct", 0) or 0)
    days_active = int(summary.get("days_active", 0) or 0)

    # Current positions data
    entry_prices = [
        float(p.get("entry_price", 0)) for p in positions
        if p.get("entry_price") and 0 < float(p["entry_price"]) < 1
    ]
    avg_entry = sum(entry_prices) / len(entry_prices) if entry_prices else 0.5

    # Weather-specific positions
    weather_pos = []
    for p in positions:
        title = str(p.get("title", "")).lower()
        if "temperature" in title or "°f" in title or "°c" in title or "fahrenheit" in title:
            ep = float(p.get("entry_price", 0))
            weather_pos.append({
                "title": p.get("title", "?")[:50],
                "entry_price": ep,
                "side": p.get("side", "?"),
                "value": float(p.get("value", 0) or 0),
            })

    # --- Scoring ---

    # Classify entry style
    if avg_entry >= 0.85:
        entry_style = "WASHER"  # buys near-certainty, no signal value
    elif avg_entry >= 0.60:
        entry_style = "HIGH_PRICE"
    elif avg_entry >= 0.40:
        entry_style = "MID_PRICE"
    else:
        entry_style = "EDGE_BUYER"  # real edge territory

    # Score components (0.0 - 1.0)
    trade_score = min(1.0, trades / 5000.0)  # 5k trades = full score

    if total_pnl > 0:
        pnl_score = min(1.0, total_pnl / 1000000.0)  # $1M PnL = full score
    elif total_pnl > -10000:
        pnl_score = 0.1  # minor loss
    else:
        pnl_score = 0.0  # heavy loser

    if avg_entry < 0.30:
        entry_score = 1.0
    elif avg_entry < 0.50:
        entry_score = 0.8
    elif avg_entry < 0.70:
        entry_score = 0.6
    elif avg_entry < 0.85:
        entry_score = 0.3
    else:
        entry_score = 0.0  # washer

    weather_bonus = min(0.2, len(weather_pos) * 0.07)

    # Final score
    score = (
        trade_score * 0.15 +
        pnl_score * 0.40 +
        entry_score * 0.30 +
        0.10 +  # base: active wallet
        weather_bonus
    )

    # Severe washer penalty
    if avg_entry >= 0.85 and total_pnl < 100000:
        score *= 0.1
    elif avg_entry >= 0.95:
        score *= 0.05

    score = max(0.0, min(1.0, score))

    # Tier
    if score >= 0.6 and avg_entry < 0.85:
        tier = "QUALITY"
    elif score >= 0.3 and avg_entry < 0.85:
        tier = "POTENTIAL"
    elif avg_entry >= 0.85:
        tier = "WASHER"
    else:
        tier = "NOISE"

    return {
        "address": row["address"],
        "label": fetch_display_name(row["address"]) or row["label"] or row["address"][:10],
        "total_pnl": round(total_pnl, 2),
        "trades": trades,
        "volume": round(volume, 2),
        "avg_entry": round(avg_entry, 4),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "days_active": days_active,
        "entry_style": entry_style,
        "weather_positions": len(weather_pos),
        "weather_details": weather_pos[:5],
        "score": round(score, 4),
        "tier": tier,
    }


def main():
    min_pnl = None
    weather_only = False
    add_quality = False

    for arg in sys.argv[1:]:
        if arg == "--weather-only":
            weather_only = True
        elif arg == "--add-quality":
            add_quality = True
        elif arg.startswith("--min-pnl="):
            min_pnl = float(arg.split("=")[1])

    print("=" * 60)
    print("  WHALE DISCOVERY ENGINE")
    print("=" * 60)
    print()

    print("[1/2] Loading whale profiles...")
    profiles = load_whale_profiles()
    print(f"  Loaded {len(profiles)} wallet profiles")
    print()

    print("[2/2] Analyzing and scoring...")
    results = []
    for row in profiles:
        r = analyze_wallet(row)
        if r:
            results.append(r)

    # Apply filters
    if min_pnl:
        results = [r for r in results if r["total_pnl"] >= min_pnl]
    if weather_only:
        results = [r for r in results if r["weather_positions"] > 0]

    results.sort(key=lambda x: -x["score"])

    # Group
    quality = [r for r in results if r["tier"] == "QUALITY"]
    potential = [r for r in results if r["tier"] == "POTENTIAL"]
    washers = [r for r in results if r["tier"] == "WASHER"]
    noise = [r for r in results if r["tier"] == "NOISE"]

    print()
    print(f"  QUALITY:     {len(quality)}  — good signal, worth adding")
    print(f"  POTENTIAL:   {len(potential)}  — worth watching")
    print(f"  WASHERS:     {len(washers)}  — avg entry >$0.85, skip")
    print(f"  NOISE:       {len(noise)}  — insufficient data")
    print()

    if quality:
        print("  ─── QUALITY CANDIDATES (ranked) ───")
        hdr = f"  {'Label':25s} {'PnL':>11s} {'Trades':>7s} {'AvgEntry':>9s} {'EntryStyle':>12s} {'Weather':>8s} {'Score':>6s}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for c in quality:
            print(f"  {c['label']:25s} ${c['total_pnl']:>8,.0f} {c['trades']:>7,d} "
                  f"${c['avg_entry']:.3f}  {c['entry_style']:12s} {c['weather_positions']:8d} {c['score']:.3f}")

    if potential:
        print()
        print("  ─── POTENTIAL ───")
        for c in potential[:15]:
            print(f"  {c['label']:25s} PnL=${c['total_pnl']:>8,.0f}  trades={c['trades']:>6,d}  "
                  f"entry=${c['avg_entry']:.3f}  score={c['score']:.3f}")

    # Save output
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "quality": quality[:30],
        "potential": potential[:30],
        "washers": [{"address": w["address"], "label": w["label"],
                      "avg_entry": w["avg_entry"], "total_pnl": w["total_pnl"]}
                     for w in washers[:20]],
        "summary": {
            "quality": len(quality),
            "potential": len(potential),
            "washers": len(washers),
            "noise": len(noise),
        }
    }
    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n  Full output saved to {OUT_PATH}")
    print()

    # --add-quality: add quality candidates to whale_watch.json
    if add_quality and quality:
        from config import load_config, save_config
        cfg = load_config()
        existing = {w["address"].lower() for w in cfg["watched_wallets"]}
        added = 0
        for c in quality[:10]:  # top 10
            if c["address"].lower() not in existing:
                cfg["watched_wallets"].append({
                    "address": c["address"],
                    "label": c["label"],
                    "win_rate": None,
                    "trades_tracked": 0,
                    "weight": True,
                })
                added += 1
                print(f"  Added {c['label']} to whale_watch.json (weight:true)")
        if added:
            save_config(cfg)
            print(f"\n  Added {added} new quality whales to watch list!")
        else:
            print("  No new quality wallets to add (all already in watch list)")

    print()
    print("  ─── HOW TO USE ───")
    print("  To add a quality candidate: python3 discover_whales.py --add-quality")
    print("  Or manually edit whale_watch.json and set weight: true")


if __name__ == "__main__":
    main()
