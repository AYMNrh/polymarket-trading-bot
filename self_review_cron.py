#!/usr/bin/env python3
"""Self-learning review cycle — runs as cron job, no interaction needed."""
from self_learning import SelfLearningEngine
from pathlib import Path
import json
import sys

learner = SelfLearningEngine()

# Load resolved markets from weather bot
weather_dir = Path('/home/aymen/weatherbot/data/markets')
resolved = []
if weather_dir.exists():
    for f in sorted(weather_dir.glob('*.json'))[-100:]:
        try:
            mkt = json.loads(f.read_text())
            if mkt.get('status') == 'resolved' and mkt.get('resolved_outcome'):
                resolved.append(mkt)
        except Exception:
            pass

if not resolved:
    print('No resolved markets to review yet.')
    sys.exit(0)

observations = learner.review_resolved_trades(resolved)
print(f'Reviewed {len(resolved)} resolved markets.')
print(f'{len(observations)} new observations/adjustments.')
print()
report = learner.strategy_report()
print(report)

# Telegram alert
try:
    from telegram_alerts import alert_strategy
    alert_strategy(report)
except Exception as e:
    print(f'Alert failed: {e}')
