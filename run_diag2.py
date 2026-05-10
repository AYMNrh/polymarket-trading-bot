import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
import inspect

import strategy
print('=== scan_all_markets ===')
print(inspect.getsource(strategy.StrategyEngine.scan_all_markets))
print()
print('=== evaluate_market ===')
print(inspect.getsource(strategy.StrategyEngine.evaluate_market))
