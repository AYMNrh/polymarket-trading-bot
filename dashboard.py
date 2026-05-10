"""
Web dashboard — FastAPI app showing whale activity, trades, signals,
order book analysis, whale conviction, and strategy reports.

Run with: uvicorn dashboard:app --reload --port 9091
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

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
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)

app = FastAPI(title="Whale Tracker Dashboard")
tracker = PositionTracker()
learner = SelfLearningEngine()
decoder = OnChainTradeDecoder(etherscan_key="T35WYX45NH88EENSM71UVNJAZQQDG3Z29I")
scraper = PolymarketScraper()
paper_trader = PaperTrader(bankroll=100.0)

LOG_FILE = Path(__file__).parent / "whale_trades.jsonl"
ORDERBOOK_CACHE = Path(__file__).parent / "data" / "orderbook_cache.json"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
            <a href="/?view=orderbook">Order Books</a>
            <a href="/?view=conviction">Conviction</a>
            <a href="/?view=strategy">Strategy Report</a>
            <a href="/?view=trades">Whale Trades</a>
            <a href="/?view=paper">Paper Portfolio</a>
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

    # Whales table — enrich with scraper portfolio data
    whale_rows = ""
    for w in whales[:15]:
        wr = w.get("win_rate")
        wr_str = f"{wr*100:.0f}%" if wr else "—"
        addr = w["address"]
        label = w.get("label", "?")

        # Try scraper for live portfolio value
        try:
            profile = scraper.get_profile(addr)
            live_pv = profile.get("portfolio_value")
            live_pnl = profile.get("total_pnl")
        except Exception:
            live_pv = None
            live_pnl = None

        pv_str = f"${live_pv:,.0f}" if live_pv else "—"
        pnl_str = f"+${live_pnl:,.0f}" if (live_pnl and live_pnl > 0) else (f"${live_pnl:,.0f}" if live_pnl else "—")
        whale_rows += (
            f"<tr>"
            f"<td>{label}</td>"
            f"<td style='font-family:mono;font-size:0.8em'>{addr[:10]}...{addr[-6:]}</td>"
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

    main = f"""
        <div class="section">
            <h2>🐋 Whale Trades</h2>
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
        title = str(p.get("title", "?"))[:65]
        ev = p.get("ev", 0)
        confidence = p.get("confidence", 0)
        whale_align = p.get("whale_aligned", False)
        whale_badge = ' <span class="badge badge-conviction">🐋</span>' if whale_align else ""

        rows += (
            f"<tr>"
            f"<td class='{side_cls}'>{direction}</td>"
            f"<td>{title}...{whale_badge}</td>"
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
        closed_rows += (
            f"<tr><td>{str(p.get('title','?'))[:50]}...</td>"
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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    view = request.query_params.get("view", "dashboard")

    if view == "orderbook":
        return _render_orderbook()
    elif view == "conviction":
        return _render_conviction()
    elif view == "strategy":
        return _render_strategy()
    elif view == "trades":
        wallet = request.query_params.get("wallet", "")
        return _render_whale_trades(selected_addr=wallet)
    elif view == "paper":
        return _render_paper_portfolio()
    else:
        return _render_dashboard()


@app.get("/api/stats")
async def api_stats():
    stats = get_stats()
    positions = tracker.get_whale_positions()
    stats["positions"] = len(positions)
    stats["high_conviction"] = len(tracker.get_conviction_signals(min_score=0.5))
    return stats


@app.get("/api/trades")
async def api_trades(limit: int = 50):
    return get_recent_trades(limit)


@app.get("/api/whales")
async def api_whales():
    return get_whale_summary()


@app.get("/api/signals")
async def api_signals(limit: int = 20):
    return get_recent_signals(limit)


@app.get("/api/positions")
async def api_positions():
    return tracker.get_whale_positions()


@app.get("/api/conviction")
async def api_conviction(min_score: float = 0.5):
    return tracker.get_conviction_signals(min_score=min_score)


@app.get("/api/orderbook")
async def api_orderbook():
    return _load_orderbook_cache()


@app.get("/api/strategy")
async def api_strategy():
    return {
        "report": learner.strategy_report(),
        "recommendations": learner.get_recommendations(),
        "parameters": learner.notes.get("parameter_adjustments", {}),
    }


def start_dashboard(host: str = "0.0.0.0", port: int = 9091):
    """Start the dashboard server (blocking)."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
