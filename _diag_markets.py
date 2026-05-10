from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()
print(f"Total: {len(markets)}")
m = markets[0]
print(f"Keys: {list(m.keys())}")
print(f"bestBid: {m.get(chr(98)+chr(101)+chr(115)+chr(116)+chr(66)+chr(105)+chr(100))}")
print(f"bestAsk: {m.get(chr(98)+chr(101)+chr(115)+chr(116)+chr(65)+chr(115)+chr(107))}")
print(f"spread: {m.get(chr(115)+chr(112)+chr(114)+chr(101)+chr(97)+chr(100))}")
q = str(m.get(chr(113)+chr(117)+chr(101)+chr(115)+chr(116)+chr(105)+chr(111)+chr(110),""))
print(f"question: {q[:80]}")
