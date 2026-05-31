"""Simplified trading dashboard.

Only the production-facing surfaces remain:
  - BTC 5-minute Up/Down straddle
  - multi-outcome arbitrage
  - wallet copy candidates
"""

from __future__ import annotations

import html
import json
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from arb_bot import load_state as load_arb_state
from btc_5m_bot import load_state as load_btc_state, LOOKAHEAD_SECONDS as BTC_LOOKAHEAD
from btc_eth_15m_bot import load_state as load_15m_state, LAST_N_SECONDS
from wallet_tracker import load_state as load_wallet_state

app = FastAPI(title="Polymarket Live Trading")


CSS = """
*{box-sizing:border-box}body{margin:0;background:#090b0f;color:#e8edf2;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:1500px;margin:0 auto;padding:22px}.nav{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 20px}
.nav a{color:#aab4c0;text-decoration:none;background:#131821;border:1px solid #222b39;border-radius:6px;padding:8px 12px}
.nav a:hover{color:#fff;border-color:#3e536e}h1{font-size:24px;margin:0}h2{font-size:17px;color:#93a4b8;margin:22px 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card{background:#121722;border:1px solid #222b39;border-radius:8px;padding:14px}
.value{font-size:24px;font-weight:700}.label{font-size:12px;color:#778596;margin-top:4px}.sub{font-size:12px;color:#667384;margin-top:4px}
table{width:100%;border-collapse:collapse;background:#121722;border-radius:8px;overflow:hidden}th{background:#18202c;color:#7f8da0;font-size:12px;text-align:left;padding:9px}
td{padding:9px;border-top:1px solid #222b39;font-size:13px;vertical-align:top}.pos{color:#20d19b}.neg{color:#ff6b6b}.muted{color:#788698}.warn{color:#f3c969}
.pill{display:inline-block;border-radius:4px;padding:2px 6px;background:#1d2633;color:#9db2ca;font-size:12px}.error{border-color:#67313a;background:#211116;color:#ff9ba9}
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "$0.00"


def page(title: str, body: str) -> HTMLResponse:
    nav = """
    <div class="nav">
      <a href="/">BTC 5m</a>
      <a href="/?view=15m">15m BTC/ETH</a>
      <a href="/?view=arbitrage">Arbitrage</a>
      <a href="/?view=wallets">Wallets</a>
      <a href="/?view=status">Status</a>
    </div>
    """
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <meta http-equiv="refresh" content="20"><title>{esc(title)}</title><style>{CSS}</style></head>
    <body><div class="wrap"><h1>{esc(title)}</h1>{nav}{body}<div class="muted" style="margin-top:24px;text-align:center">Updated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div></div></body></html>""")


def stat_cards(items: list[tuple[str, str, str | None, str | None]]) -> str:
    cards = ""
    for label, value, cls, sub in items:
        cards += f"<div class='card'><div class='value {cls or ''}'>{esc(value)}</div><div class='label'>{esc(label)}</div>"
        if sub:
            cards += f"<div class='sub'>{esc(sub)}</div>"
        cards += "</div>"
    return f"<div class='grid'>{cards}</div>"


