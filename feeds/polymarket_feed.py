"""
Feed de mercados y precios de Polymarket via REST API pública.
Descubre mercados BTC/ETH/SOL activos y obtiene sus precios.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import aiohttp

from data.models import Market, MarketType

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# Patrones para detectar mercados crypto de precio
CRYPTO_PATTERNS = [
    (r"bitcoin|btc", "BTC"),
    (r"ethereum|eth\b", "ETH"),
    (r"solana|\bsol\b", "SOL"),
]
DIRECTION_PATTERNS = [
    (r"above|over|higher|up|exceed", "above"),
    (r"below|under|lower|down", "below"),
]

class PolymarketFeed:
    def __init__(self) -> None:
        self._markets: dict[str, Market] = {}
        self._prices: dict[str, dict] = {}      # token_id → {best_bid, best_ask}
        self._last_refresh = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def refresh_markets(self) -> list[Market]:
        """Descubre mercados crypto activos con vencimiento próximo."""
        try:
            session = await self._get_session()
            # Buscar mercados BTC/ETH/SOL con vencimiento en próximas 2 horas
            params = {
                "active": "true",
                "closed": "false",
                "limit": 100,
                "tag_slug": "crypto",
            }
            async with session.get(f"{GAMMA_API}/markets", params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json()

            markets = []
            raw_list = data if isinstance(data, list) else data.get("markets", [])

            for raw in raw_list:
                market = self._parse_market(raw)
                if market and market.seconds_to_expiry < 7200:  # próximas 2h
                    self._markets[market.market_id] = market
                    markets.append(market)

            self._last_refresh = time.time()
            return markets

        except Exception as e:
            print(f"[POLY] Error refresh markets: {e}")
            return []

    def _parse_market(self, raw: dict) -> Optional[Market]:
        try:
            question = str(raw.get("question") or raw.get("title") or "").lower()

            # Detectar símbolo crypto
            symbol = None
            for pattern, sym in CRYPTO_PATTERNS:
                if re.search(pattern, question, re.IGNORECASE):
                    symbol = sym
                    break
            if not symbol:
                return None

            # Detectar dirección
            direction = "above"
            for pattern, dir_ in DIRECTION_PATTERNS:
                if re.search(pattern, question, re.IGNORECASE):
                    direction = dir_
                    break

            # Extraer strike price del título
            strike = 0.0
            price_match = re.search(r'\$?([\d,]+(?:\.\d+)?)', question)
            if price_match:
                strike = float(price_match.group(1).replace(",", ""))

            # Tokens YES/NO
            outcomes = raw.get("outcomes", "[]")
            if isinstance(outcomes, str):
                import json
                try:
                    outcomes = json.loads(outcomes)
                except:
                    outcomes = []

            tokens = raw.get("clobTokenIds", raw.get("clob_token_ids", "[]"))
            if isinstance(tokens, str):
                import json
                try:
                    tokens = json.loads(tokens)
                except:
                    tokens = []

            if len(tokens) < 2:
                return None

            # end_time
            end_date = raw.get("endDate") or raw.get("end_date_iso") or ""
            if not end_date:
                return None

            import datetime
            end_time = datetime.datetime.fromisoformat(
                str(end_date).replace("Z", "+00:00")
            ).timestamp()

            if end_time < time.time():
                return None

            return Market(
                market_id=str(raw.get("conditionId") or raw.get("condition_id") or raw.get("id", "")),
                question=raw.get("question") or raw.get("title") or "",
                token_id_yes=str(tokens[0]),
                token_id_no=str(tokens[1]),
                end_time=end_time,
                market_type=MarketType.CRYPTO,
                reference_price=strike,
                symbol=symbol,
                direction=direction,
            )
        except Exception as e:
            return None

    async def get_price(self, token_id: str) -> Optional[dict]:
        """Obtiene best_bid y best_ask para un token."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None

            return {"best_bid": best_bid, "best_ask": best_ask}
        except:
            return None

    def get_active_markets(self) -> list[Market]:
        return [m for m in self._markets.values() if not m.is_expired]

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
