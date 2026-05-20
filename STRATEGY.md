# Strategy: Whale-Aligned Edge Trading v2

## Core Principle

Trade only when 2+ quality whales are aligned. Quality whales have
`weight: true` in the wallet DB — determined by consistent edge-finding
(avg entry < $0.70, 55%+ WR), not by dumping capital on 99¢ certainties.

## 1. Entry Rules (PaperTrader)

```
WHALE GATE (evaluate_and_trade):
  whale_overlay["count"] == 0   → skip (no quality whales in market)
  whale_overlay["aligned"] == False → skip (< 2 quality whales aligned)
  
  Old veto removed: "whale opposed + weak edge" was replaced by
  the stricter gate above. If it doesn't have 2+ aligned quality
  whales, it doesn't trade.
```

Everything else stays: EV model (weather bot CDF), Kelly fractional
sizing, multi-layer exit logic.

## 2. Whale Quality DB (whale_watch.json)

Each wallet has a `weight` field:

```json
{
  "address": "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11",
  "label": "ColdMath",
  "win_rate": null,
  "trades_tracked": 0,
  "weight": true
}
```

- `weight: true` → positions are loaded for overlay decisions
- `weight: false` → wallet is tracked (win rate, activity) but does NOT
  count toward trade alignment. Washers and unverified wallets go here.

Currently quality: ColdMath, Sharky6999, RN1. All 114 other wallets
in the DB are weight: false (unverified).

### Quality qualification (for setting weight: true):
- Historical WR > 55%
- Avg entry price < $0.70 (avoids 99¢ washers)
- Consistent across 10+ trades
- Manually verified before flipping weight flag

## 3. Alignment Threshold

Require 1+ quality whales aligned (same market, same direction) to
signal alignment. Target is 2+ once more quality whales are discovered,
but for now 1 is sufficient given only 2 quality whales actively hold
weather positions.

`_check_whale_overlay()` changed from `aligned > opposed` to
`aligned >= 1` (was briefly `aligned >= 2`, relaxed back).

## 4. Whale Discovery (Future)

Target: 15-30 quality whale wallets.

Methods (not yet implemented):
1. **Gamma API scan** — query resolved weather markets, find profitable wallets
2. **Cross-category consistency** — wallet profitable in weather + other
   categories gets higher quality score
3. **Recursive discovery** — from known quality whales, check co-traders

Output: wallets added to `whale_watch.json` with `weight: false`
until manually verified.

## 5. LA Cleanup

"los angeles" removed from `_title_overlaps()` city_tokens. LA was
already removed from FORECAST_LOCATIONS (30% WR, -$6.10), so the
overlap check was matching trades against a city with no forecast.