def render_btc() -> HTMLResponse:
    state = load_btc_state()
    trades = state.get("trades", [])
    last_signal = state.get("last_signal", {})

    body = stat_cards([
        ("Bankroll", money(state.get("bankroll", 100)), None, "starts at $100"),
        ("Equity", money(state.get("equity", 100)), None, None),
        ("Total PnL", f"{float(state.get('total_pnl', 0) or 0):+.2f}", "pos" if float(state.get("total_pnl", 0) or 0) >= 0 else "neg", None),
        ("Trades", str(state.get("total_trades", 0)), None, f"{state.get('win_count',0)}W/{state.get('loss_count',0)}L"),
        ("Open Trades", str(len([t for t in trades if t.get('status')=='open'])), None, None),
        ("Last Scan", esc((state.get("last_scan") or "")[:19]), None, None),
    ])
    if state.get("last_error"):
        body += f"<div class='card error' style='margin-top:12px'>Error: {esc(state['last_error'])}</div>"

    # Last signal
    body += "<h2>Last Signal</h2>"
    if last_signal:
        if last_signal.get("executed"):
            body += f"<div class='card pos'>BUY {esc(last_signal['winner'])} @ ${last_signal['buy_price']} ({last_signal['profit_pct']}% profit) - {last_signal['seconds_left']}s left</div>"
        else:
            up_ask = last_signal.get('up_ask')
            down_ask = last_signal.get('down_ask')
            up_str = f"${up_ask:.2f}" if up_ask is not None else "?"
            down_str = f"${down_ask:.2f}" if down_ask is not None else "?"
            body += f"<div class='card warn'>Waiting: {esc(last_signal.get('reason','?'))} (UP {up_str} DOWN {down_str})</div>"
    else:
        body += "<div class='card muted'>No signal yet – waiting for windows near resolution.</div>"

    # Open trades
    open_trades = [t for t in trades if t.get("status") == "open"]
    body += "<h2>Open Trades</h2><table><tr><th>Window</th><th>Side</th><th>Buy Price</th><th>Shares</th><th>Cost</th><th>Expected PnL</th><th>Entered</th></tr>"
    for t in open_trades:
        body += (
            f"<tr><td>{esc(t.get('question','')[:50])}</td>"
            f"<td class='pos'>{t.get('winner')}</td>"
            f"<td>${t.get('buy_price')}</td>"
            f"<td>{t.get('shares')}</td>"
            f"<td>{money(t.get('cost'))}</td>"
            f"<td class='pos'>{money(t.get('expected_profit'))}</td>"
            f"<td>{esc((t.get('entry_time') or '')[:19])}</td></tr>"
        )
    if not open_trades:
        body += "<tr><td colspan='7' class='muted'>No open trades.</td></tr>"
    body += "</table>"

    # Trade history
    closed_trades = [t for t in trades if t.get("status") == "closed"]
    if closed_trades:
        body += "<h2>Recent Trades</h2><table><tr><th>Window</th><th>Side</th><th>Buy</th><th>PnL</th><th>Closed</th></tr>"
        for t in list(closed_trades)[-10:][::-1]:
            pnl = float(t.get("actual_pnl", 0) or 0)
            body += (
                f"<tr><td>{esc(t.get('question','')[:50])}</td>"
                f"<td>{t.get('winner')}</td>"
                f"<td>${t.get('buy_price')}</td>"
                f"<td class='{'pos' if pnl>=0 else 'neg'}'>{money(pnl)}</td>"
                f"<td>{esc((t.get('exit_time') or '')[:19])}</td></tr>"
            )
        body += "</table>"

    # Upcoming windows
    windows = state.get("active_windows_summary", [])
    body += "<h2>Upcoming 5m Windows</h2><table><tr><th>Window</th><th>Time Left</th><th>Volume</th><th>UP Ask</th><th>DOWN Ask</th></tr>"
    for w in windows[:20]:
        secs = w.get("seconds_left", 0)
        mins = secs // 60
        secs_rem = secs % 60
        time_str = f"{mins}m {secs_rem}s"
        is_near = secs <= BTC_LOOKAHEAD
        row_cls = " class='pos'" if is_near else ""
        body += (
            f"<tr{row_cls}>"
            f"<td>{esc(w.get('question','')[:50])}</td>"
            f"<td>{time_str}</td>"
            f"<td>{money(w.get('volume'))}</td>"
            f"<td>${w.get('up_ask','?'):.2f}</td>"
            f"<td>${w.get('down_ask','?'):.2f}</td></tr>"
        )
    body += "</table>"

    return page("BTC 5m Buy-Winner-Late", body)


