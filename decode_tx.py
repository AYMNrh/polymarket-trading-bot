#!/usr/bin/env python3
"""Decode a single Polymarket transaction and show the market details."""
import json, sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load the transaction
with open('/tmp/cold_tx.json') as f:
    data = json.load(f)

tx = data.get('result', {})
inp = tx.get('input', '0x')
raw = inp.replace('0x', '')

print(f"Transaction: {tx.get('hash','')[:20]}...")
print(f"From: {tx.get('from','')[:15]}...")
print(f"To: {tx.get('to','')[:15]}...")
print(f"Selector: 0x{raw[:8]}")
print(f"Data length: {len(raw)//2} bytes")
print()

# Look up selector on 4byte.directory
import requests
try:
    r = requests.get(f"https://api.4byte.directory/api/v1/signatures/?hex_signature=0x{raw[:8]}", timeout=10)
    sigs = r.json().get('results', [])
    if sigs:
        print(f"Function: {sigs[0].get('text_signature','unknown')}")
except Exception as e:
    print(f"Signature lookup failed: {e}")

# Try decoding as standard swapExactAmountIn params
if len(raw) >= 8 + 64 * 5:
    print()
    print("Attempting to decode swap params:")
    # Each param is 32 bytes (64 hex chars) after the 4-byte selector
    token_in_hex = raw[8:8+64]
    amount_in_hex = raw[8+64:8+128]
    token_out_hex = raw[8+128:8+192]
    min_out_hex = raw[8+192:8+256] if len(raw) >= 8+256 else "0"
    
    # Extract address (last 20 bytes of the 32-byte padded value)
    token_in = "0x" + token_in_hex[24:]
    token_out = "0x" + token_out_hex[24:]
    amount_in = int(amount_in_hex, 16) if amount_in_hex else 0
    min_out = int(min_out_hex, 16) if min_out_hex else 0
    
    print(f"  tokenIn:  {token_in}  (USDC addr)")
    print(f"  amountIn: ${amount_in/1e6:.2f}")
    print(f"  tokenOut: {token_out}  (prediction token)")
    print(f"  minOut:   {min_out}")
    
    # Try to look up the tokenOut on CLOB API
    print()
    print(f"Looking up market for token: {token_out}")
    try:
        clob_url = f"https://clob.polymarket.com/markets?token_id={token_out}"
        r2 = requests.get(clob_url, timeout=10)
        markets = r2.json()
        if isinstance(markets, dict) and markets.get('data'):
            for m in markets['data']:
                print(f"  Market: {m.get('title','?')}")
                print(f"  Question: {m.get('question','?')}")
                tags = [t.get('label','') for t in m.get('tags',[])]
                print(f"  Tags: {tags}")
    except Exception as e:
        print(f"  CLOB lookup: {e}")
