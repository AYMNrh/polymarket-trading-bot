# Comprehensive Trading Bot System Document

Generated: 2026-05-24  
Workspace: `/home/aymen/projects/scripts/trading-bot`  
Git commit pushed before this document: `017413b`  
Branch: `codex/backup-before-cleanup-20260520`

## 1. Executive Summary

This project now runs three isolated paper-trading strategy bots for Polymarket weather markets. The old broad paper-trading loops were paused and replaced with strategy-specific bots, strategy-specific state files, strategy-specific trade logs, and strategy-specific dashboard pages.

The current system separates:

- Entry discovery: every 5 minutes.
- Position monitoring and exits: every 1 minute.
- Strategy performance analysis: dashboard and JSON state files.
- Runtime event logging: append-only structured logs.
- Historical repair/rebuild tooling: reconstructs position state from trade logs when needed.

The main reason for this redesign was to stop mixing experimental behaviors, make the results auditable, and prepare the codebase for eventual real-money testing with small stakes.

Important: these bots are still paper-trading. The current numbers are useful for strategy evaluation, but they are not yet a real-money profit record.

## 2. Current Running System

### Active Processes

The live dashboard is served by:

```text
python3 -m uvicorn dashboard:app --host 0.0.0.0 --port 9091
```

Dashboard URL:

```text
http://127.0.0.1:9091
```

Cloudflared is also running to expose the dashboard tunnel.

### Active Hermes Jobs

The active Hermes cron jobs are:

| Job | Schedule | Purpose |
|---|---:|---|
| `strategy1-middle-bot` | every 5m | Strategy 1 entry scan |
| `strategy2-tail-bot` | every 5m | Strategy 2 entry scan |
| `strategy3-compound-tail-bot` | every 5m | Strategy 3 entry scan |
| `strategy1-position-monitor` | every 1m | Strategy 1 price updates and exits only |
| `strategy2-position-monitor` | every 1m | Strategy 2 price updates and exits only |
| `strategy3-position-monitor` | every 1m | Strategy 3 price updates and exits only |
| `whale-enrich` | every 360m | Whale enrichment and cached wallet context |

The old broad paper/tail jobs were paused earlier and should remain paused while the strategy bots are being evaluated.

## 3. Dashboard Pages

The dashboard now has dedicated pages for each strategy:

| Page | URL |
|---|---|
| Main dashboard | `http://127.0.0.1:9091/` |
| Strategy 1 | `http://127.0.0.1:9091/?view=strategy1` |
| Strategy 2 | `http://127.0.0.1:9091/?view=strategy2` |
| Strategy 3 | `http://127.0.0.1:9091/?view=strategy3` |
| Strategy report | `http://127.0.0.1:9091/?view=strategy-report` |

Each strategy page shows:

- Ledger equity.
- Total PnL.
- Realized PnL.
- Open PnL.
- Cash state.
- Open value.
- Open positions.
- Closed positions.
- Closed win rate.
- Unique markets.
- Last entry cycle opens.
- Last monitor cycle closes.
- Open trades.
- Closed history.
- Realtime runtime events.

Strategy 3 also shows:

- Next stake.
- Max stake.
- Profit reinvest percentage.
- Last stake update reason.

## 4. Important Accounting Terms

### Cash State

Cash State is the bot's saved cash number in the portfolio JSON file.

This number changes when positions open, partially close, and fully close. Because we repaired historical state from append-only trade logs, some older reconstructed positions do not perfectly match the historical cash trail. For that reason, Cash State is useful as runtime state, but it is not the best strategy-performance metric.

### Ledger Equity

Ledger Equity is the accountant-style value derived from trade records.

Formula:

```text
Ledger Equity = Starting Bankroll + Realized PnL + Open PnL
```

This is the better metric for paper strategy analysis because it uses the trade ledger.

### Realized PnL

Realized PnL is the sum of PnL from closed positions, including partial exits when present.

### Open PnL

Open PnL is mark-to-market PnL for currently open positions.

### Total PnL

Total PnL is:

