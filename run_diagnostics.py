import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')

from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()

markets = orch.clob.get_weather_markets()
print(f'Total markets: {len(markets)}')

# Check a few sample markets
import random
samples = random.sample(markets, min(5, len(markets)))
for m in samples:
    q = m.get('question', '?')
    token_id = m.get('condition_id', m.get('token_id', '?'))
    outcomes = m.get('outcomes', [])
    outcome_str = str(outcomes)[:40] if outcomes else 'none'
    print(f'  question: {q[:60]} | token={str(token_id)[:20]} | outcomes={outcome_str}')
print()

# Try scan a small batch directly
from strategy import GammaStrategy
strat = GammaStrategy(orch.clob)
strat.load_whale_activity([])
signals = strat.scan_all_markets(markets[:50])
print(f'Signals from first 50 markets: {len(signals)}')
if signals:
    for s in signals[:5]:
        print(f'  {s}')
else:
    # Check what scan_all_markets does internally
    import inspect
    src = inspect.getsource(strat.scan_all_markets)
    print('scan_all_markets source:')
    print(src[:2000])
