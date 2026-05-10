"""
Telegram Alert Module — sends real-time whale conviction signals,
order book anomalies, and strategy reports to Telegram.

Uses Telegram Bot API via Hermes Agent's send_message or direct HTTP.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Load Telegram config from environment or config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1534029247")

SIGNAL_LOG = Path(__file__).parent / "whale_signals.jsonl"


def send_telegram(message: str) -> bool:
    """
    Send a message via Telegram Bot API.
    Falls back to printing to console if no bot token configured.
    """
    if not TELEGRAM_BOT_TOKEN:
        # Print to console instead — dashboard will pick it up
        logger.info("[TELEGRAM] %s", message[:200])
        return False

    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }, timeout=10)
        if r.status_code == 200:
            return True
        logger.warning("Telegram send failed: %s", r.text)
        return False
    except Exception as e:
        logger.warning("Telegram error: %s", e)
        return False


CONVICTION_TEMPLATE = """\
🐋 *Conviction Alert*
Whale: {whale}
Action: {action}
Contract: `{contract[:12]}...`
Confidence: {confidence:.0f}%
Details: {details}
⏱ {time}
"""

ORDERBOOK_TEMPLATE = """\
📚 *Order Book Signal*
Token: `{token_id[:12]}...`
Mid: {mid_price:.4f}
Spread: {spread:.4f}
Skew: {skew:+.3f}
Wall Score: {wall_score:+.2f}
{extra}
"""

STRATEGY_TEMPLATE = """\
🧠 *Strategy Update*
{content}
"""


def alert_conviction(signal: dict) -> bool:
    """Send a conviction signal alert."""
    msg = CONVICTION_TEMPLATE.format(
        whale=signal.get("wallet_label", "?"),
        action=signal.get("type", "?"),
        contract=signal.get("contract", ""),
        confidence=signal.get("confidence", 0) * 100,
        details=str(signal.get("details", ""))[:100],
        time=signal.get("timestamp", datetime.now(timezone.utc).isoformat())[:19],
    )
    return send_telegram(msg)


def alert_orderbook(book_signal: dict) -> bool:
    """Send order book anomaly alert."""
    extra_parts = []
    if book_signal.get("bid_wall"):
        extra_parts.append(f"🧱 Bid Wall at ${book_signal['bid_wall']:.4f} (${book_signal['bid_wall_size']:.0f})")
    if book_signal.get("ask_wall"):
        extra_parts.append(f"🧱 Ask Wall at ${book_signal['ask_wall']:.4f} (${book_signal['ask_wall_size']:.0f})")
    if book_signal.get("is_ask_thin"):
        extra_parts.append("📈 Thin Ask — ready to jump")
    if book_signal.get("is_bid_thin"):
        extra_parts.append("📉 Thin Bid — ready to drop")
    extra = "\n".join(extra_parts) if extra_parts else "No anomalies"

    msg = ORDERBOOK_TEMPLATE.format(
        token_id=book_signal.get("token_id", ""),
        mid_price=book_signal.get("mid_price", 0),
        spread=book_signal.get("spread", 0),
        skew=book_signal.get("skew", 0),
        wall_score=book_signal.get("wall_score", 0),
        extra=extra,
    )
    return send_telegram(msg)


def alert_strategy(report: str) -> bool:
    """Send a strategy update summary."""
    lines = report.split("\n")
    summary = "\n".join(lines[:20])  # First 20 lines
    msg = STRATEGY_TEMPLATE.format(content=summary)
    return send_telegram(msg)


def check_and_signal() -> list[str]:
    """
    Read the signal log and send any new high-confidence signals.
    Tracks last-sent position to avoid duplicates.
    Returns list of signals sent.
    """
    sent_file = Path(__file__).parent / "data" / "last_signals_sent.txt"
    sent_file.parent.mkdir(exist_ok=True)

    last_sent = ""
    if sent_file.exists():
        last_sent = sent_file.read_text().strip()

    sent_signals = []
    if SIGNAL_LOG.exists():
        with open(SIGNAL_LOG) as f:
            lines = f.readlines()

        # Only check last 10 lines for recent signals
        for line in lines[-10:]:
            try:
                sig = json.loads(line.strip())
                sig_id = sig.get("timestamp", "") + sig.get("type", "")
                if sig_id == last_sent:
                    break  # Already sent this batch
                if sig.get("confidence", 0) >= 0.6:
                    alert_conviction(sig)
                    sent_signals.append(sig.get("type", "?"))
            except (json.JSONDecodeError, Exception):
                continue

        if sent_signals and lines:
            try:
                last_entry = json.loads(lines[-1].strip())
                last_id = last_entry.get("timestamp", "") + last_entry.get("type", "")
                sent_file.write_text(last_id)
            except Exception:
                pass

    return sent_signals