```text
Total PnL = Realized PnL + Open PnL
```

The dashboard and `PaperTrader.summary()` were fixed so total PnL now includes closed plus open PnL. Before the fix, `summary()["total_pnl"]` only counted open positions, which made totals look wrong.

## 5. Current Performance Snapshot

Latest snapshot from the local state files:

| Strategy | Trades | Open | Closed | W/L/F | Realized | Open PnL | Total PnL | Cash State | Unique Markets |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Strategy 1 | 41 | 6 | 35 | 34/1/0 | +$229.25 | +$7.49 | +$236.74 | $101.50 | 9 |
| Strategy 2 | 10 | 0 | 10 | 10/0/0 | +$14.08 | $0.00 | +$14.08 | $105.59 | 10 |
| Strategy 3 | 5 | 0 | 5 | 5/0/0 | +$6.02 | $0.00 | +$6.02 | $106.00 | 5 |

Strategy 3's next stake is currently `$4.00`, because its first five trades closed as winners and the compounding rule increased the planned stake.

## 6. Strategy 1: Cheap Middle Bucket Spike

### Purpose

Strategy 1 is designed to catch extremely cheap middle temperature buckets that appear mispriced relative to forecast/fair value.

### Entry Logic

Strategy 1 trades only:

- BUY side.
- Middle buckets only, such as "between 80-81F".
- Cities: Miami, Houston, Dallas, Seattle.
- Entry price <= `$0.005`.
- Market volume >= `$500`.
- Fair price / entry price >= `20x`.
- Fixed allocation: `$1.00` per trade.

### Exit Logic

Strategy 1 uses the general risk exit framework:

- Hard take profit.
- Stop loss.
- Cooldown after non-resolution closes.
- Price updates through the 1-minute monitor loop.

### Current Read

Strategy 1 has the largest paper PnL, but the profit is highly concentrated.

Most of the realized profit came from one Dallas middle-bucket market:

```text
Will the highest temperature in Dallas be between 80-81F on May 24?
```

That market produced repeated profitable entries before the unique-position and cooldown fixes were added.

### Strengths

- Very high upside when it catches a real mispricing.
- The current open trades are not only Dallas, which is encouraging.
- Fixed small stake keeps nominal downside small.

### Weaknesses

- Historical profit is concentrated in one market.
- Repeated entries can inflate confidence if not interpreted carefully.
- Needs more unique-market proof before real-money scaling.

### Current Recommendation

Treat Strategy 1 as a high-upside experimental bot. It can be included in a small real-money test later, but should receive less capital than the cleaner tail strategies until it proves itself across more unique markets.

## 7. Strategy 2: Ultra-Cheap Tail Spike Capture

### Purpose

Strategy 2 is designed to trade very cheap tail buckets where the entry price is tiny but the fair-value gap is large.

### Entry Logic

Strategy 2 trades only:

- BUY side.
- Tail buckets only, such as "or below" or "or higher".
- Entry price <= `$0.002`.
- Fair price / entry price >= `5x`.
- Forecast gap >= `7F`.
- Market volume >= `$10`.
- Fixed allocation: `$1.00` per trade.

An important bug was fixed before Strategy 2 started working:

```text
Old: volume had to be >= 10 and < 100
New: volume only has to be >= 10
```

The old max-volume cap excluded the real Polymarket tail market because many tail buckets have volume above `$100`.

### Exit Logic

Strategy 2 uses tail-experiment staged exits:

- Partial exits at multiples such as x2, x3, x4.
- MFE protection.
- Trailing stop.
- Profit protection.
- Stop loss.
- 1-minute monitor loop for faster exit management.

### Current Read

Strategy 2 is cleaner than Strategy 1 so far.

It has:

- 10 trades.
- 10 closed.
- 10 winners.
- 10 unique markets.
- No open exposure.

This is a healthier early pattern than Strategy 1 because the wins are distributed across more unique markets.

### Strengths

- Cleaner distribution across markets.
- No reliance on one repeated market.
- Fast exits through trailing stop / partial profit logic.
- Good candidate for tiny real-money testing after more paper sample size.

