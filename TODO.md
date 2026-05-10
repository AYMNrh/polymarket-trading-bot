# TODO

## Strategy Improvements

- [ ] **HRRR forecasts for US D+0/D+1** — swap ECMWF for gfs_seamless (3km HRRR) within 48h. Already proven in weatherbot. `_get_forecast_temp()` only calls ECMWF currently.

- [ ] **Stop-loss + take-profit** — 20% stop, staged take-profit ($0.75 at 48h, $0.85 at 24h, hold inside 24h). Same pattern weatherbot backtest proved at +29%.

- [ ] **Diversify cities** — all 15 positions are Chicago. Sigma tuning or per-city calibration needed so other cities produce balanced positions instead of 99% EV+ noise.

- [ ] **Dynamic Kelly sizing** — scale bets with confidence, not flat $2 cap.

- [ ] **Per-city sigma calibration** — after 30+ resolved trades per city, compute actual forecast MAE instead of fixed sigma=4.0°F/2.2°C.

- [ ] **Markets outside US** — London, Paris, Tokyo have 10-50x lower liquidity. Skip or adjust min_volume dynamically.

## Data Quality

- [ ] **Store actual_temp from ERA5 on resolve** — already fetched for self-learning, should persist on position for dashboard/review.

- [ ] **Whale signal match city + direction** — `_check_whale_overlay` uses title substring match. Should match by parsed city + temp + side explicitly.

## Infrastructure

- [ ] **Duplicate cron scripts** — 8 near-identical files. Consolidate to one `run_paper_cycle.py` with flags.

- [ ] **Atomic state writes** — `_save_state` overwrites in place. Write to temp file then rename to avoid corruption on crash.

- [ ] **Rate-limit handling** — Gamma API calls have no retry/backoff on 429s.
