"""
Configuration loader for Polymarket whale tracking bot.
Loads settings from whale_watch.json and .env.
"""
import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

CONFIG_PATH = Path(__file__).parent / "whale_watch.json"
DEFAULT_CONFIG = {
    "watched_wallets": [],
    "polygonscan_api_key": "",
    "clob_api_endpoint": "https://clob.polymarket.com",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    else:
        cfg = dict(DEFAULT_CONFIG)

    # Allow env var override for API key
    cfg["polygonscan_api_key"] = (
        os.getenv("POLYGONSCAN_API_KEY") or cfg.get("polygonscan_api_key") or ""
    )
    cfg["clob_api_endpoint"] = (
        os.getenv("CLOB_API_ENDPOINT") or cfg.get("clob_api_endpoint", "https://clob.polymarket.com")
    )
    return cfg


def save_config(cfg: dict):
    """Persist config changes back to disk (e.g. manually added wallets)."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
