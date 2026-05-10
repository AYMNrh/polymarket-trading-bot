# Polymarket Whale Trading Platform

Paper trading engine for Polymarket prediction markets with whale overlay signals.

## Architecture

- **EV Core**: Real Open-Meteo ECMWF weather forecasts → fair price via normal CDF → Kelly sizing
- **Whale Overlay**: Tracks ColdMath, Sharky6999, RN1 on-chain → consensus signals boost/penalize confidence
- **Resolution**: Auto-checks Gamma API for closed markets, records wins/losses
- **Dashboard**: FastAPI at `localhost:9091` with Paper Portfolio, Order Books, Conviction, Strategy Report, Whale Trades tabs

## Quick Start

```bash
cd ~/projects/scripts/trading-bot
python3 cron_ev_whale_cycle.py          # run one trading cycle
python3 -m uvicorn dashboard:app --port 9091  # start dashboard
```

Dashboard: http://localhost:9091/?view=paper

## Key Files

| File | Purpose |
|------|---------|
| `paper_trader.py` | Core engine: EV, Kelly, position management, resolution |
| `strategy.py` | Weather market strategy + whale signal extraction |
| `dashboard.py` | 5-tab FastAPI dashboard |
| `polymarket_scraper.py` | Scrape live whale portfolios from Polymarket profiles |
| `onchain_decoder.py` | Decode whale USDC transfers → specific markets |
| `self_learning.py` | Review resolved trades, auto-tune parameters |
| `orchestrator.py` | 4-layer cycle: monitor, strategy, learning, alerts |
| `cron_ev_whale_cycle.py` | Canonical cron entrypoint for EV + whale overlay cycle |

## Data Sources

- **Weather**: Open-Meteo ECMWF IFS (bias-corrected) for all cities
- **Markets**: Polymarket Gamma API via event slugs
- **Whales**: Etherscan V2 (on-chain) + Polymarket profile scraper (live portfolio)
