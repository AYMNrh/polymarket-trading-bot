#!/home/aymen/hermes-agent/venv/bin/python
"""Decode ColdMath's actual Polymarket trades from on-chain events."""
import os, sys, requests, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['POLYGONSCAN_API_KEY'] = 'T35WYX45NH88EENSM71UVNJAZQQDG3Z29I'

key = 'T35WYX45NH88EENSM71UVNJAZQQDG3Z29I'
neg = '0xC5d563A36AE78145c45a50134d48A1215220f80a'
cold = '0x594edb9112f526fa6a80b8f858a6379c8a2c1c11'.lower()

# Get event logs from NegRisk where ColdMath is involved
url = f"https://api.etherscan.io/v2/api?chainid=137&module=logs&action=getLogs&address={neg}&fromBlock=86000000&toBlock=86100000&apikey={key}&limit=1000"
r = requests.get(url, timeout=30)
logs = r.json().get('result', [])

# Find the position events (0xd0a08e8c...) where ColdMath is topic3 (taker)
# These contain the condition ID in topic1
conditions = set()
for log in logs:
    topics = log.get('topics', [])
    sig = topics[0] if topics else ''
    if 'd0a08e8c' in sig and len(topics) >= 4:
        # topic3 = last 20 bytes = taker address
        taker = '0x' + topics[3][-40:] if len(topics[3]) >= 40 else ''
        if taker.lower() == cold:
            condition_id = '0x' + topics[1][-64:]
            conditions.add(condition_id)

print(f"Found {len(conditions)} unique markets ColdMath traded\n")

# Look up each market on CLOB API
for i, cid in enumerate(list(conditions)[:10]):
    url2 = f"https://clob.polymarket.com/simplified-markets?condition_id={cid}"
    r2 = requests.get(url2, timeout=15)
    if r2.status_code == 200:
        data = r2.json().get('data', [])
        for m in data[:1]:
            title = m.get('question', m.get('title', '?'))
            print(f"{i+1}. {title[:80]}")
    else:
        print(f"{i+1}. condition: {cid[:30]}... (lookup failed)")
