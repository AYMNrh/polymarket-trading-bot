#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')
from paper_trader import PaperTrader
t = PaperTrader(bankroll=100.0)
print(t.get_portfolio_report())