### Weaknesses

- Small sample size.
- Thin/cheap markets can have noisy or fake mark-to-market moves.
- Real fills may be worse than paper marks.

### Current Recommendation

Strategy 2 is the cleanest candidate for eventual small real-money testing. It should remain paper-traded until it has a larger sample, but if choosing one bot for a tiny test, Strategy 2 is currently the most defensible.

## 8. Strategy 3: Compound Tail Stake

### Purpose

Strategy 3 tests a money-management layer on top of Strategy 2's tail-entry logic.

The signal model is mostly Strategy 2. The difference is the stake sizing.

### Entry Logic

Strategy 3 uses Strategy 2-style tail filters:

- BUY side.
- Tail buckets only.
- Entry price <= `$0.002`.
- Fair price / entry price >= `5x`.
- Forecast gap >= `7F`.
- Market volume >= `$10`.

Additional Strategy 3 liquidity rule:

```text
market volume must be >= planned stake * 20
```

This prevents the compounding stake from growing into markets that are too thin for the planned size.

### Staking Logic

Strategy 3 starts with:

```text
base stake = $1.00
```

After a winning full close:

```text
next stake = current stake + 50% of net profit
```

After a losing full close:

```text
next stake = $1.00
```

Maximum stake:

```text
$5.00
```

Important: partial exits do not change the next stake. The next stake only updates after the position fully closes.

### Current Read

Strategy 3 completed its first 5 trades:

- 5 trades.
- 5 closed.
- 5 winners.
- Total PnL: +$6.02.
- Next stake: `$4.00`.

This is a good first result, but the sample is very small.

### Strengths

- Lets winners increase future position size.
- Resets after losses instead of martingaling.
- Max stake prevents exponential runaway.
- Liquidity check prevents oversizing into tiny books.

### Weaknesses

- Compounding can overstate performance in a lucky streak.
- If real fills slip, compounded stakes can turn small paper edges into worse real execution.
- Needs careful monitoring once next stake rises above `$1`.

### Current Recommendation

Strategy 3 is promising but should stay paper-only until it has significantly more trades. The first result is clean, but compounding should not be trusted from a five-trade sample.

## 9. Entry Scan vs Monitor Split

The system now separates entry scans from position management.

### Entry Scan

Runs every 5 minutes.

Purpose:

- Discover current weather markets.
- Evaluate new entries.
- Open positions if filters pass.

Jobs:

```text
strategy1-middle-bot
strategy2-tail-bot
strategy3-compound-tail-bot
```

### Position Monitor

Runs every 1 minute.

Purpose:

- Update prices for open positions.
- Apply risk stops.
- Apply take-profit logic.
- Apply staged partial exits.
- Resolve positions.
- Never open new trades.

Jobs:

```text
strategy1-position-monitor
strategy2-position-monitor
strategy3-position-monitor
```

This split is safer than running full entry scans every minute because it reacts faster to exits without increasing overtrading risk.

## 10. Files Added or Changed

### Core Strategy Engine

```text
paper_trader.py
```

Major changes:

- Added strategy modes:
  - `strategy-1-middle`
  - `strategy-2-tail`
  - `strategy-3-compound-tail`
- Added separate state files and trade logs.
- Added Strategy 1 filters.
- Added Strategy 2 filters.
- Added Strategy 3 stake state.
- Added Strategy 3 compounding/reset logic.
- Fixed total PnL summary.
- Fixed closed PnL counting with partial exits.
- Added unique position IDs so repeated entries no longer overwrite old history.
- Added strategy runtime event logging.
- Disabled unsafe bankroll reconciliation for repaired strategy modes.

### Shared Runner

```text
strategy_bot_runner.py
```

Purpose:

- Runs entry cycles for each strategy.
- Runs monitor-only cycles for each strategy.
- Logs cycle start/end events.
- Updates last strategy cycle and last monitor cycle in state.

### Cron Entrypoints

```text
cron_strategy1_middle.py
cron_strategy2_tail.py
cron_strategy3_compound_tail.py
```

