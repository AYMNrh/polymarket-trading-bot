from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()
print(f"Total: {len(markets)}")
# Check more indices for weather
indices = list(range(100, min(500, len(markets)), 20))
for i in indices[:20]:
    q = str(markets[i].get("question","?"))
    bid = markets[i].get(chr(98)+chr(101)+chr(115)+chr(116)+chr(66)+chr(105)+chr(100))
    print(f"  [{i}] {q[:70]} bid={bid}")