def render_arbitrage() -> HTMLResponse:
    state = load_arb_state()
    opps = state.get("opportunities", [])
    open_positions = [p for p in state.get("positions", []) if p.get("status") == "open"]
    body = stat_cards([
        ("Bankroll", money(state.get("bankroll", 100)), None, None),
        ("Equity", money(state.get("equity", 100)), None, None),
        ("Open Arbs", str(len(open_positions)), None, None),
        ("Opportunities", str(len(opps)), None, "analytics source only"),
        ("Total PnL", f"{float(state.get('total_pnl',0) or 0):+.2f}", "pos" if float(state.get("total_pnl",0) or 0) >= 0 else "neg", None),
        ("Last Scan", esc((state.get("last_scan") or "")[:19]), None, None),
    ])
    if state.get("last_error"):
        body += f"<div class='card error' style='margin-top:12px'>Source/error: {esc(state['last_error'])}</div>"
    body += "<h2>Open Arbitrage Positions</h2><table><tr><th>Event</th><th>Outcomes</th><th>Cost</th><th>Expected Profit</th><th>Return</th><th>Entered</th></tr>"
    for p in open_positions:
        body += f"<tr><td>{esc(p.get('title'))}</td><td>{p.get('outcomes')}</td><td>{money(p.get('cost'))}</td><td class='pos'>{money(p.get('expected_profit'))}</td><td>{p.get('arb_pct')}%</td><td>{esc((p.get('entry_time') or '')[:19])}</td></tr>"
    if not open_positions:
        body += "<tr><td colspan='6' class='muted'>No open arbitrage positions.</td></tr>"
    body += "</table><h2>Current Opportunities</h2><table><tr><th>Arb</th><th>Event</th><th>Outcomes</th><th>YES Sum</th><th>Volume</th><th>Top Prices</th></tr>"
    for r in opps[:40]:
        badges = " ".join(f"<span class='pill'>{d.get('price')}</span>" for d in r.get("outcome_details", [])[:6])
        body += f"<tr><td class='pos'>{r.get('arb_pct')}%</td><td>{esc(r.get('title'))}</td><td>{r.get('outcomes')}</td><td>{r.get('yes_sum')}</td><td>{money(r.get('total_volume'))}</td><td>{badges}</td></tr>"
    if not opps:
        body += "<tr><td colspan='6' class='muted'>No analytics arbitrage opportunities.</td></tr>"
    body += "</table>"
    return page("Multi-Outcome Arbitrage", body)


def render_wallets() -> HTMLResponse:
    state = load_wallet_state()
    signals = state.get("signals", [])
    body = stat_cards([
        ("Tracked Wallets", str(len(state.get("wallets", []))), None, "edit data/watch_wallets.json"),
        ("Low Entry Signals", str(len(signals)), None, "price <= 0.15, small size"),
        ("Last Scan", esc((state.get("last_scan") or "")[:19]), None, None),
    ])
    if state.get("last_error"):
        body += f"<div class='card error' style='margin-top:12px'>Source/error: {esc(state['last_error'])}</div>"
    if not state.get("wallets"):
        body += "<div class='card warn' style='margin-top:12px'>No wallets configured yet. Add addresses to data/watch_wallets.json.</div>"
    body += "<h2>Copy Candidates</h2><table><tr><th>Wallet</th><th>Market</th><th>Outcome</th><th>Price</th><th>Size</th><th>Time</th></tr>"
    for s in signals:
        body += f"<tr><td>{esc(s.get('wallet'))[:12]}...</td><td>{esc(s.get('market'))}</td><td>{esc(s.get('outcome'))}</td><td>{s.get('price')}</td><td>{money(s.get('size'))}</td><td>{esc((s.get('timestamp') or '')[:19])}</td></tr>"
    if not signals:
        body += "<tr><td colspan='6' class='muted'>No low-entry copy signals.</td></tr>"
    body += "</table>"
    return page("Wallet Copy Tracker", body)


