#!/home/aymen/hermes-agent/venv/bin/python
"""Decode all events from a specific Polymarket transaction."""
import requests, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

key = 'T35WYX45NH88EENSM71UVNJAZQQDG3Z29I'

# Get a ColdMath USDC->CTF transaction from our database
from etherscan_client import EtherscanV2Client
client = EtherscanV2Client(key)

# Get ColdMath's transfers to find real tx hashes
cold = '0x594edb9112f526fa6a80b8f858a6379c8a2c1c11'
transfers = client.get_token_transfers(cold, start_block=86000000, limit=10)
ctf_set = {'0x4bfb41d5b3570c1c6cbb5e7cb3e8d9a0b0a0b0c0'.lower(),
           '0xc5d563a36ae78145c45a50134d48a1215220f80a'.lower()}

for t in transfers:
    to = t.get('to','').lower()
    if to in ctf_set:
        tx_hash = t.get('hash','')
        val = float(t.get('value',0)) / 1e6
        print(f"\n=== ${val:.2f} to {to[:15]} tx={tx_hash} ===")
        
        # Get the transaction receipt
        url = f"https://api.etherscan.io/v2/api?chainid=137&module=proxy&action=eth_getTransactionReceipt&txhash={tx_hash}&apikey={key}"
        r = requests.get(url, timeout=30)
        data = r.json()
        receipt = data.get('result')
        
        if not receipt:
            print(f"  No receipt available (tx too old?)")
            # Try getting the transaction itself
            url2 = f"https://api.etherscan.io/v2/api?chainid=137&module=proxy&action=eth_getTransactionByHash&txhash={tx_hash}&apikey={key}"
            r2 = requests.get(url2, timeout=15)
            tx = r2.json().get('result', {})
            inp = tx.get('input','')[:10]
            to_tx = tx.get('to','')[:15]
            from_tx = tx.get('from','')[:15]
            print(f"  Tx: from={from_tx} to={to_tx} input={inp}")
        else:
            logs = receipt.get('logs', [])
            print(f"  {len(logs)} events in this transaction:")
            for i, log in enumerate(logs):
                addr = log.get('address','')[:15]
                topics = log.get('topics', [])
                data_field = log.get('data','')
                print(f"  Log {i}: {addr} topics={len(topics)}")
                for j, t in enumerate(topics):
                    print(f"    topic{j}: {t[:66]}")
                if data_field:
                    print(f"    data: {data_field[:80]}")
        break  # Just one for now
