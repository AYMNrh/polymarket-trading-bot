import json
data = json.load(open('/home/aymen/projects/scripts/trading-bot/whale_positions.json'))
print(f'Total positions in file: {len(data)}')
keys = list(data.keys())
for k in keys:
    d = data[k]
    print(f'  {d["wallet_label"]:15s} {d["direction"]:4s} net=${d["net_size"]:>6.0f} conf={d["conviction"]*100:.0f}% trend={d.get("conviction_trend","?")}')
