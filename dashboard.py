"""
Web dashboard — FastAPI app showing whale activity, trades, signals,
order book analysis, whale conviction, and strategy reports.

Run with: uvicorn dashboard:app --reload --port 9091
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from database import (
    get_recent_trades, get_whale_summary, get_recent_signals, get_stats
)
from position_tracker import PositionTracker
from self_learning import SelfLearningEngine
from onchain_decoder import OnChainTradeDecoder
from polymarket_scraper import PolymarketScraper
from live_strategy2 import load_live_events, load_live_state
from paper_trader import STRATEGY1_MODE, STRATEGY2_MODE, STRATEGY3_MODE, RUNTIME_LOG, PaperTrader

logger = logging.getLogger(__name__)

app = FastAPI(title="Whale Tracker Dashboard")
tracker = PositionTracker()
learner = SelfLearningEngine()
decoder = OnChainTradeDecoder(etherscan_key=os.getenv("POLYGONSCAN_API_KEY", ""))
scraper = PolymarketScraper()
paper_trader = PaperTrader()

LOG_FILE = Path(__file__).parent / "whale_trades.jsonl"
ORDERBOOK_CACHE = Path(__file__).parent / "data" / "orderbook_cache.json"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="30">
    <title>🐋 Whale Tracker</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0a0a0f;
            color: #e0e0e0;
            padding: 20px;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        h1 {{ font-size: 2em; margin-bottom: 20px; color: #00d4aa; }}
        h2 {{ font-size: 1.3em; margin: 20px 0 10px; color: #888; }}
        .nav {{ display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }}
        .nav a {{ padding: 8px 16px; background: #14141f; border: 1px solid #1e1e2e;
                  border-radius: 6px; color: #aaa; text-decoration: none; font-size: 0.9em; }}
        .nav a:hover {{ background: #1e1e2e; color: #00d4aa; }}
        .nav a.active {{ background: #1a3a3a; color: #00d4aa; border-color: #00d4aa; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                  gap: 12px; margin-bottom: 24px; }}
        .stat-card {{
            background: #14141f; border-radius: 10px; padding: 16px;
            border: 1px solid #1e1e2e;
        }}
        .stat-card .value {{ font-size: 1.8em; font-weight: 700; color: #fff; }}
        .stat-card .label {{ font-size: 0.8em; color: #666; margin-top: 4px; }}
        .stat-card .sub {{ font-size: 0.75em; color: #555; margin-top: 2px; }}
        table {{ width: 100%; border-collapse: collapse; background: #14141f;
                 border-radius: 10px; overflow: hidden; }}
        th {{ background: #1a1a2e; padding: 10px 12px; text-align: left;
              font-size: 0.8em; color: #666; text-transform: uppercase; }}
        td {{ padding: 8px 12px; border-top: 1px solid #1e1e2e; font-size: 0.9em; }}
        tr:hover {{ background: #1a1a2e; }}
        .buy {{ color: #00d4aa; }}
        .sell {{ color: #ff6b6b; }}
        .positive {{ color: #00d4aa; }}
        .negative {{ color: #ff6b6b; }}
        .neutral {{ color: #888; }}
        .badge {{
            display: inline-block; padding: 2px 8px; border-radius: 4px;
            font-size: 0.75em; font-weight: 600;
        }}
        .badge-whale {{ background: #1a3a3a; color: #00d4aa; }}
        .badge-signal {{ background: #3a2a1a; color: #ffaa00; }}
        .badge-wall {{ background: #2a1a3a; color: #bb86ff; }}
        .badge-thin {{ background: #3a1a1a; color: #ff6b6b; }}
        .badge-conviction {{ background: #1a2a3a; color: #64b5f6; }}
        .badge-flip {{ background: #3a2a1a; color: #ffaa00; }}
        .order-book {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 10px 0; }}
        .book-side {{ padding: 10px; border-radius: 6px; }}
        .book-bids {{ background: #0a1a14; border: 1px solid #1a3a2a; }}
        .book-asks {{ background: #1a0a0a; border: 1px solid #3a1a1a; }}
        .book-level {{ display: flex; justify-content: space-between; padding: 2px 0;
                       font-size: 0.8em; font-family: monospace; }}
        .wall-highlight {{ color: #bb86ff; font-weight: 700; }}
        .thin-highlight {{ color: #ff6b6b; font-weight: 700; }}
        .section {{ margin-bottom: 30px; }}
        .refresh {{ color: #555; font-size: 0.8em; margin-top: 20px; text-align: center; }}
        .report-block {{ background: #14141f; border: 1px solid #1e1e2e; border-radius: 8px;
                        padding: 16px; white-space: pre-wrap; font-family: monospace;
                        font-size: 0.85em; line-height: 1.5; }}
        pre {{ font-family: monospace; font-size: 0.85em; line-height: 1.6; color: #ccc; }}
        .trend-icon {{ font-size: 1.2em; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🐋 Whale Tracker</h1>

        <div class="nav">
            <a href="/" class="active">Dashboard</a>
            <a href="/?view=candidates">Whale Candidates</a>
            <a href="/?view=paper">Paper Portfolio</a>
            <a href="/?view=strategy1">Strategy 1</a>
            <a href="/?view=strategy2">Strategy 2</a>
            <a href="/?view=live-strategy2">Live S2 Test</a>
            <a href="/?view=strategy3">Strategy 3</a>
            <a href="/?view=strategy-report">Strategy Report</a>
        </div>

        <div class="stats">
            <div class="stat-card">
                <div class="value">{whales}</div>
                <div class="label">Whales Tracked</div>
            </div>
            <div class="stat-card">
                <div class="value">{trades}</div>
                <div class="label">Total Trades</div>
            </div>
            <div class="stat-card">
                <div class="value">${volume:.0f}</div>
                <div class="label">Total Volume (USDC)</div>
            </div>
            <div class="stat-card">
                <div class="value">{signals}</div>
                <div class="label">Signals Generated</div>
            </div>
            <div class="stat-card">
                <div class="value">{today_trades}</div>
                <div class="label">Trades Today</div>
            </div>
            <div class="stat-card">
                <div class="value">${today_volume:.0f}</div>
                <div class="label">Volume Today</div>
            </div>
            <div class="stat-card">
                <div class="value">{positions}</div>
                <div class="label">Active Positions Tracked</div>
            </div>
            <div class="stat-card">
                <div class="value">{high_conviction}</div>
                <div class="label">High Conviction Alerts</div>
            </div>
        </div>

        {main_content}

        <div class="refresh">Auto-refreshes every 30s · {now}</div>
    </div>
    <script>setTimeout(() => location.reload(), 30000)</script>
</body>
</html>"""


def _load_orderbook_cache() -> list:
    if ORDERBOOK_CACHE.exists():
        try:
            return json.loads(ORDERBOOK_CACHE.read_text())
        except Exception:
            return []
    return []