These run the strategy entry cycle directly.

### Hermes Entry Wrappers

```text
scripts/hermes_strategy1_middle.py
scripts/hermes_strategy2_tail.py
scripts/hermes_strategy3_compound_tail.py
```

These are copied into `~/.hermes/scripts/` as:

```text
strategy1_middle.py
strategy2_tail.py
strategy3_compound_tail.py
```

### Hermes Monitor Wrappers

```text
scripts/hermes_strategy1_monitor.py
scripts/hermes_strategy2_monitor.py
scripts/hermes_strategy3_monitor.py
```

These are copied into `~/.hermes/scripts/` as:

```text
strategy1_monitor.py
strategy2_monitor.py
strategy3_monitor.py
```

### Dashboard

```text
dashboard.py
```

Major changes:

- Added Strategy 1 page.
- Added Strategy 2 page.
- Added Strategy 3 page.
- Added Strategy Report page.
- Added ledger equity and PnL cards.
- Added runtime log table.
- Added Strategy 3 stake cards.
- Added last monitor close card.

### Repair Tool

```text
repair_strategy_state.py
```

Purpose:

- Rebuild strategy state files from append-only trade logs.
- Preserve historical trade records.
- Restore overwritten re-entry history.
- Recompute wins/losses.
- Include partial-exit PnL in classification.

### Reports

```text
reports/strategy_evolution_2026-05-24.md
reports/comprehensive_trading_bot_system_2026-05-24.md
```

The first report documents how the strategies evolved. This report documents the whole current operating system.

### Ignore Rules

```text
.gitignore
```

Updated to avoid pushing runtime artifacts:

- root `archive/`
- `cloudflared`
- `ngrok`
- hyphenated data backups such as `data/*.bak-*`

## 11. State and Logs

### Strategy State Files

These files store each bot's current paper portfolio state:

```text
data/strategy1_middle_portfolio.json
data/strategy2_tail_portfolio.json
data/strategy3_compound_tail_portfolio.json
```

### Strategy Trade Logs

These are append-only trade records:

```text
data/strategy1_middle_trades.jsonl
data/strategy2_tail_trades.jsonl
data/strategy3_compound_tail_trades.jsonl
```

### Runtime Log

Structured runtime events:

```text
data/strategy_runtime.jsonl
```

This includes:

- cycle starts
- cycle ends
- skips
- trade opens
- updates
- closes
- monitor starts
- monitor ends
- Strategy 3 stake updates

## 12. Bugs Found and Fixed

### 1. Strategy 2 Volume Cap

Problem:

```text
STRATEGY2_MAX_VOLUME = 100.0
```

This excluded almost every real tail market because many Polymarket tail buckets have volume above `$100`.

Fix:

```text
Only require volume >= 10.
```

### 2. Re-Entry Overwriting

Problem:

Repeated entries into the same condition/side reused the same key:

```text
condition_id-BUY
```

That overwrote prior closed trades and made total trades disagree with stored records.

Fix:

Each entry now gets an immutable position ID:

```text
condition-side-sequence-timestamp
```

Open-position dedupe still prevents duplicate simultaneous entries, but closed history is preserved.

### 3. Total PnL Was Misleading

Problem:

`summary()["total_pnl"]` only counted open position PnL.

Fix:

It now computes:

```text
total_pnl = realized_pnl + unrealized_pnl
```

### 4. Dashboard Account PnL Was Misleading

Problem:

Dashboard account PnL used preserved cash state after repair, while trade rows used reconstructed ledger PnL.

Fix:

Dashboard now separates:

- Ledger Equity.
- Total PnL.
- Realized PnL.
- Open PnL.
- Cash State.

### 5. Strategy State Repair Needed Partial-Exit PnL

Problem:

Closed position classification could miss PnL from partial exits.

Fix:

Net PnL now includes:

```text
position pnl + sum(partial exit pnl)
```

## 13. Operational Commands

### Check Hermes Jobs

```bash
/home/aymen/.local/bin/hermes cron list
```

### Run Strategy Entry Manually