def render_15m() -> HTMLResponse:
    state = load_15m_state()
    windows = state.get("active_windows_summary", [])
    trades = state.get("trades", [])
    last_signal = state.get("last_signal", {})

    body = stat_cards([
        ("Bankroll", money(state.get("bankroll", 100)), None, "starts at $100"),
        ("Equity", money(state.get("equity", 100)), None, None),
        ("Total PnL", f"{float(state.get('total_pnl',0) or 0):+.2f}", "pos" if float(state.get("total_pnl",0) or 0) >= 0 else "neg", None),
        ("Trades", str(state.get("total_trades", 0)), None, f"{state.get('win_count',0)}W/{state.get('loss_count',0)}L"),
        ("Open Trades", str(len([t for t in trades if t.get('status')=='open'])), None, None),
        ("Last Scan", esc((state.get("last_scan") or "")[:19]), None, None),
    ])
    if state.get("last_error"):
        body += f"<div class='card error' style='margin-top:12px'>Error: {esc(state['last_error'])}</div>"

    # Last signal
    body += "<h2>Last Signal</h2>"
    if last_signal:
        if last_signal.get("executed"):
            body += f"<div class='card pos'>BUY {esc(last_signal['winner'])} @ ${last_signal['buy_price']} ({last_signal['profit_pct']}% profit) - {last_signal['seconds_left']}s left</div>"
        else:
            up_ask = last_signal.get('up_ask')
            down_ask = last_signal.get('down_ask')
            up_str = f"${up_ask:.2f}" if up_ask is not None else "?"
            down_str = f"${down_ask:.2f}" if down_ask is not None else "?"
            body += f"<div class='card warn'>Waiting: {esc(last_signal.get('reason','?'))} (UP {up_str} DOWN {down_str})</div>"
    else:
        body += "<div class='card muted'>No signal yet – waiting for windows near resolution.</div>"

    # Open trades
    open_trades = [t for t in trades if t.get("status") == "open"]
    body += "<h2>Open Trades</h2><table><tr><th>Window</th><th>Side</th><th>Buy Price</th><th>Shares</th><th>Cost</th><th>Expected PnL</th><th>Entered</th></tr>"
    for t in open_trades:
        body += (
            f"<tr><td>{esc(t.get('question','')[:50])}</td>"
            f"<td class='pos'>{t.get('winner')}</td>"
            f"<td>${t.get('buy_price')}</td>"
            f"<td>{t.get('shares')}</td>"
            f"<td>{money(t.get('cost'))}</td>"
            f"<td class='pos'>{money(t.get('expected_profit'))}</td>"
            f"<td>{esc((t.get('entry_time') or '')[:19])}</td></tr>"
        )
    if not open_trades:
        body += "<tr><td colspan='7' class='muted'>No open trades.</td></tr>"
    body += "</table>"

    # Trade history
    closed_trades = [t for t in trades if t.get("status") == "closed"]
    if closed_trades:
        body += "<h2>Recent Trades</h2><table><tr><th>Window</th><th>Side</th><th>Buy</th><th>PnL</th><th>Closed</th></tr>"
        for t in list(closed_trades)[-10:][::-1]:
            pnl = float(t.get("actual_pnl", 0) or 0)
            body += (
                f"<tr><td>{esc(t.get('question','')[:50])}</td>"
                f"<td>{t.get('winner')}</td>"
                f"<td>${t.get('buy_price')}</td>"
                f"<td class='{'pos' if pnl>=0 else 'neg'}'>{money(pnl)}</td>"
                f"<td>{esc((t.get('exit_time') or '')[:19])}</td></tr>"
            )
        body += "</table>"

    # Upcoming windows
    body += "<h2>Upcoming 15m Windows</h2><table><tr><th>Window</th><th>Time Left</th><th>Volume</th><th>UP Ask</th><th>DOWN Ask</th></tr>"
    for w in windows[:20]:
        secs = w.get("seconds_left", 0)
        mins = secs // 60
        secs_rem = secs % 60
        time_str = f"{mins}m {secs_rem}s"
        is_near = secs <= LAST_N_SECONDS
        row_cls = " class='pos'" if is_near else ""
        body += (
            f"<tr{row_cls}>"
            f"<td>{esc(w.get('question','')[:50])}</td>"
            f"<td>{time_str}</td>"
            f"<td>{money(w.get('volume'))}</td>"
            f"<td>${w.get('up_ask','?'):.2f}</td>"
            f"<td>${w.get('down_ask','?'):.2f}</td></tr>"
        )
    body += "</table>"

    return page("15m BTC/ETH Buy-Winner-Late", body)


def render_status() -> HTMLResponse:
    btc = load_btc_state()
    arb = load_arb_state()
    wallets = load_wallet_state()
    body = "<h2>System State</h2><pre class='card'>" + esc(json.dumps({
        "btc": {"last_scan": btc.get("last_scan"), "last_error": btc.get("last_error"), "live_enabled": btc.get("live_enabled")},
        "arbitrage": {"last_scan": arb.get("last_scan"), "last_error": arb.get("last_error"), "opportunities": len(arb.get("opportunities", []))},
        "wallets": {"last_scan": wallets.get("last_scan"), "last_error": wallets.get("last_error"), "wallet_count": len(wallets.get("wallets", []))},
    }, indent=2)) + "</pre>"
    return page("Status", body)


@app.get("/", response_class=HTMLResponse)
async def root(view: str = "btc"):
    if view == "arbitrage":
        return render_arbitrage()
    if view == "wallets":
        return render_wallets()
    if view == "status":
        return render_status()
    if view == "15m":
        return render_15m()
    return render_btc()


@app.get("/api/state")
async def api_state():
    return {
        "btc": load_btc_state(),
        "arbitrage": load_arb_state(),
        "wallets": load_wallet_state(),
    }


def start_dashboard(host: str = "0.0.0.0", port: int = 9091):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