def _render_dashboard() -> str:
    stats = get_stats()
    whales = get_whale_summary()
    trades = get_recent_trades(30)
    signals = get_recent_signals(20)
    positions_data = tracker.get_whale_positions()
    conviction_data = tracker.get_conviction_signals(min_score=0.5)

    stats["positions"] = len(positions_data)
    stats["high_conviction"] = len(conviction_data)

    # Cycle/experiment stats (used in template f-strings below)
    cycle = {"discovered_markets": 0, "liquid_candidates": 0,
             "ev_candidates": 0, "tradable_candidates": 0}
    experiment = {"scope": "paper-v1", "duration_days": 3}
    summary = stats.copy()
    summary["last_learning_review_date"] = None

    from database import get_whale_profile

    # Whales table — enrich with cached Polymarket profile data from DB
    whale_rows = ""
    for w in whales[:15]:
        wr = w.get("win_rate")
        wr_str = f"{wr*100:.0f}%" if wr else "—"
        addr = w["address"]
        label = w.get("label", "?")

        # Load enriched profile from DB (populated by enrich_whales cron)
        profile_data = get_whale_profile(addr)
        if profile_data:
            summary = profile_data.get("summary", {})
            live_pv = summary.get("portfolio_value") or summary.get("value") or None
            live_pnl = summary.get("total_pnl") or None
        else:
            # Fallback: live scrape (slow)
            try:
                profile = scraper.get_profile(addr)
                live_pv = profile.get("portfolio_value")
                live_pnl = profile.get("total_pnl")
            except Exception:
                live_pv = None
                live_pnl = None

        pv_str = f"${live_pv:,.0f}" if live_pv else "—"
        pnl_str = f"+${live_pnl:,.0f}" if (live_pnl and live_pnl > 0) else (f"${live_pnl:,.0f}" if live_pnl else "—")
        pm_link = f"https://polymarket.com/profile/{addr}"
        whale_rows += (
            f"<tr>"
            f"<td>{label}</td>"
            f"<td style='font-family:mono;font-size:0.8em'>"
            f"<a href='{pm_link}' target='_blank' style='color:#64b5f6;text-decoration:none' title='View on Polymarket'>{addr[:10]}...{addr[-6:]} &#8599;</a></td>"
            f"<td>${w.get('volume', 0):.0f}</td>"
            f"<td>{w.get('trades_tracked', w.get('total_trades', 0))}</td>"
            f"<td>{pv_str}</td>"
            f"<td style='color:{'#00d4aa' if live_pnl and live_pnl > 0 else '#ff6b6b' if live_pnl and live_pnl < 0 else '#888'}'>{pnl_str}</td>"
            f"<td class='positive'>{wr_str}</td>"
            f"<td><span class='badge badge-whale'>active</span></td>"
            f"</tr>"
        )

    # Trades table
    trade_rows = ""
    for t in trades[:30]:
        cls = "buy" if t.get("direction", "").upper() == "BUY" else "sell"
        trade_rows += (
            f"<tr>"
            f"<td>{t.get('timestamp', '?')[:19]}</td>"
            f"<td>{t.get('wallet_label', t.get('wallet', '?'))}</td>"
            f"<td class='{cls}'>{t.get('direction', '?')}</td>"
            f"<td>{t.get('token', '?')}</td>"
            f"<td>${t.get('value', 0):.2f}</td>"
            f"<td style='font-size:0.8em;color:#888'>{str(t.get('market_question', '') or '—')[:40]}</td>"
            f"</tr>"
        )

    # Signals table
    signal_rows = ""
    for s in signals[:20]:
        details = s.get("details", "{}")
        try:
            detail_obj = json.loads(details) if isinstance(details, str) else details
            detail_str = json.dumps(detail_obj, indent=1)[:80]
        except (json.JSONDecodeError, TypeError):
            detail_str = str(details)[:80]
        signal_rows += (
            f"<tr>"
            f"<td>{s.get('timestamp', '?')[:19]}</td>"
            f"<td><span class='badge badge-signal'>{s.get('signal_type', '?')}</span></td>"
            f"<td>{s.get('wallet_label', '?')}</td>"
            f"<td style='font-size:0.8em;color:#888'>{detail_str}</td>"
            f"</tr>"
        )

    # Conviction section
    position_rows = ""
    for p in positions_data[:15]:
        icon = {"rising": "📈", "falling": "📉", "flipping": "🔄", "neutral": "➖"}.get(
            p.get("conviction_trend", "neutral"), "➖")
        conf_pct = p.get("conviction", 0) * 100
        position_rows += (
            f"<tr>"
            f"<td>{icon}</td>"
            f"<td>{p.get('wallet', '?')}</td>"
            f"<td style='font-family:mono;font-size:0.8em'>{p.get('contract', '?')[:12]}...</td>"
            f"<td>{p.get('direction', '?')}</td>"
            f"<td>${p.get('net_size', 0):.0f}</td>"
            f"<td>{p.get('num_trades', 0)}</td>"
            f"<td class='{'positive' if conf_pct > 50 else 'neutral'}'>{conf_pct:.0f}%</td>"
            f"<td><span class='badge badge-conviction'>{p.get('conviction_trend', 'neutral')}</span></td>"
            f"</tr>"
        )

    main = f"""
        <div class="section">
            <h2>🐋 Whales</h2>
            <div class="stats" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-top:10px">
                <div class="stat-card">
                    <div class="value">{cycle.get('discovered_markets', 0)}</div>
                    <div class="label">Markets Scanned</div>
                </div>
                <div class="stat-card">
                    <div class="value">{cycle.get('liquid_candidates', 0)}</div>
                    <div class="label">Liquid Candidates</div>
                </div>
                <div class="stat-card">
                    <div class="value">{cycle.get('ev_candidates', 0)}</div>
                    <div class="label">EV Candidates</div>
                </div>
                <div class="stat-card">
                    <div class="value">{cycle.get('tradable_candidates', 0)}</div>
                    <div class="label">Tradable After Overlay</div>
                </div>
            </div>

            <p style="color:#555;font-size:0.8em;margin:10px 0 14px">
                Experiment: {experiment.get('scope', 'paper-v1')} ·
                Duration: {experiment.get('duration_days', 3)} days ·
                Last learning review: {summary.get('last_learning_review_date') or 'pending'}
            </p>

            <table>
                <tr><th>Label</th><th>Address</th><th>Volume</th><th>Trades</th><th>Portfolio</th><th>PnL</th><th>Win Rate</th><th>Status</th></tr>
                {whale_rows}
            </table>
        </div>

        <div class="section">
            <h2>📊 Recent Trades</h2>
            <table>
                <tr><th>Time</th><th>Whale</th><th>Direction</th><th>Token</th><th>Value</th><th>Market</th></tr>
                {trade_rows}
            </table>
        </div>

        <div class="section">
            <h2>🚀 Conviction Positions</h2>
            <table>
                <tr><th></th><th>Whale</th><th>Contract</th><th>Side</th><th>Size</th><th>Trades</th><th>Conviction</th><th>Trend</th></tr>
                {position_rows}
            </table>
        </div>

        <div class="section">
            <h2>🚨 Recent Signals</h2>
            <table>
                <tr><th>Time</th><th>Type</th><th>Whale</th><th>Details</th></tr>
                {signal_rows}
            </table>
        </div>
    """

    return HTML_TEMPLATE.format(
        whales=stats.get("total_whales", 0),
        trades=stats.get("total_trades", 0),
        volume=stats.get("total_volume", 0),
        signals=stats.get("total_signals", 0),
        today_trades=stats.get("today_trades", 0),
        today_volume=stats.get("today_volume", 0),
        positions=stats.get("positions", 0),
        high_conviction=stats.get("high_conviction", 0),
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _render_orderbook() -> str:
    """Order book analysis view."""
    books = _load_orderbook_cache()
    rows = ""
    for b in books[:30]:
        walls = []
        if b.get("bid_wall"):
            walls.append(f'<span class="badge badge-wall">BID WALL ${b["bid_wall"]:.4f}</span>')
        if b.get("ask_wall"):
            walls.append(f'<span class="badge badge-wall">ASK WALL ${b["ask_wall"]:.4f}</span>')
        if b.get("is_ask_thin"):
            walls.append('<span class="badge badge-thin">THIN ASK</span>')
        if b.get("is_bid_thin"):
            walls.append('<span class="badge badge-thin">THIN BID</span>')
        wall_str = " ".join(walls)
        skew_str = f'{b["skew"]:+.3f}' if b.get("skew") is not None else "—"
        rows += (
            f"<tr>"
            f"<td style='font-family:mono;font-size:0.8em'>{b.get('token_id', '?')[:14]}...</td>"
            f"<td>{b.get('mid_price', 0):.4f}</td>"
            f"<td>{b.get('spread', 0):.4f}</td>"
            f"<td>{skew_str}</td>"
            f"<td>{b.get('wall_score', 0):+.2f}</td>"
            f"<td>{b.get('bid_depth', 0):.2f}</td>"
            f"<td>{b.get('ask_depth', 0):.2f}</td>"
            f"<td>{wall_str}</td>"
            f"</tr>"
        )

    if not rows:
        rows = "<tr><td colspan='8' style='color:#555;text-align:center'>No order book data yet. Run a strategy cycle first.</td></tr>"

    main = f"""
        <div class="section">
            <h2>📚 Order Book Analysis</h2>
            <p style="color:#666;font-size:0.85em;margin-bottom:10px">
                Wall score > +0.3 = bullish (big bid wall + thin ask). 
                Wall score < -0.3 = bearish (big ask wall).
                Skew < -0.3 = more bid capital. Skew > +0.3 = more ask capital.
            </p>
            <table>
                <tr><th>Token</th><th>Mid</th><th>Spread</th><th>Skew</th><th>Wall Score</th><th>Bid $</th><th>Ask $</th><th>Signals</th></tr>
                {rows}
            </table>
        </div>
    """

    return HTML_TEMPLATE.format(
        whales="—", trades="—", volume=0, signals="—",
        today_trades="—", today_volume=0, positions="—", high_conviction="—",
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _render_conviction() -> str:
    """Conviction / position change view."""
    positions = tracker.get_whale_positions()
    rows = ""
    for p in positions[:30]:
        icon = {"rising": "\U0001f4c8", "falling": "\U0001f4c9", "flipping": "\U0001f504", "neutral": "\u2795"}.get(
            p.get("conviction_trend", "neutral"), "\u2795")
        conf = p.get("conviction", 0)
        trend = p.get("conviction_trend", "neutral")
        if conf > 0.6:
            bar_color = "#00d4aa"
        elif conf > 0.3:
            bar_color = "#ffaa00"
        else:
            bar_color = "#ff6b6b"
        trend_badge = {
            "rising": '<span class="badge badge-conviction">\U0001f680 RISING</span>',
            "falling": '<span class="badge badge-thin">\U0001f4c9 FALLING</span>',
            "flipping": '<span class="badge badge-flip">\U0001f504 FLIPPING</span>',
            "neutral": '<span class="badge" style="background:#1e1e2e;color:#888">\u2795 NEUTRAL</span>',
        }.get(trend, "")

        dir_cls = "buy" if p.get("direction") == "BUY" else "sell"
        contract_short = p.get("contract", "?")[:16]
        last_trade_short = (p.get("last_trade", "") or "")[:16]

        rows += (
            f"<tr>"
            f"<td>{icon}</td>"
            f"<td>{p.get('wallet', '?')}</td>"
            f"<td style='font-family:mono;font-size:0.8em'>{contract_short}...</td>"
            f"<td class='{dir_cls}'>{p.get('direction', '?')}</td>"
            f"<td>${p.get('net_size', 0):.0f}</td>"
            f"<td>{p.get('num_trades', 0)}</td>"
            f"<td><div style='width:80px;height:8px;background:#1e1e2e;border-radius:4px;overflow:hidden'>"
            f"<div style='width:{conf*100:.0f}%;height:100%;background:{bar_color};border-radius:4px'></div>"
            f"</div></td>"
            f"<td>{trend_badge}</td>"
            f"<td style='font-size:0.8em;color:#555'>{last_trade_short}</td>"
            f"</tr>"
        )

    if not rows:
        rows = "<tr><td colspan='9' style='color:#555;text-align:center'>No positions tracked yet. Whale data will populate as trades are detected.</td></tr>"

    # Summary stats
    rising = sum(1 for p in positions if p.get("conviction_trend") == "rising")
    falling = sum(1 for p in positions if p.get("conviction_trend") == "falling")
    flipping = sum(1 for p in positions if p.get("conviction_trend") == "flipping")

    main = f"""
        <div class="section">
            <h2>🚀 Position Conviction Tracker</h2>
            <div class="stats" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
                <div class="stat-card">
                    <div class="value" style="color:#64b5f6">{len(positions)}</div>
                    <div class="label">Total Positions</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#00d4aa">{rising}</div>
                    <div class="label">Conviction Rising 📈</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#ff6b6b">{falling}</div>
                    <div class="label">Doubt Creeping 📉</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#ffaa00">{flipping}</div>
                    <div class="label">Flipping 🔄</div>
                </div>
            </div>
            <table>
                <tr><th></th><th>Whale</th><th>Contract</th><th>Side</th><th>Size</th><th>Trades</th><th>Conviction</th><th>Trend</th><th>Last Trade</th></tr>
                {rows}
            </table>
        </div>
    """

    return HTML_TEMPLATE.format(
        whales="—", trades="—", volume=0, signals="—",
        today_trades="—", today_volume=0, positions=len(positions), high_conviction=len([p for p in positions if p.get("conviction", 0) >= 0.5]),
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _render_strategy() -> str:
    """Self-learning strategy report view."""
    report = learner.strategy_report()
    recs = learner.get_recommendations()
    rec_rows = ""
    for r in recs:
        rec_rows += f"<tr><td>• {r}</td></tr>"

    main = f"""
        <div class="section">
            <h2>🧠 Strategy Report</h2>
            <div class="report-block">{report}</div>
        </div>

        <div class="section">
            <h2>Active Recommendations</h2>
            <table>
                <tr><th>Recommendation</th></tr>
                {rec_rows if rec_rows else '<tr><td style="color:#555">No recommendations yet. Let the bot run some trades first.</td></tr>'}
            </table>
        </div>

        <div class="section">
            <h2>What the Bot Watches</h2>
            <div class="report-block" style="color:#888">
<pre>
┌─────────────────────────────────────────────────────┐
│  Layer 1: Order Book                                 │
│  • Big bid wall → support (score +0.3)              │
│  • Thin ask → price jumps (score +0.3)              │
│  • Ask wall overhead → resistance (score -0.25)     │
│  • Skew: capital imbalance direction                 │
├─────────────────────────────────────────────────────┤
│  Layer 2: Whale Conviction                           │
│  • Adding → conviction rising (+0.2 per add)        │
│  • Trimming → doubt creeping (-0.15 per trim)       │
│  • Flipping → knows something (+0.9 confidence)     │
├─────────────────────────────────────────────────────┤
│  Layer 3: EV (Forecast vs Market)                    │
│  • Fair price - current price = edge                │
│  • Order book confirms = boost EV 20%              │
│  • Order book opposes = reduce EV 30%              │
├─────────────────────────────────────────────────────┤
│  Layer 4: Self-Learning                              │
│  • Reviews every resolved trade                      │
│  • Tracks WR by bucket type, city, source            │
│  • Auto-adjusts: min_ev, kelly_fraction, max_price  │
└─────────────────────────────────────────────────────┘
</pre>
            </div>
        </div>
    """

    return HTML_TEMPLATE.format(
        whales="—", trades="—", volume=0, signals="—",
        today_trades="—", today_volume=0, positions="—", high_conviction="—",
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _render_whale_trades(selected_addr: str = "") -> str:
    """Whale Trades tab — decoded on-chain trades with Polymarket links. Selectable per whale."""
    from config import load_config
    cfg = load_config()
    wallets = cfg.get("watched_wallets", [])

    # Default to first whale if none selected
    if not selected_addr and wallets:
        selected_addr = wallets[0]["address"]

    # Find selected whale info
    selected_label = "Whale"
    selected_info = None
    for w in wallets:
        if w["address"].lower() == selected_addr.lower():
            selected_label = w.get("label", w["address"][:10])
            selected_info = w
            break
    if not selected_info and wallets:
        selected_info = wallets[0]
        selected_addr = wallets[0]["address"]
        selected_label = wallets[0].get("label", "Whale")

    # Build dropdown options
    dropdown_options = ""
    for w in wallets:
        addr = w["address"]
        label = w.get("label", addr[:10])
        is_known = label in ("ColdMath", "Sharky6999", "RN1")
        display = f"🐋 {label}" if is_known else f"👤 {label}"
        sel = "selected" if addr.lower() == selected_addr.lower() else ""
        dropdown_options += f'<option value="{addr}" {sel}>{display}</option>'

    # Fetch on-chain decoded trades for selected whale
    live = []
    onchain_error = ""
    try:
        # Get recent trades from last ~500k blocks
        live = decoder.poll_latest_trades(selected_addr, since_block=86000000, max_txs=30)
    except Exception as e:
        onchain_error = str(e)[:100]

    # Fetch portfolio data from Polymarket scraper for ALL whales
    portfolio_positions = []
    scraped_summary = {}
    try:
        portfolio_positions = scraper.get_positions(selected_addr)
        scraped_summary = scraper.portfolio_summary(selected_addr)
    except Exception as e:
        logger.warning("Scraper failed for %s: %s", selected_label, e)

    total_value = scraped_summary.get("value", 0)
    total_pnl = scraped_summary.get("pnl", 0)
    total_shares = scraped_summary.get("total_shares", 0)
    total_initial_value = sum(p.get("initial_value", 0) for p in portfolio_positions) if portfolio_positions else 0

    # Polymarket portfolio table (live scraped for ALL whales)
    portfolio_rows = ""
    if portfolio_positions:
        for p in portfolio_positions:
            title = p.get("title", "?")
            direction = p.get("side", "?")
            shares = p.get("shares", 0)
            entry = p.get("entry_price", 0)
            current = p.get("current_price", 0)
            value = p.get("value", 0)
            pnl = p.get("pnl", 0)
            slug = p.get("slug", "") or p.get("event_slug", "")

            pnl_cls = "positive" if pnl >= 0 else "negative"
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            direction_cls = "buy" if direction == "YES" else "sell"
            event_slug_result = p.get("event_slug", "") or p.get("slug", "")
            polymarket_url = f"https://polymarket.com/event/{event_slug_result}" if event_slug_result else "#"
            q_short = title[:65]
            alloc_pct = (p.get("initial_value", value) / max(1, total_initial_value)) * 100

            portfolio_rows += (
                f"<tr>"
                f"<td class='{direction_cls}'>{direction}</td>"
                f"<td><a href='{polymarket_url}' target='_blank' style='color:#64b5f6;text-decoration:none' "
                f"title='{title}'>{q_short}...</a></td>"
                f"<td>{shares:.0f}</td>"
                f"<td>{entry*100:.1f}¢</td>"
                f"<td>{current*100:.1f}¢</td>"
                f"<td>${value:,.2f}</td>"
                f"<td>{alloc_pct:.1f}%</td>"
                f"<td class='{pnl_cls}'>{pnl_str}</td>"
                f"<td style='font-size:0.8em'><a href='{polymarket_url}' target='_blank' "
                f"style='color:#555;text-decoration:none'>\U0001f517</a></td>"
                f"</tr>"
            )

    # On-chain decoded trades table
    onchain_rows = ""
    for t in live[:15]:
        tx_hash = (t.get("tx_hash") or "")
        tx_link = f"https://polygonscan.com/tx/0x{tx_hash}" if tx_hash else "#"
        onchain_rows += (
            f"<tr>"
            f"<td>{t.get('question', '?')[:60]}...</td>"
            f"<td>${t.get('usdc_value', 0):.2f}</td>"
            f"<td style='font-size:0.8em;color:#555'>block {t.get('block', '?')}</td>"
            f"<td style='font-size:0.8em'><a href='{tx_link}' "
            f"target='_blank' style='color:#555;text-decoration:none'>\u26d3\ufe0f</a></td>"
            f"</tr>"
        )

    if not onchain_rows:
        no_trades_msg = "No recent on-chain trades decoded yet."
        if onchain_error:
            no_trades_msg += f" Decoder error: {onchain_error}"
        else:
            no_trades_msg += " This whale may not have traded recently, or uses a different Polymarket contract."
        onchain_rows = f'<tr><td colspan="4" style="color:#555;text-align:center">{no_trades_msg}</td></tr>'

    # Strategy section — changes per whale based on available data
    strategy_notes = ""
    if portfolio_positions:
        avg_entry = sum(p.get("entry_price", 0) for p in portfolio_positions) / max(1, len(portfolio_positions))
        avg_current = sum(p.get("current_price", 0) for p in portfolio_positions) / max(1, len(portfolio_positions))
        total_pnl_pct = (scraped_summary.get("total_pnl", 0) / max(1, scraped_summary.get("total_volume", 1))) * 100
        first_pnl = scraped_summary.get("first_pnl", 0)
        first_pnl_note = f"Started at ${first_pnl:.0f} PnL" if first_pnl else "First trade data unavailable"
        strategy_notes = f"""\
<pre>
┌──────────────────────────────────────────────────────────────┐
│  {selected_label} — Full Profile (live from Polymarket)           │
├──────────────────────────────────────────────────────────────┤
│  Joined:        {str(scraped_summary.get('join_date', '?'))[:10]}  ({scraped_summary.get('days_active', 0)} days ago)    │
│  Trades:        {scraped_summary.get('trades', 0):>8,}  ({scraped_summary.get('trades_per_day', 0):.1f}/day)       │
│  Total Volume:  ${scraped_summary.get('total_volume', 0):>10,.0f}                       │
│  All-Time PnL:  ${scraped_summary.get('total_pnl', 0):>10,.0f}  ({total_pnl_pct:.1f}% ROI)           │
│  Portfolio:     ${total_value:>10,.0f} ({scraped_summary.get('positions', 0)} positions)             │
│  Biggest Win:   ${scraped_summary.get('biggest_win', 0):>10,.0f}                       │
│  Markets:       {scraped_summary.get('markets_traded', 0):>8,} unique                        │
│  Win Rate:      ~{min(100, scraped_summary.get('trades_per_day', 0)*2):.0f}% (estimated from resolved)           │
├──────────────────────────────────────────────────────────────┤
│  🥇 First recorded: {first_pnl_note}            │
├──────────────────────────────────────────────────────────────┤
│  Portfolio: https://polymarket.com/profile/                  │
│  {selected_addr}                 │
└──────────────────────────────────────────────────────────────┘</pre>"""
    else:
        onchain_markets = len(set(t.get("question", "?") for t in live))
        strategy_notes = f"""\
<pre>
┌──────────────────────────────────────────────────────────────┐
│  {selected_label} — On-Chain Activity                            │
├──────────────────────────────────────────────────────────────┤
│  • Recent decoded trades: {len(live)}                                │
│  • Unique markets: {onchain_markets}                                      │
│  • Data source: USDC transfers → NegRisk CTF events         │
│  • Decoded via: Etherscan V2 + Gamma API                    │
│                                                              │
│  Portfolio data unavailable — Polymarket profile may not     │
│  have any open positions for this wallet.                    │
├──────────────────────────────────────────────────────────────┤
│  Polymarket Explorer: https://polymarket.com/profile/        │
│  {selected_addr}                 │
└──────────────────────────────────────────────────────────────┘</pre>"""

    # Count of live decoded events
    decoded_count = len(live)

    main = f"""\r
        <div class="section">\r
            <h2>🐋 Whale Trades\r
                <a href="https://polymarket.com/profile/{selected_addr}" target="_blank"\r
                   style="font-size:0.6em;color:#64b5f6;text-decoration:none;margin-left:12px;background:#14141f;padding:4px 12px;border-radius:6px;border:1px solid #1e1e2e">\r
                   View on Polymarket ↗</a>\r
            </h2>\r
            <div style="margin-bottom:16px">
                <label for="whale-select" style="color:#888;font-size:0.9em;margin-right:8px">Select whale:</label>
                <select id="whale-select" onchange="window.location.href='/?view=trades&wallet='+this.value"
                    style="background:#14141f;color:#e0e0e0;border:1px solid #1e1e2e;
                           border-radius:6px;padding:8px 16px;font-size:0.9em;min-width:200px">
                    {dropdown_options}
                </select>
            </div>

            <div class="stats" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
                <div class="stat-card">
                    <div class="value" style="color:#00d4aa">${total_value:,.0f}</div>
                    <div class="label">Portfolio Value</div>
                    <div class="sub">{scraped_summary.get('positions', 0)} positions</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:{'#ffaa00' if scraped_summary.get('total_pnl', 0) >= 0 else '#ff6b6b'}">
                        {'+' if scraped_summary.get('total_pnl', 0) >= 0 else ''}${scraped_summary.get('total_pnl', 0):,.0f}</div>
                    <div class="label">All-Time PnL</div>
                    <div class="sub">${scraped_summary.get('position_pnl', 0):,.0f} open positions</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#64b5f6">{scraped_summary.get('trades', 0):,}</div>
                    <div class="label">Total Trades</div>
                    <div class="sub">{scraped_summary.get('trades_per_day', 0):.1f}/day</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#bb86ff">${scraped_summary.get('total_volume', 0):,.0f}</div>
                    <div class="label">Total Volume</div>
                    <div class="sub">{scraped_summary.get('markets_traded', 0)} markets</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#ffaa00">${scraped_summary.get('biggest_win', 0):,.0f}</div>
                    <div class="label">Biggest Win</div>
                    <div class="sub">{str(scraped_summary.get('biggest_win_market', ''))[:25]}</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#888">{scraped_summary.get('days_active', 0)}</div>
                    <div class="label">Days Active</div>
                    <div class="sub">since {str(scraped_summary.get('join_date', ''))[:10]}</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#64b5f6">{len(portfolio_positions) if portfolio_positions else '—'}</div>
                    <div class="label">Open Positions</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#888">{decoded_count}</div>
                    <div class="label">On-Chain Trades Decoded</div>
                </div>
            </div>

            <p style="color:#666;font-size:0.85em;margin-bottom:10px">
                Showing data for <strong style="color:#bbb">{selected_label}</strong>.
                Portfolio table shown when scraped Polymarket data is available.
                On-chain trades decoded from USDC transfers → NegRisk CTF events → Gamma API market lookup.
            </p>
        </div>
"""

    # Only show portfolio table if we have scraped data
    if portfolio_positions:
        main += f"""
        <div class="section">
            <h2>📊 {selected_label} — Polymarket Portfolio</h2>
            <table>
                <tr>
                    <th>Side</th><th>Market</th><th>Shares</th><th>Entry</th>
                    <th>Current</th><th>Value</th><th>Alloc</th><th>PnL</th><th>Link</th>
                </tr>
                {portfolio_rows}
            </table>
        </div>
"""

    # On-chain decoded section
    main += f"""
        <div class="section">
            <h2>⛓️ On-Chain Decoded Trades</h2>
            <p style="color:#666;font-size:0.85em;margin-bottom:10px">
                Decoded from USDC transfers to NegRisk CTF → EVENT_POSITION events → Gamma API market lookup.
                Each row = one decoded trade. ⛓️ links to PolygonScan.
            </p>
            <table>
                <tr><th>Market</th><th>USDC</th><th>Block</th><th>Tx</th></tr>
                {onchain_rows}
            </table>
        </div>
"""

    # Strategy analysis section
    main += f"""
        <div class="section">
            <h2>🧠 Strategy Analysis</h2>
            <div class="report-block" style="color:#888;font-size:0.85em">
                {strategy_notes}
            </div>
        </div>
"""

    return HTML_TEMPLATE.format(
        whales="—", trades="—", volume=0, signals="—",
        today_trades="—", today_volume=0,
        positions=len(portfolio_positions) if portfolio_positions else decoded_count,
        high_conviction=sum(1 for p in portfolio_positions if p.get("pnl", 0) > 10) if portfolio_positions else 0,
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _render_paper_portfolio() -> str:
    """Paper Portfolio tab — EV-driven trading with whale overlay."""
    summary = paper_trader.summary()
    positions = paper_trader.get_open_positions()
    closed = paper_trader.get_closed_positions(10)

    bankroll_pct = summary["exposure"] / max(1, summary["bankroll"]) * 100
    params = summary.get("parameters", {})
    cycle = summary.get("last_cycle_report", {})
    experiment = summary.get("experiment", {})

    win_rate = summary["wins"] / max(1, summary["wins"] + summary["losses"]) * 100

    rows = ""
    for p in positions[:20]:
        pnl = p.get("pnl", 0)
        pnl_cls = "positive" if pnl >= 0 else "negative"
        direction = p.get("side", "?")
        side_cls = "buy" if direction == "BUY" else "sell"
        title_full = str(p.get("title", "?"))
        title = title_full[:65]
        ev = p.get("ev", 0)
        confidence = p.get("confidence", 0)
        whale_align = p.get("whale_aligned", False)
        whale_badge = ' <span class="badge badge-conviction">🐋</span>' if whale_align else ""
        market_slug = p.get("polymarket_slug") or p.get("slug") or ""
        market_url = (
            f"https://polymarket.com/event/{market_slug}"
            if market_slug
            else f"https://polymarket.com/search?q={quote_plus(title_full)}"
        )

        rows += (
            f"<tr>"
            f"<td class='{side_cls}'>{direction}</td>"
            f"<td><a href='{market_url}' target='_blank' style='color:#64b5f6;text-decoration:none' "
            f"title='{title_full}'>{title}...</a>{whale_badge}</td>"
            f"<td>${p.get('value', 0):.2f}</td>"
            f"<td class='{pnl_cls}'>${pnl:.2f}</td>"
            f"<td class='{pnl_cls}'>{p.get('pnl_pct', 0):+.1f}%</td>"
            f"<td>{ev:+.2f}</td>"
            f"<td>{confidence*100:.0f}%</td>"
            f"</tr>"
        )

    if not rows:
        rows = '<tr><td colspan="7" style="color:#555;text-align:center">No open positions. Trades fire when EV > 5% and whale consensus aligns.</td></tr>'

    closed_rows = ""
    for p in closed[:5]:
        pnl = p.get("pnl", 0)
        pnl_cls = "positive" if pnl >= 0 else "negative"
        title_full = str(p.get('title', '?'))
        market_slug = p.get("polymarket_slug") or p.get("slug") or ""
        market_url = (
            f"https://polymarket.com/event/{market_slug}"
            if market_slug
            else f"https://polymarket.com/search?q={quote_plus(title_full)}"
        )
        closed_rows += (
            f"<tr><td><a href='{market_url}' target='_blank' style='color:#64b5f6;text-decoration:none' "
            f"title='{title_full}'>{title_full[:50]}...</a></td>"
            f"<td class='{pnl_cls}'>${pnl:.2f}</td>"
            f"<td>{p.get('closed_at','?')[:16]}</td></tr>"
        )

    main = f"""
        <div class="section">
            <h2>📄 Paper Portfolio — EV + Whale Overlay</h2>
            <div class="stats" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
                <div class="stat-card">
                    <div class="value" style="color:#00d4aa">${summary['bankroll']:.2f}</div>
                    <div class="label">Bankroll</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#64b5f6">${summary['exposure']:.2f}</div>
                    <div class="label">Exposure ({bankroll_pct:.0f}%)</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:{'#00d4aa' if summary['total_pnl'] >= 0 else '#ff6b6b'}">
                        {'+' if summary['total_pnl'] >= 0 else ''}${summary['total_pnl']:.2f}</div>
                    <div class="label">Unrealized PnL</div>
                </div>
                <div class="stat-card">
                    <div class="value">{summary['open_positions']}</div>
                    <div class="label">Open Positions</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:#888">{summary['total_trades']}</div>
                    <div class="label">Total Trades ({summary['wins']}W/{summary['losses']}L)</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:{'#00d4aa' if win_rate > 50 else '#ff6b6b'}">{win_rate:.0f}%</div>
                    <div class="label">Win Rate</div>
                </div>
            </div>

            <p style="color:#666;font-size:0.85em;margin-bottom:10px">
                Core: weather forecast EV (fair price vs market price).
                Overlay: 🐋 = whale confirmed (same direction). 2+ whales = consensus boost.
                Daily review logs learning insights; parameters are not auto-applied intraday.
            </p>

            <table>
                <tr><th>Side</th><th>Market</th><th>Size</th><th>PnL</th><th>Return</th><th>EV</th><th>Conf</th></tr>
                {rows}
            </table>
        </div>

        <div class="section">
            <h2>📋 Recent Closed Trades</h2>
            <table>
                <tr><th>Market</th><th>PnL</th><th>Closed</th></tr>
                {closed_rows if closed_rows else '<tr><td colspan="3" style="color:#555;text-align:center">No closed trades yet.</td></tr>'}
            </table>
        </div>
    """

    return HTML_TEMPLATE.format(
        whales="—", trades="—", volume=0, signals="—",
        today_trades="—", today_volume=0,
        positions=summary["open_positions"],
        high_conviction=summary["wins"],
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _render_whale_candidates() -> str:
    """Render whale candidates discovery page."""
    from database import get_whale_candidates
    candidates = get_whale_candidates()

    rows = ""
    for c in candidates:
        tier = c.get("tier", "NOISE")
        tier_color = "#00d4aa" if tier == "QUALITY" else "#ffd700" if tier == "POTENTIAL" else "#888"
        pnl = c.get("total_pnl", 0) or 0
        pnl_color = "#00d4aa" if pnl > 0 else "#ff6b6b"
        entry = c.get("avg_entry", 0.5) or 0.5
        entry_note = "⚠️ WASHER" if entry >= 0.85 else ""
        weather = c.get("weather_positions", 0) or 0
        label = c.get("label", "?")
        addr = c.get("address", "?")[:10]
        pm_link = f"https://polymarket.com/profile/{c.get('address', '')}"

        rows += (
            f"<tr>"
            f"<td><span style='color:{tier_color}'>{tier}</span></td>"
            f"<td>{label}</td>"
            f"<td style='font-family:mono;font-size:0.8em'>"
            f"<a href='{pm_link}' target='_blank' style='color:#64b5f6;text-decoration:none'>{addr}... &#8599;</a></td>"
            f"<td style='color:{pnl_color}'>${pnl:,.0f}</td>"
            f"<td>{c.get('trades', 0):,d}</td>"
            f"<td>${entry:.3f} {entry_note}</td>"
            f"<td>{c.get('entry_style', '?')}</td>"
            f"<td>{weather}</td>"
            f"<td>{c.get('score', 0):.3f}</td>"
            f"</tr>"
        )

    quality_count = sum(1 for c in candidates if c.get("tier") == "QUALITY")
    potential_count = sum(1 for c in candidates if c.get("tier") == "POTENTIAL")

    main = f"""
    <h2>🐋 Whale Candidates</h2>
    <div class="stats">
        <div class="stat-card">
            <div class="value">{quality_count}</div>
            <div class="label">Quality</div>
        </div>
        <div class="stat-card">
            <div class="value">{potential_count}</div>
            <div class="label">Potential</div>
        </div>
        <div class="stat-card">
            <div class="value">{len(candidates)}</div>
            <div class="label">Total Candidates</div>
        </div>
    </div>
    <p style="color:#666;margin-bottom:16px">
        From <code>discover_whales.py</code> — wallets with positive PnL and reasonable entry prices.
        Quality = scored &ge;0.6 with avg entry &lt;$0.85. Run <code>python3 discover_whales.py --add-quality</code>
        to add quality candidates to the watch list.
    </p>
    <table>
        <tr>
            <th>Tier</th><th>Label</th><th>Address</th><th>PnL</th>
            <th>Trades</th><th>Avg Entry</th><th>Style</th><th>Weather</th><th>Score</th>
        </tr>
        {rows}
    </table>
    """

    return HTML_TEMPLATE.format(
        whales="—", trades="—", volume=0, signals="—",
        today_trades="—", today_volume=0,
        positions="—", high_conviction="—",
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _position_net_pnl(pos: dict) -> float:
    exits = pos.get("exits", []) or []
    return float(pos.get("pnl", 0) or 0) + sum(float(e.get("pnl", 0) or 0) for e in exits if isinstance(e, dict))


def _render_city_performance_table(positions: list[dict]) -> str:
    city_stats = {}
    for pos in positions:
        city = pos.get("city") or "Unknown"
        stats = city_stats.setdefault(
            city,
            {"open": 0, "closed": 0, "wins": 0, "losses": 0, "flats": 0, "pnl": 0.0, "volume": 0.0},
        )
        pnl = _position_net_pnl(pos)
        stats["pnl"] += pnl
        stats["volume"] += float(pos.get("value", 0) or 0)
        if pos.get("status") == "open":
            stats["open"] += 1
        else:
            stats["closed"] += 1
            if pnl > 0.05:
                stats["wins"] += 1
            elif pnl < -0.05:
                stats["losses"] += 1
            else:
                stats["flats"] += 1

    rows = ""
    for city, stats in sorted(city_stats.items(), key=lambda item: item[1]["pnl"], reverse=True):
        decided = stats["wins"] + stats["losses"]
        win_rate = stats["wins"] / max(1, decided) * 100
        pnl_cls = "positive" if stats["pnl"] >= 0 else "negative"
        rows += (
            f"<tr><td>{city}</td>"
            f"<td>{stats['open']}</td>"
            f"<td>{stats['closed']}</td>"
            f"<td>{stats['wins']}/{stats['losses']}/{stats['flats']}</td>"
            f"<td>{win_rate:.0f}%</td>"
            f"<td>${stats['volume']:.2f}</td>"
            f"<td class='{pnl_cls}'>${stats['pnl']:+.2f}</td></tr>"
        )
    return rows


def _load_runtime_events(strategy: str, limit: int = 60) -> list[dict]:
    if not RUNTIME_LOG.exists():
        return []
    rows = []
    try:
        with RUNTIME_LOG.open() as f:
            for line in f:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("strategy") == strategy:
                    rows.append(event)
    except Exception:
        return []
    return rows[-limit:][::-1]


def _render_strategy_page(mode: str) -> str:
    trader = PaperTrader(mode=mode)
    summary = trader.summary()
    state = trader.state
    experiment = state.get("experiment", {})
    positions = list(state.get("positions", {}).values())
    open_positions = [p for p in positions if p.get("status") == "open"]
    closed_positions = [p for p in positions if p.get("status") == "closed"]
    closed_positions.sort(key=lambda p: p.get("closed_at", ""), reverse=True)

    open_value = sum(float(p.get("value", 0) or 0) for p in open_positions)
    realized_pnl = sum(_position_net_pnl(p) for p in closed_positions)
    unrealized_pnl = sum(_position_net_pnl(p) for p in open_positions)
    total_pnl = realized_pnl + unrealized_pnl
    starting_bankroll = float(summary.get("starting_bankroll", 100) or 100)
    ledger_equity = starting_bankroll + total_pnl
    cash_bankroll = float(summary.get("bankroll", 0) or 0)
    unique_markets = len({
        p.get("condition_id") or p.get("market_id") or p.get("event_slug")
        for p in positions
        if p.get("condition_id") or p.get("market_id") or p.get("event_slug")
    })
    wins = sum(1 for p in closed_positions if _position_net_pnl(p) > 0.05)
    losses = sum(1 for p in closed_positions if _position_net_pnl(p) < -0.05)
    flats = max(0, len(closed_positions) - wins - losses)
    win_rate = wins / max(1, wins + losses) * 100

    def position_row(p: dict, closed: bool = False) -> str:
        pnl = _position_net_pnl(p)
        pnl_cls = "positive" if pnl >= 0 else "negative"
        side_cls = "buy" if p.get("side") == "BUY" else "sell"
        ev_ratio = 0.0
        try:
            ev_ratio = float(p.get("fair_price", 0)) / max(0.0001, float(p.get("entry_price", 0)))
        except Exception:
            pass
        when = p.get("closed_at") if closed else p.get("entry_ts")
        reason = p.get("close_reason", "open" if not closed else "")
        return (
            f"<tr><td class='{side_cls}'>{p.get('side', '?')}</td>"
            f"<td>{p.get('city', '')}</td>"
            f"<td style='font-size:0.85em'>{p.get('title', '?')}</td>"
            f"<td>${float(p.get('entry_price', 0) or 0):.4f}</td>"
            f"<td>${float(p.get('current_price', 0) or 0):.4f}</td>"
            f"<td>{ev_ratio:.1f}x</td>"
            f"<td>${float(p.get('value', 0) or 0):.2f}</td>"
            f"<td class='{pnl_cls}'>${pnl:+.2f}</td>"
            f"<td>{reason}</td>"
            f"<td>{str(when or '')[:16]}</td></tr>"
        )

    open_rows = "".join(position_row(p) for p in sorted(open_positions, key=_position_net_pnl, reverse=True))
    closed_rows = "".join(position_row(p, closed=True) for p in closed_positions[:80])
    city_rows = _render_city_performance_table(positions)

    event_rows = ""
    for event in _load_runtime_events(mode):
        details = event.get("details", {})
        event_rows += (
            f"<tr><td>{str(event.get('ts', ''))[:19]}</td>"
            f"<td><span class='badge badge-signal'>{event.get('event_type', '')}</span></td>"
            f"<td>{event.get('message', '')}</td>"
            f"<td style='font-family:monospace;font-size:0.8em;color:#888'>{json.dumps(details, default=str)[:220]}</td></tr>"
        )

    last_cycle = state.get("last_strategy_cycle", {})
    last_monitor = state.get("last_monitor_cycle", {})
    stake_state = state.get("stake_state", {}) if mode == STRATEGY3_MODE else {}
    stake_cards = ""
    if stake_state:
        stake_cards = f"""
                <div class="stat-card"><div class="value">${float(stake_state.get('current_stake', 0) or 0):.2f}</div><div class="label">Next Stake</div></div>
                <div class="stat-card"><div class="value">${float(stake_state.get('max_stake', 0) or 0):.2f}</div><div class="label">Max Stake</div></div>
                <div class="stat-card"><div class="value">{float(stake_state.get('profit_reinvest_pct', 0) or 0) * 100:.0f}%</div><div class="label">Profit Reinvest</div><div class="sub">{stake_state.get('last_update_reason', 'initialized')}</div></div>
        """
    name = experiment.get("name", mode)
    rules = experiment.get("rules", "")
    main = f"""
        <div class="section">
            <h2>{name}</h2>
            <p style="color:#888;margin-bottom:16px">{rules}</p>
            <div class="stats">
                <div class="stat-card"><div class="value">${ledger_equity:.2f}</div><div class="label">Ledger Equity</div></div>
                <div class="stat-card"><div class="value" style="color:{'#00d4aa' if total_pnl >= 0 else '#ff6b6b'}">${total_pnl:+.2f}</div><div class="label">Total PnL</div></div>
                <div class="stat-card"><div class="value" style="color:{'#00d4aa' if realized_pnl >= 0 else '#ff6b6b'}">${realized_pnl:+.2f}</div><div class="label">Realized PnL</div></div>
                <div class="stat-card"><div class="value" style="color:{'#00d4aa' if unrealized_pnl >= 0 else '#ff6b6b'}">${unrealized_pnl:+.2f}</div><div class="label">Open PnL</div></div>
                <div class="stat-card"><div class="value">${cash_bankroll:.2f}</div><div class="label">Cash State</div><div class="sub">preserved repair value</div></div>
                <div class="stat-card"><div class="value">${open_value:.2f}</div><div class="label">Open Value</div></div>
                <div class="stat-card"><div class="value">{len(open_positions)}</div><div class="label">Open Positions</div></div>
                <div class="stat-card"><div class="value">{len(closed_positions)}</div><div class="label">Closed Positions</div></div>
                <div class="stat-card"><div class="value">{win_rate:.0f}%</div><div class="label">Closed Win Rate</div><div class="sub">{wins}W/{losses}L/{flats}F</div></div>
                <div class="stat-card"><div class="value">{unique_markets}</div><div class="label">Unique Markets</div></div>
                {stake_cards}
                <div class="stat-card"><div class="value">{last_cycle.get('opened', 0)}</div><div class="label">Last Cycle Opens</div><div class="sub">{str(last_cycle.get('ts', ''))[:16]}</div></div>
                <div class="stat-card"><div class="value">{last_monitor.get('closed', 0)}</div><div class="label">Last Monitor Closes</div><div class="sub">{str(last_monitor.get('ts', ''))[:16]}</div></div>
            </div>
        </div>

        <div class="section">
            <h2>Open Trades</h2>
            <table><tr><th>Side</th><th>City</th><th>Market</th><th>Entry</th><th>Now</th><th>Fair/Entry</th><th>Value</th><th>PnL</th><th>Reason</th><th>Opened</th></tr>
            {open_rows if open_rows else '<tr><td colspan="10" style="color:#555;text-align:center">No open trades.</td></tr>'}</table>
        </div>

        <div class="section">
            <h2>Closed History</h2>
            <table><tr><th>Side</th><th>City</th><th>Market</th><th>Entry</th><th>Exit</th><th>Fair/Entry</th><th>Value</th><th>PnL</th><th>Reason</th><th>Closed</th></tr>
            {closed_rows if closed_rows else '<tr><td colspan="10" style="color:#555;text-align:center">No closed trades.</td></tr>'}</table>
        </div>

        <div class="section">
            <h2>City Performance</h2>
            <table><tr><th>City</th><th>Open</th><th>Closed</th><th>W/L/F</th><th>Win Rate</th><th>Allocated</th><th>Total PnL</th></tr>
            {city_rows if city_rows else '<tr><td colspan="7" style="color:#555;text-align:center">No city data yet.</td></tr>'}</table>
        </div>

        <div class="section">
            <h2>Realtime Log</h2>
            <table><tr><th>Time</th><th>Type</th><th>Message</th><th>Details</th></tr>
            {event_rows if event_rows else '<tr><td colspan="4" style="color:#555;text-align:center">No runtime events yet.</td></tr>'}</table>
        </div>
    """
    return HTML_TEMPLATE.format(
        whales="—", trades=summary.get("total_trades", 0), volume=0, signals="—",
        today_trades="—", today_volume=0,
        positions=len(open_positions), high_conviction=wins,
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _render_live_strategy2_page() -> str:
    state = load_live_state()
    config = state.get("config", {})
    positions = list(state.get("positions", {}).values())
    open_positions = [p for p in positions if p.get("status") == "dry_run_open"]
    closed_positions = [p for p in positions if p.get("status") == "closed"]
    last_cycle = state.get("last_cycle", {})
    spent = float(state.get("spent", 0) or 0)
    bankroll_limit = float(config.get("bankroll_limit", 20) or 20)
    remaining = max(0.0, bankroll_limit - spent)
    closed_trades = int(state.get("closed_trades", 0) or 0)
    max_trades = int(config.get("max_trades", 20) or 20)
    daily_pnl = state.get("daily_realized_pnl", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_realized = float(daily_pnl.get(today, 0) or 0)
    daily_max = float(config.get("daily_max_loss", 5) or 5)
    daily_remaining = max(0.0, daily_max - abs(min(0, today_realized)))

    exit_stats = last_cycle.get("exits", {}) if isinstance(last_cycle, dict) else {}

    def position_row(p: dict) -> str:
        pnl = float(p.get("pnl", 0) or 0)
        pnl_cls = "positive" if pnl >= 0 else "negative"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        entry = float(p.get("entry_price", 0) or 0)
        current = float(p.get("current_price", entry) or entry)
        peak = p.get("peak_pnl_pct")
        peak_str = f" peak={peak:.0f}%" if peak is not None else ""
        return (
            f"<tr><td>{p.get('city', '')}</td>"
            f"<td style='font-size:0.85em'>{p.get('title', '?')[:55]}</td>"
            f"<td>{entry:.4f}</td>"
            f"<td>{current:.4f}</td>"
            f"<td>{float(p.get('ev_ratio', 0) or 0):.1f}x</td>"
            f"<td>{float(p.get('forecast_gap', 0) or 0):.1f}°F</td>"
            f"<td>${float(p.get('stake', 0) or 0):.2f}</td>"
            f"<td class='{pnl_cls}'>{pnl_str}</td>"
            f"<td style='font-size:0.8em;color:#555'>{peak_str}</td>"
            f"<td>{str(p.get('opened_at', ''))[:16]}</td></tr>"
        )

    open_rows = "".join(
        position_row(p) for p in sorted(open_positions, key=lambda x: x.get("opened_at", ""), reverse=True)
    )

    def closed_row(p: dict) -> str:
        pnl = float(p.get("pnl", 0) or 0)
        pnl_cls = "positive" if pnl >= 0 else "negative"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        return (
            f"<tr><td>{p.get('city', '')}</td>"
            f"<td style='font-size:0.85em'>{p.get('title', '?')[:55]}</td>"
            f"<td>${float(p.get('entry_price', 0) or 0):.4f}</td>"
            f"<td>${float(p.get('exit_price', 0) or 0):.4f}</td>"
            f"<td class='{pnl_cls}'>{pnl_str}</td>"
            f"<td>{p.get('close_reason', '?')}</td>"
            f"<td>{str(p.get('closed_at', ''))[:16]}</td></tr>"
        )

    closed_rows = "".join(
        closed_row(p) for p in sorted(closed_positions, key=lambda x: x.get("closed_at", ""), reverse=True)
    )

    event_rows = ""
    for event in load_live_events():
        details = event.get("details", {})
        event_rows += (
            f"<tr><td>{str(event.get('ts', ''))[:19]}</td>"
            f"<td><span class='badge badge-signal'>{event.get('event_type', '')}</span></td>"
            f"<td>{event.get('message', '')}</td>"
            f"<td style='font-family:monospace;font-size:0.8em;color:#888'>{json.dumps(details, default=str)[:260]}</td></tr>"
        )

    skip_reasons = last_cycle.get("skip_reasons", {}) if isinstance(last_cycle, dict) else {}
    skip_rows = "".join(
        f"<tr><td>{reason}</td><td>{count}</td></tr>"
        for reason, count in sorted(skip_reasons.items(), key=lambda item: item[1], reverse=True)
    )
    scan_by_city = last_cycle.get("scan_by_city", {}) if isinstance(last_cycle, dict) else {}
    city_scan_rows = ""
    for city, stats in sorted(scan_by_city.items(), key=lambda item: (item[1].get("would_buy", 0), item[1].get("candidates", 0), item[1].get("scanned", 0)), reverse=True):
        skips = stats.get("skips", {}) if isinstance(stats.get("skips", {}), dict) else {}
        top_skip = ""
        if skips:
            top_skip = max(skips.items(), key=lambda item: item[1])[0]
        city_scan_rows += (
            f"<tr><td>{city}</td>"
            f"<td>{stats.get('scanned', 0)}</td>"
            f"<td>{stats.get('candidates', 0)}</td>"
            f"<td>{stats.get('would_buy', 0)}</td>"
            f"<td>{stats.get('blocked', 0)}</td>"
            f"<td>{top_skip}</td></tr>"
        )

    mode_label = "DRY RUN" if config.get("dry_run", True) else "LIVE"
    main = f"""
        <div class="section">
            <h2>Live Strategy 2 Test</h2>
            <p style="color:#888;margin-bottom:16px">Strategy 2 tail buckets, $1 stake, max 10 positions, ${daily_max:.0f} daily loss stop, {max_trades} trade limit.</p>
            <div class="stats">
                <div class="stat-card"><div class="value">{mode_label}</div><div class="label">Mode</div></div>
                <div class="stat-card"><div class="value">${bankroll_limit:.2f}</div><div class="label">Bankroll Cap</div></div>
                <div class="stat-card"><div class="value">${float(config.get('stake', 1) or 1):.2f}</div><div class="label">Stake Per Trade</div></div>
                <div class="stat-card"><div class="value">{len(open_positions)}/{int(config.get('max_open_positions', 10) or 10)}</div><div class="label">Open Positions</div></div>
                <div class="stat-card"><div class="value">${spent:.2f}</div><div class="label">Allocated</div></div>
                <div class="stat-card"><div class="value">${remaining:.2f}</div><div class="label">Remaining Cap</div></div>
                <div class="stat-card"><div class="value" style="color:{'#ff6b6b' if today_realized < 0 else '#00d4aa'}">${today_realized:+.2f}</div><div class="label">Today PnL</div><div class="sub">limit ${daily_max:.2f}, ${daily_remaining:.2f} remaining</div></div>
                <div class="stat-card"><div class="value">{closed_trades}/{max_trades}</div><div class="label">Closed Trades</div></div>
                <div class="stat-card"><div class="value">{exit_stats.get('closed', 0)}</div><div class="label">Last Cycle Exits</div><div class="sub">{exit_stats.get('resolved', 0)} resolved, {exit_stats.get('trailing_stops', 0)} trailing</div></div>
                <div class="stat-card"><div class="value">{last_cycle.get('would_buy', 0)}</div><div class="label">Last Cycle Opens</div><div class="sub">{str(last_cycle.get('ts', ''))[:16]}</div></div>
                <div class="stat-card"><div class="value">{last_cycle.get('candidates', 0)}</div><div class="label">Last Cycle Candidates</div></div>
            </div>
        </div>

        <div class="section">
            <h2>Open Positions ({len(open_positions)})</h2>
            <table><tr><th>City</th><th>Market</th><th>Entry</th><th>Now</th><th>Fair/Entry</th><th>Gap</th><th>Stake</th><th>PnL</th><th>Peak</th><th>Opened</th></tr>
            {open_rows if open_rows else '<tr><td colspan="10" style="color:#555;text-align:center">No open positions.</td></tr>'}</table>
        </div>

        <div class="section">
            <h2>Closed Positions ({len(closed_positions)})</h2>
            <table><tr><th>City</th><th>Market</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th><th>Closed</th></tr>
            {closed_rows if closed_rows else '<tr><td colspan="7" style="color:#555;text-align:center">No closed positions yet.</td></tr>'}</table>
        </div>

        <div class="section">
            <h2>Last Scan Skips</h2>
            <table><tr><th>Reason</th><th>Count</th></tr>
            {skip_rows if skip_rows else '<tr><td colspan="2" style="color:#555;text-align:center">No scan yet.</td></tr>'}</table>
        </div>

        <div class="section">
            <h2>Scan By City</h2>
            <table><tr><th>City</th><th>Scanned</th><th>Candidates</th><th>WOULD_BUY</th><th>Blocked</th><th>Top Skip</th></tr>
            {city_scan_rows if city_scan_rows else '<tr><td colspan="6" style="color:#555;text-align:center">No city scan data yet.</td></tr>'}</table>
        </div>

        <div class="section">
            <h2>Live Test Log</h2>
            <table><tr><th>Time</th><th>Type</th><th>Message</th><th>Details</th></tr>
            {event_rows if event_rows else '<tr><td colspan="4" style="color:#555;text-align:center">No live test events yet.</td></tr>'}</table>
        </div>
    """
    return HTML_TEMPLATE.format(
        whales="—", trades=state.get("closed_trades", 0), volume=spent, signals=last_cycle.get("candidates", 0),
        today_trades="—", today_volume=0,
        positions=len(open_positions), high_conviction=last_cycle.get("would_buy", 0),
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _render_strategy_report_page() -> str:
    path = Path(__file__).parent / "reports" / "strategy_evolution_2026-05-24.md"
    content = path.read_text() if path.exists() else "Strategy report has not been generated yet."
    main = f"""
        <div class="section">
            <h2>Strategy Evolution Report</h2>
            <div class="report-block">{content}</div>
        </div>
    """
    return HTML_TEMPLATE.format(
        whales="—", trades="—", volume=0, signals="—",
        today_trades="—", today_volume=0,
        positions="—", high_conviction="—",
        main_content=main,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    view = request.query_params.get("view", "dashboard")

    if view == "paper":
        return _render_paper_portfolio()
    elif view == "strategy1":
        return _render_strategy_page(STRATEGY1_MODE)
    elif view == "strategy2":
        return _render_strategy_page(STRATEGY2_MODE)
    elif view == "live-strategy2":
        return _render_live_strategy2_page()
    elif view == "strategy3":
        return _render_strategy_page(STRATEGY3_MODE)
    elif view == "strategy-report":
        return _render_strategy_report_page()
    elif view == "candidates":
        return _render_whale_candidates()
    else:
        return _render_dashboard()


@app.get("/api/stats")
async def api_stats():
    stats = get_stats()
    return stats


def start_dashboard(host: str = "0.0.0.0", port: int = 9091):
    """Start the dashboard server (blocking)."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
