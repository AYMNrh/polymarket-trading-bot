from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()
print(f"Total: {len(markets)}")
# Count weather-related titles across ALL 500
import re
weather_kw = ["temp","weather","high","low","celsius","fahrenheit","degree","snow","rain","humidity","wind","forecast","climate"]
count = 0
for m in markets:
    q = str(m.get("question","")).lower()
    if any(kw in q for kw in weather_kw):
        count += 1
print(f"Weather-related titles (broad): {count}")
# Show all that do have weather keywords
for i,m in enumerate(markets):
    q = str(m.get("question","")).lower()
    if any(kw in q for kw in weather_kw):
        print(f"  [{i}] {m.get(chr(113)+chr(117)+chr(101)+chr(115)+chr(116)+chr(105)+chr(111)+chr(110),"?")}")
        if count <= 10: continue
