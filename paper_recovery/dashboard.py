"""Read-only dashboard. Trading runs in an explicit background worker."""
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse


def esc(value):
    return html.escape(str(value if value is not None else ''))


def cash(value):
    return 'Unavailable' if value is None else f'${value:,.2f}'


def date(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if value else 'Never'


CSS = '''
*{box-sizing:border-box}body{margin:0;background:#090b0f;color:#e8edf2;font-family:Segoe UI,sans-serif}
.wrap{max-width:1350px;margin:auto;padding:24px}h1{font-size:26px}h2{font-size:18px;margin-top:30px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card,.banner{padding:16px;background:#121722;border:1px solid #334051;border-radius:8px;margin-bottom:16px}
.value{font-size:25px}.muted{color:#94a2b5}.bad{color:#ff9595}.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #283343;font-size:13px}
th{color:#9eb0c7}a{color:#77cfff}nav{display:flex;gap:20px;margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:600px){.wrap{padding:14px}.value{font-size:21px}nav{flex-wrap:wrap}}
'''


def create_app(store, lifespan=None, root=None):
    app = FastAPI(title='Paper Recovery', lifespan=lifespan)

    @app.get('/api/state')
    def state():
        return store.snapshot()

    @app.get('/healthz')
    def health():
        s = store.snapshot()
        return JSONResponse({'status': s['status'], 'last_success': s['last_success'],
                             'accounting_ok': s['accounting_ok'], 'errors': s['report'].get('errors', [])},
                            status_code=200 if s['healthy'] else 503)

    @app.get('/', response_class=HTMLResponse)
    def index(view: str = 'paper'):
        s = store.snapshot()
        body = f'<div class="banner"><b>PAPER ONLY</b> &middot; {esc(s["status"])} &middot; US weather complete-set research<br>'
        body += f'<span class="muted">Last successful scan: {date(s["last_success"])}. Last attempt: {date(s["last_scan"])}.</span></div>'
        body += '<div class="cards">'
        for label, value in [('Cash', cash(s['cash'])), ('Open cost', cash(s['open_cost'])),
                             ('Liquidation equity', cash(s['equity'])), ('Realized PnL', cash(s['realized_pnl'])),
                             ('Basket cap', cash(s['parameters']['basket_cap']))]:
            body += f'<div class="card"><div class="value">{esc(value)}</div><div class="muted">{label}</div></div>'
        body += '</div>'
        for error in s['report'].get('errors', []):
            body += f'<div class="banner bad">{esc(error)}</div>'
        if view == 'trades':
            whales = s['whales']
            body += f'<h2>Whale activity</h2><p>{whales.get("watched_wallets",0)} watched wallets. Last successful poll: {date(whales.get("last_success"))}.</p>'
            body += '<p class="muted">Public activity is context only. Initial history is capped at 100 trades per wallet.</p>'
            body += '<div class="scroll"><table><tr><th>Time</th><th>Wallet</th><th>Market</th><th>Side</th><th>Outcome</th><th>Price</th><th>Shares</th></tr>'
            for t in s['activity']:
                body += '<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in [date(t.get('timestamp')), t.get('wallet_label'),
                         t.get('title'), t.get('side'), t.get('outcome'), t.get('price'), t.get('size')]) + '</tr>'
            body += '</table></div><pre class="card">' + esc(json.dumps(whales, indent=2)) + '</pre>'
        elif view == 'history':
            body += '<h2>Earlier experiments</h2><p>Preserved historical records. These balances belong to different experiments and are not the new ledger.</p>'
            for name in ('paper_portfolio.json', 'btc_5m_state.json', 'btc_eth_15m_state.json', 'arb_bot_state.json'):
                path = Path(root) / 'data' / name if root else None
                if path and path.exists():
                    try:
                        old = json.loads(path.read_text())
                        fields = {k: old.get(k) for k in ('bankroll', 'total_trades', 'total_pnl', 'last_scan', 'last_error')}
                        body += f'<h2>{esc(name)}</h2><pre class="card">{esc(json.dumps(fields,indent=2))}</pre>'
                    except (ValueError, OSError):
                        body += f'<p class="bad">Cannot read {esc(name)}</p>'
        else:
            report = s['report']
            body += f'<h2>Latest cycle</h2><p>{report.get("discovered",0)} markets discovered; {report.get("books_checked",0)} pairs of books checked; {report.get("opened",0)} opened; {report.get("settled",0)} settled.</p>'
            body += '<div class="scroll"><table><tr><th>Why a market was skipped</th><th>Count</th></tr>'
            for reason, count in sorted(report.get('rejections', {}).items(), key=lambda x: -x[1]):
                body += f'<tr><td>{esc(reason.replace("_"," "))}</td><td>{count}</td></tr>'
            body += '</table></div><h2>Paper baskets</h2>'
            body += '<p class="muted">Equal YES and NO holdings in one condition. Both books are checked twice. These are simulated fills, not proof of live atomic execution. Settlement requires an official result.</p>'
            body += '<div class="scroll"><table><tr><th>Market</th><th>Status</th><th>Cost</th><th>Units each</th><th>Proceeds</th><th>Exit reason</th></tr>'
            for b in s['baskets']:
                p = b['plan']
                body += '<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in [p['title'], b['status'], cash(b['cost']), p['units'], cash(b['proceeds']), b['reason']]) + '</tr>'
            if not s['baskets']:
                body += '<tr><td colspan="6">No eligible paper baskets yet. The rejection counts explain why.</td></tr>'
            body += '</table></div><h2>Experiment</h2>'
            body += f'<p>Entry deadline: {date(s["entry_deadline"])}. Settlement monitoring continues. Accounting check: {s["accounting_ok"]}.</p>'
            body += '<p class="muted">Learning is an outcome report; parameters stay fixed. Missing or stale marks show as unavailable.</p>'
        return HTMLResponse('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                            '<meta http-equiv="refresh" content="30"><title>Paper Recovery</title><style>' + CSS + '</style></head><body><main class="wrap">'
                            '<h1>Paper Recovery</h1><nav><a href="/?view=paper">Paper experiment</a><a href="/?view=trades">Whale activity</a>'
                            '<a href="/?view=history">Earlier experiments</a><a href="/api/state">Full status</a></nav>' + body + '</main></body></html>')
    return app
