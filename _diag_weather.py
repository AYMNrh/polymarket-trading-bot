from orchestrator import WhaleOrchestrator
orch = WhaleOrchestrator()
markets = orch.clob.get_weather_markets()
print(f"Total: {len(markets)}")
# Check for weather-related titles
weather_count = 0
for m in markets[:100]:
    q = str(m.get("question", "")).lower()
    if "temp" in q or "weather" in q or "high" in q or "low" in q or "celsius" in q or "fahrenheit" in q or "degree" in q or "snow" in q or "rain" in q or "humidity" in q:
        weather_count += 1
print(f"Weather-related titles in first 100: {weather_count}")

# Check which have bestBid/bestAsk
has_bid = sum(1 for m in markets if m.get(chr(98)+chr(101)+chr(115)+chr(116)+chr(66)+chr(105)+chr(100)) is not None)
has_ask = sum(1 for m in markets if m.get(chr(98)+chr(101)+chr(115)+chr(116)+chr(65)+chr(115)+chr(107)) is not None)
print(f"Markets with bestBid: {has_bid}/{len(markets)}")
print(f"Markets with bestAsk: {has_ask}/{len(markets)}")

# Sample some titles
print()
print("Sample titles:")
for i in [0,1,2,3,4,5,10,20,50,99]:
    if i < len(markets):
        q = markets[i].get("question","?")
        bid = markets[i].get(chr(98)+chr(101)+chr(115)+chr(116)+chr(66)+chr(105)+chr(100))
        print(f"  [{i}] {str(q)[:60]} bid={bid}")
