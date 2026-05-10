from strategy import StrategyEngine
s = StrategyEngine()
for attr in ["min_confidence","min_ev","max_spread","min_wall_score","max_capital_per_position","slippage_bps"]:
    print(f"{attr}: {getattr(s, attr, chr(78)+chr(47)+chr(65))}")
