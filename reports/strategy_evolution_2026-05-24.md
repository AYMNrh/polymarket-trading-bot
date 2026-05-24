# Strategy Evolution Report - 2026-05-24

This report summarizes how the current two production-candidate paper strategies were derived from the local code, trade history, strategy notes, archived ledgers, snapshots, and runtime logs in this repository.

## Source Data Used

- `data/paper_portfolio.json`
- `data/paper_trades.jsonl`
- `data/tail_experiment_portfolio.json`
- `data/tail_experiment_trades.jsonl`
- `data/v6_logs/events.jsonl`
- `data/snapshots/summary.csv`
- `archive/v1-pre-fix-20260521*/`
- `STRATEGY.md`
- `original_strategy.md`
- `paper_trader.py`
- `cron_ev_whale_cycle.py`
- `cron_tail_experiment.py`

The explicit local trade logs run mainly from 2026-05-09 through 2026-05-23. Snapshot history starts on 2026-05-13.

## Original Strategy

The first bot was a broad EV + whale-overlay weather paper trader:

- Scan curated weather markets.
- Estimate fair price from forecast temperature versus the market bucket.
- Compare fair price to market price.
- Use whale activity as a confirmation/veto overlay.
- Enter many small paper positions.
- Use take profit, hard take profit, trailing stop, EV flip, stop loss, and resolution exits.

Early parameters from the archived v1 strategy:

- `MIN_EV = 0.05`
- `MAX_BET = 2.0`
- `MIN_VOLUME = 200`
- `STOP_LOSS_PCT = 90`
- `TAKE_PROFIT_PCT = 100`
- `HARD_TAKE_PROFIT_PCT = 400`
- `TRAILING_STOP_PCT = 45`
- `MAX_PRICE = 0.45`

## Iteration 1: Main Bot Cleanup

The v1 analysis showed:

- Closed trades around the pre-fix snapshot: 206.
- Bankroll around `$262.53` from a `$100` start.
- Win rate around `49.5%`.
- Profit factor around `2.35`.
- The bot was profitable, but the distribution was ugly: many stop losses and a few very large winners.

Main changes made:

- Raised `MIN_EV` from `0.05` to `0.08`.
- Tightened `STOP_LOSS_PCT` from `90` to `70`.
- Raised `TRAILING_STOP_PCT` from `45` to `55`.
- Removed weak cities such as Los Angeles and San Francisco from active forecast locations.
- Added stronger runtime hygiene and state reconciliation.
- Fixed the fair-price inversion bug in `strategy.py`.

## Iteration 2: Whale Gate

The strategy notes introduced a quality-whale gate:

- Only wallets marked `weight: true` count for trading overlay.
- Low-quality or unverified wallets remain tracked but do not drive trade entries.
- The intended target was to grow from a few quality wallets toward 15-30 verified wallets.

Result from later raw data:

- Whale alignment helped annotate trades but did not fully separate winners from losers.
- Main bot PnL was driven more by price bucket and exit behavior than whale count alone.

## Iteration 3: Tail Experiment

The Tail bot was introduced as a separate experiment:

- BUY only.
- Tail buckets only: `or below` and `or higher`.
- Very cheap entries.
- Fair value divided by entry price as the primary signal.
- Staged exits at x2, x3, x4, plus MFE and trailing protection.

Tail experiment constants evolved toward:

- Entry max around `0.01`.
- Minimum volume lowered because tail markets are naturally thin.
- Minimum EV ratio initially around `3x`.
- Continuous mode after the initial trade cap was removed.

## Latest Raw Results

Main bot, latest portfolio:

- Starting bankroll: `$100`
- Bankroll: `$282.48`
- Open value: about `$34.34`
- Equity: about `$316.82`
- Account PnL: about `+$216.82`
- Return: about `+216.8%`
- Position records: 247
- Closed records: 235

Main bot winners:

- Middle buckets: `+$203.10`
- BUY side: `+$216.62`
- Entry `<=0.1c`: `+$162.16`
- Entry `0.2c-0.5c`: `+$45.62`
- Hard take profit exits: `+$261.65`

Main bot losers:

- Stop losses: `-$148.84`
- Entry `3c-10c`: `-$18.57`
- Entry `0.5c-1c`: `-$4.46`
- SELL side was effectively flat, not useful.

Tail bot, latest portfolio:

- Starting bankroll: `$100`
- Bankroll: `$127.70`
- Open value: about `$1.53`
- Equity: about `$129.23`
- Account PnL: about `+$29.23`
- Return: about `+29.2%`
- Position records: 48
- Closed records: 36

Tail bot winners:

- Below tails: `+$258.86`
- Above tails: `+$157.79`
- Entry `0.1c-0.2c`: `+$249.46`
- Entry `<=0.1c`: `+$111.80`
- Forecast gap `7F-12F`: `+$258.54`
- Forecast gap `12F+`: `+$136.44`
- x4 exits in event log: `+$502.72`

Tail bot losers:

- Forecast gap `<3F`: `-$1.85`
- Entry `0.2c-0.5c`: only `+$0.87`
- EV ratio `3x-5x`: only `+$2.67`
- Stop losses in event log: `-$3.80`

## Final Strategy 1: Cheap Middle Bucket Spike

This strategy is derived from the main bot's strongest raw-data slice.

Rules now encoded in `strategy-1-middle`:

- Market type: middle bucket only.
- Side: BUY only.
- Entry price: `<= 0.005`.
- Cities: Miami, Houston, Dallas, Seattle.
- Market volume: `>= 500`.
- Fair value / entry price: `>= 20x`.
- Position size: `$1` fixed paper allocation.
- Exit logic: existing hard take-profit, take-profit, trailing, stop-loss, and resolution system.

Why:

- Filtered raw slice: 63 records, `+$194.90`, average `+$3.09`.
- It keeps the middle-bucket upside while removing broad noisy tails, SELLs, weak cities, and mid-priced entries.

## Final Strategy 2: Ultra-Cheap Tail Spike Capture

This strategy is derived from the Tail bot's cleanest raw-data slice.

Rules now encoded in `strategy-2-tail`:

- Market type: above-tail or below-tail only.
- Side: BUY only.
- Entry price: `<= 0.002`.
- Fair value / entry price: `>= 5x`.
- Forecast gap: `>= 7F`.
- Volume: `>= 10`. The original `< 100` cap was removed after live runtime logs showed current tradeable tail markets commonly sit above `$100` volume.
- Position size: `$1` fixed paper allocation.
- Exit logic: staged x2/x3/x4 partials, MFE protection, trailing stop, and hard take-profit.

Why:

- Filtered raw slice: 33 records, `+$353.61`, average `+$10.72`, W/L/F `28/1/4`.
- Even cleaner volume-filtered slice: 12 records, `+$295.30`, W/L/F `11/0/1`.
- The edge is spike capture, not resolution holding.

## Real-Money Readiness Notes

These bots are still paper/simulation runners. Before enabling real-money execution:

- Keep per-trade sizing tiny.
- Confirm real order book depth, not just reported midpoint/last trade.
- Place exit orders immediately after entry.
- Keep strategy state separate from experimental ledgers.
- Continue logging every cycle, skip reason, open, update, partial close, close, and dashboard snapshot.
- Fix and audit any remaining ledger accounting issues before trusting exact per-position ROI.

The new implementation isolates both strategies into separate state and trade logs so live-readiness can be evaluated without mixing new production-candidate behavior into the older exploratory history.
