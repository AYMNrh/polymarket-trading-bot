"""
Database layer — stores trades, whales, signals, positions, PnL.
Uses SQLite for simplicity, no external DB needed.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "whale_data.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS whales (
                address TEXT PRIMARY KEY,
                label TEXT,
                first_seen TEXT,
                last_seen TEXT,
                total_volume REAL DEFAULT 0,
                win_rate REAL,
                trades_tracked INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT UNIQUE,
                wallet_address TEXT,
                wallet_label TEXT,
                timestamp TEXT,
                direction TEXT,
                token TEXT,
                token_name TEXT,
                value REAL,
                contract TEXT,
                block INTEGER,
                usdc_value REAL,
                market_question TEXT,
                is_whale INTEGER DEFAULT 0,
                FOREIGN KEY (wallet_address) REFERENCES whales(address)
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                signal_type TEXT,
                wallet_address TEXT,
                wallet_label TEXT,
                details TEXT,
                confidence REAL,
                acted_on INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT,
                market_question TEXT,
                outcome TEXT,
                entry_price REAL,
                current_price REAL,
                size REAL,
                direction TEXT,
                entered_at TEXT,
                updated_at TEXT,
                status TEXT DEFAULT 'open',
                pnl REAL,
                strategy TEXT
            );
            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY,
                whale_trades INTEGER DEFAULT 0,
                signals_generated INTEGER DEFAULT 0,
                total_whale_volume REAL DEFAULT 0,
                new_whales_discovered INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp);
        """)


def save_trade(trade: dict):
    """Insert or update a trade record. Updates market_question if provided later."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO trades
                (tx_hash, wallet_address, wallet_label, timestamp,
                 direction, token, token_name, value, contract, block,
                 usdc_value, market_question, is_whale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tx_hash) DO UPDATE SET
                market_question = COALESCE(excluded.market_question, trades.market_question),
                usdc_value = COALESCE(excluded.usdc_value, trades.usdc_value)
        """, (
            trade.get("tx_hash"),
            trade.get("address", trade.get("wallet_address")),
            trade.get("wallet_label", trade.get("wallet", "")),
            trade.get("timestamp"),
            trade.get("direction"),
            trade.get("token"),
            trade.get("token_name", ""),
            trade.get("value", 0),
            trade.get("contract", ""),
            trade.get("block", 0),
            trade.get("usdc_value", trade.get("value", 0)),
            trade.get("market_question", ""),
            1 if trade.get("is_whale", True) else 0,
        ))


def save_whale(whale: dict):
    """Insert or update a whale record. On update, only touches last_seen and label."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO whales
                (address, label, first_seen, last_seen, total_volume,
                 trades_tracked, wins, losses, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(address) DO UPDATE SET
                last_seen = COALESCE(excluded.last_seen, whales.last_seen),
                label = COALESCE(excluded.label, whales.label),
                is_active = 1
        """, (
            whale.get("address"),
            whale.get("label", whale["address"][:8]),
            whale.get("first_seen", datetime.now(timezone.utc).isoformat()),
            whale.get("last_seen", datetime.now(timezone.utc).isoformat()),
            whale.get("total_volume", 0),
            whale.get("trades_tracked", whale.get("num_trades", 0)),
            whale.get("wins", 0),
            whale.get("losses", 0),
        ))


def save_signal(signal: dict):
    """Save a trading signal."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO signals
                (timestamp, signal_type, wallet_address, wallet_label, details, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            signal.get("timestamp", datetime.now(timezone.utc).isoformat()),
            signal.get("type", "unknown"),
            signal.get("wallet_address", ""),
            signal.get("wallet_label", ""),
            json.dumps(signal, default=str),
            signal.get("confidence", 0.5),
        ))


def get_recent_trades(limit: int = 50) -> list[dict]:
    """Get most recent trades."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_whale_summary() -> list[dict]:
    """Get all whales with stats."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT w.*, COUNT(t.id) as total_trades,
                   COALESCE(SUM(t.value), 0) as volume
            FROM whales w
            LEFT JOIN trades t ON t.wallet_address = w.address
            WHERE w.is_active = 1
            GROUP BY w.address
            ORDER BY volume DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_recent_signals(limit: int = 20) -> list[dict]:
    """Get most recent signals."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats() -> dict:
    """Get overall stats for dashboard."""
    with get_conn() as conn:
        stats = {}
        stats["total_whales"] = conn.execute(
            "SELECT COUNT(*) FROM whales WHERE is_active=1"
        ).fetchone()[0]
        stats["total_trades"] = conn.execute(
            "SELECT COUNT(*) FROM trades"
        ).fetchone()[0]
        stats["total_signals"] = conn.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]
        stats["total_volume"] = conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM trades"
        ).fetchone()[0]
        stats["today_trades"] = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE date(timestamp) = date('now')"
        ).fetchone()[0]
        stats["today_volume"] = conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM trades WHERE date(timestamp) = date('now')"
        ).fetchone()[0]
        return stats


# Initialize on import
init_db()
