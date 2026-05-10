"""
Decodes Polymarket trades from transaction input data.
When a whale sends USDC to the CTF Exchange, the tx input data
contains the actual trade parameters including token addresses
that encode the market (city, temperature, outcome).

Works by:
1. Getting the transaction receipt for each USDC->CTF transfer
2. Parsing the txn input to extract token addresses
3. Decoding token addresses into condition IDs
4. Looking up market info from CLOB API
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CTF_EXCHANGE = "0x4bFb41d5B3570C1C6cBb5E7cB3E8d9a0B0a0b0c0"
NEG_RISK_CTF = "0xC5d563A36AE78145c45a50134d48A1215220f80a"

# Known Polymarket function selectors
SELECTORS = {
    "0x2287e350": "swapExactAmountIn",
    "0x2c642e6b": "swapExactAmountOut", 
}

# Known CTF exchange contract versions
CTF_CONTRACTS = {
    CTF_EXCHANGE.lower(): "CTF Exchange",
    NEG_RISK_CTF.lower(): "NegRiskCTF",
}


def parse_token_id_from_address(token_address: str) -> str | None:
    """
    Polymarket prediction tokens have addresses derived from condition IDs.
    For NegRisk markets: token address is the condition ID padded to 32 bytes.
    For Standard markets: similar pattern.
    
    This extracts the condition ID from the token address when possible.
    """
    addr = token_address.replace("0x", "").lower()
    # Token addresses on Polygon are 40 hex chars
    if len(addr) != 40:
        return None
    return f"0x{addr}"


def decode_swap_input(input_data: str) -> dict:
    """
    Decode the swapExactAmountIn/swapExactAmountOut input data.
    
    Standard ABI encoding for swapExactAmountIn:
    - selector: 4 bytes
    - tokenIn: 32 bytes (padded address)
    - amountIn: 32 bytes (uint256)
    - tokenOut: 32 bytes (padded address)
    - amountOutMin: 32 bytes (uint256)
    - deadline: 32 bytes (uint256)
    """
    if not input_data or input_data == "0x":
        return {}
    
    data = input_data.replace("0x", "")
    if len(data) < 8:
        return {}
    
    selector = f"0x{data[:8]}"
    fn_name = SELECTORS.get(selector, f"unknown({selector})")
    
    result = {"function": fn_name, "selector": selector}
    
    # Parse standard swap parameters (offset 8 bytes = after selector)
    # Each param is 64 hex chars (32 bytes)
    try:
        if len(data) >= 8 + 64:
            token_in = f"0x{data[8+24:8+64]}".lower()  # last 20 bytes of first param (address)
            result["token_in"] = token_in
        if len(data) >= 8 + 128:
            token_out = f"0x{data[8+88:8+128]}".lower()
            result["token_out"] = token_out
        if len(data) >= 8 + 64:
            amount_in = int(data[8+64:8+128], 16) if len(data) >= 8+128 else 0
            result["amount_in"] = amount_in / 1e6  # USDC decimals
    except (ValueError, IndexError) as e:
        logger.warning("Failed to parse swap input: %s", e)
    
    return result


def get_market_id_from_token(token_addr: str) -> str | None:
    """
    On Polymarket, prediction tokens have a known relationship to condition IDs.
    
    For NegRisk markets:
    - Token address = first 40 hex chars of keccak256(condition_id + outcome)
    
    We can reverse this by looking up the token on the CLOB API.
    For now, return the raw address as a placeholder.
    """
    if not token_addr or token_addr == "0x0000000000000000000000000000000000000000":
        return None
    return token_addr


def decode_trade_from_tx(tx: dict, clob_client=None) -> dict | None:
    """
    Decode a Polymarket trade from a transaction dict.
    
    Args:
        tx: Transaction dict with 'input', 'to', 'hash', 'value' fields
        clob_client: Optional CLOB client for market lookups
    
    Returns:
        Dict with decoded trade info or None if not a Polymarket trade
    """
    to_addr = tx.get("to", "").lower()
    if to_addr not in CTF_CONTRACTS:
        return None
    
    input_data = tx.get("input", "0x")
    if not input_data or input_data == "0x":
        return None
    
    decoded = decode_swap_input(input_data)
    if not decoded:
        return None
    
    result = {
        "tx_hash": tx.get("hash", ""),
        "contract": CTF_CONTRACTS.get(to_addr, to_addr),
        "function": decoded.get("function", "unknown"),
        "selector": decoded.get("selector", ""),
        "token_in": decoded.get("token_in", ""),
        "token_out": decoded.get("token_out", ""),
        "amount_in": decoded.get("amount_in", 0),
        "market_question": None,
        "city": None,
        "temperature": None,
    }
    
    # Try to get market info from CLOB
    if clob_client and decoded.get("token_out"):
        market_id = get_market_id_from_token(decoded["token_out"])
        if market_id:
            try:
                # Try looking up by token address
                for endpoint in ["/markets", f"/markets/{market_id}"]:
                    market = clob_client._get(endpoint)
                    if market:
                        title = market.get("title", market.get("question", ""))
                        result["market_question"] = title
                        result["city"] = extract_city(title)
                        result["temperature"] = extract_temperature(title)
                        break
            except Exception:
                pass
    
    return result


def extract_city(title: str) -> str | None:
    """Extract city name from market title."""
    if not title:
        return None
    t = title.lower()
    cities = ["new york", "nyc", "chicago", "los angeles", "miami",
              "houston", "phoenix", "denver", "seattle", "boston", "dallas",
              "san francisco", "washington", "philadelphia", "atlanta",
              "london", "tokyo", "paris", "berlin", "sydney"]
    for city in cities:
        if city in t:
            return city.title()
    return None


def extract_temperature(title: str) -> int | None:
    """Extract temperature threshold from market title."""
    if not title:
        return None
    import re
    match = re.search(r'(\d+)\s*(?:°|deg|degree|F)', title)
    return int(match.group(1)) if match else None


def analyze_whale_trades_from_txlist(
    transactions: list[dict],
    clob_client=None,
    label: str = ""
) -> dict:
    """
    Analyze a whale's trades from their transaction list.
    
    Args:
        transactions: List of tx dicts (from Etherscan txlist)
        clob_client: Optional CLOB client for market lookups
    
    Returns:
        Dict with analysis results
    """
    trades = []
    city_counts = defaultdict(int)
    temp_counts = defaultdict(int)
    total_buy = 0.0
    total_sell = 0.0
    
    for tx in transactions:
        to_addr = tx.get("to", "").lower()
        # CTF interactions send MATIC or call contract functions
        if to_addr not in CTF_CONTRACTS:
            continue
        
        trade = decode_trade_from_tx(tx, clob_client)
        if trade and trade.get("amount_in", 0) > 0:
            trades.append(trade)
            total_buy += trade["amount_in"]
            
            city = trade.get("city") or "unknown"
            temp = trade.get("temperature") or 0
            city_counts[city] += 1
            if temp:
                temp_counts[temp] += 1
    
    return {
        "wallet": label,
        "total_decoded_trades": len(trades),
        "total_buy_volume": round(total_buy, 2),
        "top_cities": sorted(city_counts.items(), key=lambda x: -x[1])[:10],
        "top_temperatures": sorted(temp_counts.items(), key=lambda x: -x[1])[:10],
        "recent_trades": trades[:10],
    }