```bash
python3 cron_strategy1_middle.py
python3 cron_strategy2_tail.py
python3 cron_strategy3_compound_tail.py
```

### Run Position Monitor Manually

```bash
python3 scripts/hermes_strategy1_monitor.py
python3 scripts/hermes_strategy2_monitor.py
python3 scripts/hermes_strategy3_monitor.py
```

### Run Shared Runner Directly

Entry cycle:

```bash
python3 strategy_bot_runner.py strategy-1-middle
python3 strategy_bot_runner.py strategy-2-tail
python3 strategy_bot_runner.py strategy-3-compound-tail
```

Monitor cycle:

```bash
python3 strategy_bot_runner.py monitor strategy-1-middle
python3 strategy_bot_runner.py monitor strategy-2-tail
python3 strategy_bot_runner.py monitor strategy-3-compound-tail
```

### Restart Dashboard

```bash
python3 -m uvicorn dashboard:app --host 0.0.0.0 --port 9091
```

### Rebuild Strategy State From Logs

```bash
python3 repair_strategy_state.py
```

Use this only when state is corrupted or when trade records need to be reconstructed from append-only logs.

## 14. Risk Notes Before Real Money

### Paper Trading Is Not Real Execution

Paper trades use observed/derived prices. Real money introduces:

- Slippage.
- Failed fills.
- Partial fills.
- Wider effective spreads.
- Latency.
- Market impact.
- Fees or hidden costs.

### Strategy 1 Risk

Strategy 1 has the biggest paper gains, but its historical performance is concentrated. It needs more unique-market evidence before serious allocation.

### Strategy 2 Risk

Strategy 2 is cleaner but still has a small sample size. It should remain the primary candidate for a tiny real-money test only after more paper trades.

### Strategy 3 Risk

Strategy 3 compounds profits. This can grow stake size quickly during a winning streak. The `$5` cap and liquidity filter reduce the risk, but the strategy still needs more data before real-money use.

### Recommended Real-Money Readiness Criteria

Before using real money, require:

- At least 50 closed trades for Strategy 2 or Strategy 3.
- At least 25 unique markets.
- No major accounting mismatches.
- Realistic fill simulation or live tiny-fill test.
- Max stake hard cap.
- Exchange wallet balance reconciliation.
- Emergency kill switch.
- Clear daily loss limit.

## 15. Suggested Next Improvements

### 1. Add a Real-Money Safety Layer

Before live trading:

- Add `LIVE_TRADING_ENABLED=false` default.
- Require explicit env var for live orders.
- Add max daily spend.
- Add max daily loss.
- Add per-market exposure cap.
- Add global kill switch.

### 2. Add Fill Simulation

Improve paper realism by using:

- bid/ask spread
- order book depth
- slippage model
- minimum available ask size
- failed fill simulation

### 3. Add Strategy Comparison Page

Create one dashboard page showing all strategies side by side:

- total PnL
- realized PnL
- open PnL
- win rate
- unique markets
- avg PnL per trade
- max drawdown
- current exposure
- last entry scan
- last monitor run

### 4. Add Daily Reports

Generate one daily markdown report:

- new trades
- closed trades
- best/worst trades
- skipped trade counts by reason
- strategy-level performance
- recommended parameter changes

### 5. Add Alerts

Telegram alerts should trigger on:

- new trade opened
- trade closed
- Strategy 3 stake increase
- drawdown threshold
- monitor job failure
- dashboard down

## 16. Current Verdict

Strategy 1 is the high-upside spike bot. It has the biggest paper PnL but needs broader proof.

Strategy 2 is the cleanest early strategy. It has smaller profit but better distribution across unique markets.

Strategy 3 is a promising staking experiment on top of Strategy 2. Its first run closed 5/5 winners and moved next stake to `$4.00`, but it is far too early to trust the compounding system with real money.

The safest current architecture is the one now running:

```text
5-minute entry scans
1-minute exit monitors
separate strategy state
separate logs
dashboard visibility
paper-only operation
```

This should continue collecting paper data before any real-money deployment.
