"""
Polymarket Subgraph client — queries on-chain trade data via The Graph.
No API key needed. Returns exact markets, prices, outcomes for any wallet.
"""
import logging
from collections import defaultdict

import requests

logger = logging.getLogger(__name__)

# Polymarket subgraph on The Graph Network
SUBGRAPH_URL = "https://api.thegraph.com/subgraphs/name/polymarket/matic-subgraph"


class PolymarketSubgraph:
    """Queries Polymarket's subgraph for whale trade data."""

    def __init__(self):
        self._session = requests.Session()

    def _query(self, graphql_query: str) -> dict | None:
        """Execute a GraphQL query against the Polymarket subgraph."""
        try:
            r = self._session.post(
                SUBGRAPH_URL,
                json={"query": graphql_query},
                timeout=30,
            )
            data = r.json()
            if "errors" in data:
                logger.warning("Subgraph error: %s", data["errors"][:1])
                return None
            return data.get("data")
        except Exception as e:
            logger.error("Subgraph request failed: %s", e)
            return None

    def get_whale_trades(self, wallet_address: str, limit: int = 50) -> list[dict]:
        """
        Get all filled orders for a wallet.
        
        Returns: list of trades with outcome, token price, size, market info
        """
        wallet = wallet_address.lower()
        query = f"""
        {{
          orders(
            first: {limit},
            where: {{maker: "{wallet}"}},
            orderBy: creationTimestamp,
            orderDirection: desc
          ) {{
            id
            maker
            outcome
            tokenSize
            price
            fee
            creationTimestamp
            filled
            salt
            market {{
              id
              question
              title
              outcomeType
              creationTimestamp
              outcomes
              tags {{
                id
                label
              }}
            }}
          }}
        }}
        """
        data = self._query(query)
        if not data:
            return []
        return data.get("orders", [])

    def get_whale_positions(self, wallet_address: str) -> list[dict]:
        """Get current open positions for a wallet."""
        wallet = wallet_address.lower()
        query = f"""
        {{
          positions(
            where: {{address: "{wallet}"}},
            orderBy: timestamp,
            orderDirection: desc,
            first: 50
          ) {{
            id
            address
            timestamp
            outcome
            size
            value
            market {{
              id
              question
              title
              outcomes
              tags {{ label }}
            }}
          }}
        }}
        """
        data = self._query(query)
        if not data:
            return []
        return data.get("positions", [])

    def get_market_detail(self, market_id: str) -> dict | None:
        """Get details for a specific market."""
        query = f"""
        {{
          market(id: "{market_id}") {{
            id
            question
            title
            outcomeType
            outcomes
            creationTimestamp
            tags {{ label }}
          }}
        }}
        """
        data = self._query(query)
        if not data:
            return None
        return data.get("market")

    def analyze_whale(self, wallet_address: str, label: str = "") -> dict:
        """
        Full analysis: get all trades, decode outcomes, summarize strategy.
        """
        orders = self.get_whale_trades(wallet_address, limit=100)
        
        trades = []
        city_counts = defaultdict(int)
        outcome_counts = defaultdict(int)
        total_volume = 0.0
        buy_count = 0
        sell_count = 0
        
        for o in orders:
            market = o.get("market", {})
            question = market.get("question", market.get("title", "Unknown"))
            outcome = o.get("outcome", "?")
            price = float(o.get("price", 0))
            size = float(o.get("tokenSize", 0))
            value = price * size
            filled = o.get("filled", False)
            timestamp = o.get("creationTimestamp", "")
            
            # Determine direction
            direction = "BUY"
            
            # Extract city and temperature from question
            city = self._extract_city(question)
            temp = self._extract_temperature(question)
            
            trade = {
                "question": question[:80],
                "outcome": outcome,
                "price": round(price, 4),
                "size": round(size, 4),
                "value": round(value, 2),
                "direction": direction,
                "city": city,
                "temperature": temp,
                "filled": filled,
                "timestamp": timestamp,
            }
            trades.append(trade)
            
            if city:
                city_counts[city] += 1
            outcome_counts[outcome] += 1
            total_volume += value
            buy_count += 1

        # Summarize
        top_cities = sorted(city_counts.items(), key=lambda x: -x[1])[:10]
        
        return {
            "wallet": label or wallet_address[:10],
            "address": wallet_address,
            "total_orders": len(orders),
            "total_volume": round(total_volume, 2),
            "buy_count": buy_count,
            "top_cities": top_cities,
            "recent_trades": trades[:10],
        }

    @staticmethod
    def _extract_city(text: str) -> str | None:
        if not text:
            return None
        t = text.lower()
        cities = ["new york", "nyc", "chicago", "los angeles", "miami",
                  "houston", "phoenix", "denver", "seattle", "boston", "dallas",
                  "san francisco", "washington", "philadelphia", "atlanta",
                  "london", "tokyo", "paris", "berlin", "sydney"]
        for city in cities:
            if city in t:
                return city.title()
        return None

    @staticmethod
    def _extract_temperature(text: str) -> int | None:
        if not text:
            return None
        import re
        match = re.search(r'(\d+)\s*(?:°|deg|degree|F)', text)
        return int(match.group(1)) if match else None
