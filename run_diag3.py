import logging
logging.basicConfig(level=logging.INFO)
import inspect

import strategy
print('=== _extract_city ===')
print(inspect.getsource(strategy.StrategyEngine._extract_city))
print()
print('=== _extract_temperature ===')
print(inspect.getsource(strategy.StrategyEngine._extract_temperature))
print()

# Also check a couple weather market titles if any
from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()
weather_titles = [m.get('question', m.get('title', '?')) for m in markets[:20]]
print('=== Sample titles (weather-tagged) ===')
for t in weather_titles:
    print(f'  {t[:90]}')
