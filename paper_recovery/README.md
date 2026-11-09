# Paper recovery

This is a new, isolated paper experiment. Existing weather, crypto and arbitrage
files and balances are preserved. The recovery runner never imports an order
submission client, loads private keys, or submits a live order.

## Run

From `/home/aymen/projects/scripts/trading-bot`:

```sh
python3 -m unittest paper_recovery.test_recovery -v
python3 -m paper_recovery once
python3 -m paper_recovery serve
```

`serve` starts a localhost-only dashboard on port 9091 and runs one cycle at a
time. It waits 60 seconds after each completed cycle; wallet activity is polled
normally every five minutes, or every minute while catching up on history. A file
lock prevents overlapping cron and service invocations. `/healthz` returns 503
for failed or stale trading scans or unhealthy whale monitoring.

For Hermes, the only recovery trading command is
`python3 -m paper_recovery once`, with that repository as its working directory.
Do not also schedule the old `trading_runner.py`, `scripts/fast_loop.py`, or old
weather entrypoints as part of this experiment: they run different strategies.
The new `serve` command already contains the worker, so it needs no extra cron.

On this WSL installation the provided user service can be installed with:

```sh
systemctl --user link /home/aymen/projects/scripts/trading-bot/paper_recovery/paper-recovery.service
systemctl --user enable --now paper-recovery.service
systemctl --user status paper-recovery.service
journalctl --user -u paper-recovery.service -n 30
```

The service depends on WSL and the host remaining awake. It does not enable VPN
auto-connect or change system DNS. If connectivity fails, scans report errors
and no missing data is substituted with a fake price. Keep TLS verification on.

On this Windows host, systemd alone did not keep the WSL distribution alive.
A hidden `wsl.exe -d Ubuntu --exec sleep infinity` process now keeps it running
for the current Windows session. This is not a Windows login startup task;
after a reboot WSL must be started again, and the host must remain awake.

## Exact experiment

- Starting cash: $100, initialized once, never refilled on a restart.
- Maximum total cost per two-leg paper basket: $10, including fees and buffer.
- Maximum open cost: $20. No reinvestment beyond the fixed exposure limit.
- New entries stop three days after the first successful market scan.
- Continue monitoring open positions for official settlement after the deadline.
- Universe: daily maximum-temperature YES/NO markets for twelve US cities,
  discovered by full dated event slug, covering yesterday through two days ahead.
- Up to 80 markets receive orderbook checks per cycle; discovery order rotates.
- Equal quantities of YES and NO must belong to the same verified condition.
- Both books must match token and condition IDs, be fresh, and have acceptable
  timestamp skew. Ask depth is walked in price order. No midpoint fills.
- Refetch both books and metadata before recording a simulated complete fill.
- The documented Gamma `feeSchedule` is used. Unknown fees or an unsupported
  fee-curve version reject the candidate. Fees are rounded conservatively.
- Per-order minima are enforced using the current documented Gamma cash minimum
  and additionally the CLOB book's size floor. Ambiguous metadata never permits
  an undersized order. With a $5 cash minimum on both legs, $10 plus fees will not
  fit the approved $10 total cap. Such baskets must be skipped.
- Minimum modeled profit: $0.02 after fees and a $0.01 execution buffer.
- Identical conditions cannot be reopened or credited twice.

Expected profit is not cash. Complete sets remain open until Gamma reports an
official resolved outcome with a valid payout. A closed market alone does not
trigger settlement. Missing bid depth or stale marks make liquidation equity
unavailable. The ledger checks `cash + open cost = initial cash + realized PnL`.

This is **not proof of live arbitrage execution**: two independent fills are not
atomic, prices may change between requests, and real settlement/merge operations
have operational costs. The simulator does not claim to have performed a merge.
It does not apply individual-leg take-profit stops, which would break the hedge.
Zero eligible trades is a valid outcome; the bot does not invent an edge to trade.

## Wallets and review

Watchlists are merged from `whale_watch.json` (`watched_wallets`) and
`data/watch_wallets.json` (list or `wallets`). Addresses are deduplicated without
case sensitivity. Public Data API trade activity replaces the authenticated
analytics provider for this runner. Initial history is capped at 100 recent
trades per wallet; later polling uses overlapping watermarks and bounded
pagination with persistent cursors and visible backlog warnings. Busy wallets
resume a frozen time window on the next poll rather than repeating the same
pages. This is activity monitoring, not a full
on-chain accounting indexer. Whale activity is information and does not alter
complete-set sizing or create trades.

Review reports summarize cycles, rejection reasons and realized outcomes.
Automatic parameter changes are disabled. Synthetic tests verify mechanics;
they are never mixed with the new experiment's PnL.

## Storage and views

- `paper_recovery/runtime/ledger.sqlite3`: account, baskets, scans, whale trades,
  polling cursors and experiment parameters. WAL files are part of SQLite.
- `/?view=paper`: paper ledger and scan rejection counts.
- `/?view=trades`: whale activity from the same recovery ledger.
- `/?view=history`: read-only summaries of the previous experiments.
- `/api/state`: full status, including quote samples and data errors.

Use SQLite's backup API when backing up an active database. Do not copy only
the main database file while ignoring its WAL. Runtime files are git-ignored.

## Audit findings, September 10, 2026

The Windows worktree was an obsolete May snapshot. The WSL repository was on
`codex/backup-before-cleanup-20260520`, commit `44314b3`, with additional uncommitted
changes. Its dashboard and strategy lineup had changed substantially.

Observed before repair:

1. Nothing listened on port 9091; no trading service was running.
2. The saved arbitrage scan reported invalid credentials for the Falcon provider.
   That failure was separate from the current Polymarket DNS connection failure.
3. All three public Polymarket API hostnames resolved to `145.239.225.117` and
   returned a certificate for `*.anj.fr`. Connecting the user's Mullvad VPN
   restored valid public API connections in Windows and WSL.
4. The old wallet reader used `data/watch_wallets.json`, while the 101-wallet
   watchlist was in `whale_watch.json`.
5. `arb_bot.py` was an analysis-only scanner: it never opened or settled a paper
   position. Negative-risk IDs did not prove that a paginated group contained
   all outcomes, and its executable classification omitted current fees/minima.
6. The old runner mixed unrelated strategies; dashboard uptime and trading
   activity were not the same thing. The latest saved BTC state was August 4,
   while 15-minute state was June 10. Earlier postmortem results therefore are
   historical snapshots, not current measurements of performance.

Sources checked:

- https://docs.polymarket.com/market-data/market-details
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/trading/positions/manage
- https://docs.polymarket.com/api-reference/core/get-user-activity
- https://anj.fr/promotion-illegal-gambling-services-blocking-polymarket-website

The repair makes the paper experiment observable and testable. It does not
establish that a profitable strategy exists at this budget or frequency.
