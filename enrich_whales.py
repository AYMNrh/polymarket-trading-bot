#!/usr/bin/env python3
"""Enrich whale database with Polymarket profile data (portfolio, PnL, volume, etc.).

Run periodically (every 6h) to keep whale profile data fresh in the DB.
Dashboard reads from this instead of scraping live on every page load.
"""
import json
import logging
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

logging.basicConfig(level=logging.WARNING, stream=sys.stdout,
                    format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from config import load_config
from polymarket_scraper import PolymarketScraper
from database import get_conn


def get_or_add_column(conn, table, column, col_def):
    """Add column if it doesn't exist (SQLite ALTER TABLE limitation)."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        conn.commit()
        return True
    return False


def enrich_all():
    cfg = load_config()
    wallets = cfg.get("watched_wallets", [])
    if not wallets:
        print("No watched wallets configured")
        return

    scraper = PolymarketScraper()
    conn = get_conn()

    # Ensure profile_json column exists
    get_or_add_column(conn, "whales", "profile_json", "profile_json TEXT")
    get_or_add_column(conn, "whales", "profile_updated_at", "profile_updated_at TEXT")

    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    enriched = 0
    errors = 0

    for w in wallets:
        addr = w.get("address", "")
        label = w.get("label", addr[:10])
        if not addr:
            continue

        try:
            # Fetch profile & positions via Polymarket scraper
            profile = scraper.get_profile(addr)
            positions = scraper.get_positions(addr)
            summary = scraper.portfolio_summary(addr)

            if not profile and not positions:
                logger.info("No data for %s (%s), skipping", label, addr[:10])
                continue

            profile_payload = {
                "positions": positions,
                "profile": profile,
                "summary": summary,
                "cached_at": time.time(),
            }

            conn.execute(
                """UPDATE whales SET
                    profile_json = ?,
                    profile_updated_at = ?,
                    total_volume = MAX(total_volume, ?),
                    trades_tracked = MAX(trades_tracked, ?)
                   WHERE address = ?""",
                (
                    json.dumps(profile_payload, default=str),
                    now,
                    summary.get("total_volume", 0) or 0,
                    summary.get("trades", 0) or 0,
                    addr.lower(),
                )
            )
            conn.commit()
            enriched += 1

            t = summary.get("trades", 0)
            v = summary.get("total_volume", 0) or 0
            pnl = summary.get("total_pnl", 0) or 0
            pv = summary.get("portfolio_value", 0) or 0
            print(f"  {label:20s} | {t:>5} trades | ${v:>9,.0f} vol | ${pnl:>8,.0f} PnL | ${pv:>8,.0f} portfolio")

        except Exception as e:
            logger.warning("Failed to enrich %s: %s", addr[:10], e)
            errors += 1

        time.sleep(1.0)  # Polite delay between Polymarket scrapes

    conn.close()
    print(f"\nEnriched {enriched}/{len(wallets)} whales ({errors} errors)")


def update_whale_win_rates(db_path=None):
    """Estimate whale win rates from trade direction data.

    Since we can't easily get resolved outcomes from on-chain for paper
    trading, this uses a heuristic: whale BUY trades are treated as "wins"
    (predicting upward movement) and SELL trades as "losses".

    For each whale in the trades table with a non-empty market_question:
      - total_trades = BUY + SELL
      - wins = BUY count
      - losses = SELL count
      - win_rate = wins / total_trades

    Updates the whales table in-place. If a whale has no trades, their
    existing win/loss stats are left unchanged.

    Args:
        db_path: Path to the SQLite DB. If None, uses PROJECT / "whale_data.db".
    """
    if db_path is None:
        # Default: use the standard project DB connection
        conn = get_conn()
        db_path = PROJECT / "whale_data.db"
    else:
        # Custom path: open a direct SQLite connection
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

    logger.info("Updating whale win rates from trades DB: %s", db_path)

    # Ensure win/loss columns exist (safe if already present)
    get_or_add_column(conn, "whales", "wins", "wins INTEGER DEFAULT 0")
    get_or_add_column(conn, "whales", "losses", "losses INTEGER DEFAULT 0")
    get_or_add_column(conn, "whales", "win_rate", "win_rate REAL")

    # Aggregate trades per whale: count BUY vs SELL
    rows = conn.execute(
        """SELECT
               wallet_address,
               wallet_label,
               COUNT(*) AS total_trades,
               SUM(CASE WHEN direction = 'BUY'  THEN 1 ELSE 0 END) AS buy_count,
               SUM(CASE WHEN direction = 'SELL' THEN 1 ELSE 0 END) AS sell_count
           FROM trades
           WHERE market_question IS NOT NULL AND market_question != ''
           GROUP BY wallet_address
        """
    ).fetchall()

    updated = 0
    for wallet_address, wallet_label, total, buys, sells in rows:
        if total == 0:
            continue

        wins = buys       # heuristic: BUY = win
        losses = sells    # heuristic: SELL = loss
        win_rate = round(wins / total, 4)

        conn.execute(
            """UPDATE whales SET
                   wins = MAX(wins, ?),
                   losses = MAX(losses, ?),
                   win_rate = MAX(COALESCE(win_rate, 0), ?),
                   trades_tracked = MAX(trades_tracked, ?)
               WHERE address = ?""",
            (wins, losses, win_rate, total, wallet_address.lower()),
        )
        updated += 1

        logger.debug(
            "  %s (%s) | %d trades | %d wins (BUY) | %d losses (SELL) | %.1f%%",
            wallet_label or wallet_address[:10],
            wallet_address[:10],
            total, wins, losses, win_rate * 100,
        )

    conn.commit()

    # Also update any whales that have no trades at all — set win_rate to 0
    conn.execute(
        """UPDATE whales SET win_rate = 0
           WHERE win_rate IS NULL AND address NOT IN (
               SELECT DISTINCT wallet_address FROM trades
           )"""
    )
    conn.commit()

    conn.close()
    print(f"  Updated win rates for {updated} whale(s)")


if __name__ == "__main__":
    import sys

    # Default: run enrich_all().  Pass --win-rates to run win-rate update instead.
    if "--win-rates" in sys.argv:
        update_whale_win_rates()
    else:
        enrich_all()
