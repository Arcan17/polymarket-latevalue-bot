"""
Market Discovery para la serie "BTC Up or Down 5m" de Polymarket.

## Estructura real (verificada Abril 2026)

Serie: btc-up-or-down-5m
  - Crea un mercado nuevo cada 5 minutos automáticamente
  - Slug por evento: btc-updown-5m-{unix_timestamp_del_cierre}
  - Volumen: ~$29M/día (en la serie completa)
  - Liquidez por mercado: ~$20-50k USDC

Resolución:
  - "Up"   si BTC_chainlink_final >= BTC_chainlink_inicio
  - "Down" si BTC_chainlink_final <  BTC_chainlink_inicio
  - Fuente: Chainlink Data Streams BTC/USD (data.chain.link/streams/btc-usd)

Precio de referencia (strike):
  - El strike es el precio Chainlink al inicio del intervalo.
  - NO está disponible vía API pública (confirmado Abril 2026).
  - Mejor aproximación: Binance 1m klines open price al timestamp exacto.
    La diferencia Chainlink Data Streams / Binance suele ser < $10 en BTC.
  - Se fija UNA VEZ cuando el mercado arranca (interval_start pasó).
  - Para mercados futuros usa precio spot actual como estimación temporal.

Fees (verificados):
  - Maker: 0% (rebate completo del 100%)
  - Taker: 7.2%
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import aiohttp

from data.models import Market, MarketStatus
from utils.logger import get_logger

logger = get_logger(__name__)

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# slug_prefix → símbolo
SERIES_CONFIGS = {
    "btc-updown-5m":  "BTC",
    "eth-updown-5m":  "ETH",
    "sol-updown-5m":  "SOL",
    "xrp-updown-5m":  "XRP",
    "bnb-updown-5m":  "BNB",
    "btc-updown-15m": "BTC",
    "eth-updown-15m": "ETH",
    "sol-updown-15m": "SOL",
    "xrp-updown-15m": "XRP",
    "bnb-updown-15m": "BNB",
}

# Tamaño de slot por serie (segundos) — determina los timestamps a buscar
SERIES_SLOT_SIZES = {
    "btc-updown-5m":  300,
    "eth-updown-5m":  300,
    "sol-updown-5m":  300,
    "xrp-updown-5m":  300,
    "bnb-updown-5m":  300,
    "btc-updown-15m": 900,
    "eth-updown-15m": 900,
    "sol-updown-15m": 900,
    "xrp-updown-15m": 900,
    "bnb-updown-15m": 900,
}

# Binance symbol map
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT",
    "SOL": "SOLUSDT", "XRP": "XRPUSDT",
    "BNB": "BNBUSDT",
}


class MarketDiscovery:
    """
    Busca y mantiene los mercados BTC/ETH/SOL Up/Down 5m activos.

    Estrategia de discovery:
      1. Construye el slug del mercado actual: btc-updown-5m-{ts_cierre}
      2. También busca los próximos N mercados pre-creados
      3. Filtra por: active=True, closed=False, acceptingOrders=True

    Strike price:
      - Para mercados ya iniciados: Binance REST klines open price al segundo exacto
      - Para mercados futuros: precio spot actual (se actualiza cuando el mercado inicia)
    """

    def __init__(
        self,
        spot_price_fn=None,          # callable() → float | None  (BTC, retrocompat)
        price_history_fn=None,       # callable(symbol, ts) → float | None  (Binance WS snapshot)
        rtds_price_at_fn=None,       # callable(symbol, ts) → float | None  (Chainlink exacto)
        spot_price_fns: dict = None, # {"BTC": fn, "ETH": fn, "SOL": fn}
        lookahead_markets: int = 2,
    ) -> None:
        if spot_price_fns:
            self._spot_price_fns = spot_price_fns
        elif spot_price_fn:
            self._spot_price_fns = {"BTC": spot_price_fn}
        else:
            self._spot_price_fns = {}
        self._price_history_fn = price_history_fn
        self._rtds_price_at_fn = rtds_price_at_fn  # Chainlink histórico exacto
        self._lookahead = lookahead_markets
        self._markets: dict[str, Market] = {}       # condition_id → Market
        self._price_confirmed: set[str] = set()     # condition_ids con precio confirmado
        self._price_source: dict[str, str] = {}     # condition_id → "rtds" | "binance" | "pre"

    @property
    def active_markets(self) -> list[Market]:
        now = time.time()
        return [
            m for m in self._markets.values()
            if m.status == MarketStatus.ACTIVE and not m.is_expired
        ]

    async def refresh(self) -> list[Market]:
        """
        Busca mercados activos y retorna los nuevos.
        Para mercados ya conocidos: actualiza el strike si tenemos precio REST
        y el actual era solo una estimación (mercado futuro).
        """
        try:
            markets = await self._fetch_current_markets()
            new_markets = []
            for m in markets:
                if m.market_id not in self._markets:
                    new_markets.append(m)
                    confirmed = m.market_id in self._price_confirmed
                    if confirmed:
                        src_key = self._price_source.get(m.market_id, "rtds")
                        source = {"rtds": "Chainlink-RTDS ✓", "binance": "Binance-REST ✓",
                                  "pre": "pre-inicio ⚠️"}.get(src_key, "confirmado ✓")
                    else:
                        source = "estimado"
                    logger.info(
                        f"Mercado nuevo: {m.question[:55]} "
                        f"| strike=${m.reference_price:,.2f} ({source}) "
                        f"| T={m.seconds_to_expiry:.0f}s"
                    )
                else:
                    existing = self._markets[m.market_id]
                    # Actualizar strike solo si:
                    # 1. El nuevo tiene precio confirmado
                    # 2. El existente NO tiene precio confirmado (era estimación futura)
                    if (m.market_id in self._price_confirmed
                            and existing.market_id not in self._price_confirmed):
                        old_price = existing.reference_price
                        existing.reference_price = m.reference_price
                        self._price_confirmed.add(existing.market_id)
                        src_key = self._price_source.get(m.market_id, "rtds")
                        src_label = {"rtds": "Chainlink-RTDS", "binance": "Binance-REST",
                                     "pre": "pre-inicio ⚠️"}.get(src_key, "confirmado")
                        logger.info(
                            f"[{m.symbol}] Strike confirmado: "
                            f"${old_price:,.2f} → ${m.reference_price:,.2f} ({src_label})"
                        )
                    else:
                        # Preservar precio ya confirmado (no reemplazar con estimación)
                        m.reference_price = existing.reference_price
                self._markets[m.market_id] = m
            return new_markets
        except Exception as e:
            logger.error(f"MarketDiscovery.refresh error: {e}")
            return []

    async def _fetch_current_markets(self) -> list[Market]:
        """
        Busca mercados para BTC/ETH/SOL/XRP en series de 5m y 15m.
        Cada serie usa su propio tamaño de slot para generar los timestamps.
        """
        now = int(time.time())
        markets = []

        async with aiohttp.ClientSession() as session:
            for slug_prefix, symbol in SERIES_CONFIGS.items():
                if symbol not in self._spot_price_fns:
                    continue

                slot_s = SERIES_SLOT_SIZES.get(slug_prefix, 300)
                timestamps_to_check = set()
                for i in range(-1, self._lookahead + 2):
                    ts = ((now + i * slot_s) // slot_s) * slot_s
                    timestamps_to_check.add(ts)

                for ts in sorted(timestamps_to_check):
                    slug = f"{slug_prefix}-{ts}"
                    market = await self._fetch_event_by_slug(session, slug, symbol, slot_s=slot_s)
                    if market is not None:
                        markets.append(market)

        return markets

    async def _fetch_event_by_slug(
        self, session: aiohttp.ClientSession, slug: str, symbol: str = "BTC",
        slot_s: int = 300,
    ) -> Optional[Market]:
        """
        Obtiene un mercado por slug y lo convierte a Market.
        Si el mercado ya inició, obtiene el strike exacto via Binance REST klines.
        """
        try:
            url = f"{GAMMA_MARKETS_URL}?slug={slug}"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return None
                markets_data = await resp.json()

            if not markets_data:
                return None

            mkt = markets_data[0] if isinstance(markets_data, list) else markets_data

            if not mkt.get("acceptingOrders") or mkt.get("closed"):
                return None

            events_list = mkt.get("events", [])
            if not events_list:
                return None
            event = events_list[0]

            if not event.get("active") or event.get("closed"):
                return None

            # Extraer interval_start del evento
            import datetime
            start_time_str = event.get("startTime") or event.get("startDate") or mkt.get("startDate", "")
            interval_start: Optional[float] = None
            if start_time_str:
                try:
                    start_dt = datetime.datetime.fromisoformat(
                        str(start_time_str).replace("Z", "+00:00")
                    )
                    interval_start = start_dt.timestamp()
                except Exception:
                    pass

            # Strike price — orden de prioridad:
            # 1. Chainlink RTDS histórico (exacto — misma fuente que Polymarket)
            # 2. Binance REST klines open (aprox $5-20 de diferencia)
            # 3. Precio spot actual (estimación para mercados futuros)
            rtds_price: Optional[float] = None
            rest_price: Optional[float] = None

            if interval_start and interval_start <= time.time():
                # Intentar Chainlink histórico primero
                if self._rtds_price_at_fn is not None:
                    try:
                        rtds_price = self._rtds_price_at_fn(symbol, interval_start)
                    except Exception:
                        rtds_price = None

                # Fallback a Binance REST si no tenemos Chainlink histórico
                if rtds_price is None:
                    rest_price = await self._fetch_binance_kline_open(
                        symbol, interval_start, session
                    )

            confirmed_price = rtds_price or rest_price
            price_src = "rtds" if rtds_price else ("binance" if rest_price else None)
            market = self._parse_market(event, mkt, symbol, interval_start, confirmed_price,
                                        slot_s=slot_s)

            # Marcar como confirmado si tenemos precio exacto
            if market and confirmed_price is not None:
                self._price_confirmed.add(market.market_id)
                if price_src:
                    self._price_source[market.market_id] = price_src

            return market

        except Exception as e:
            logger.debug(f"Error fetching {slug}: {e}")
            return None

    async def _fetch_binance_kline_open(
        self,
        symbol: str,
        interval_start: float,
        session: aiohttp.ClientSession,
    ) -> Optional[float]:
        """
        Obtiene el precio de apertura de la vela de 1 minuto que contiene interval_start.
        Esto aproxima el precio Chainlink al inicio del intervalo.
        Binance open price ≈ Chainlink Data Streams (diferencia típica < $10 en BTC).
        """
        binance_symbol = BINANCE_SYMBOLS.get(symbol.upper())
        if not binance_symbol:
            return None

        # No pedir velas que aún no cerraron
        if interval_start > time.time() - 60:
            return None

        start_ms = int(interval_start * 1000)
        url = (
            f"{BINANCE_KLINES_URL}"
            f"?symbol={binance_symbol}&interval=1m"
            f"&startTime={start_ms}&limit=1"
        )

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            if not data:
                return None

            # data[0] = [open_time, open, high, low, close, volume, ...]
            open_price = float(data[0][1])
            logger.debug(
                f"[{symbol}] Binance kline open@{int(interval_start)}: ${open_price:,.4f}"
            )
            return open_price

        except Exception as e:
            logger.debug(f"Binance klines error {symbol}: {e}")
            return None

    def _parse_market(
        self,
        event: dict,
        mkt: dict,
        symbol: str = "BTC",
        interval_start: Optional[float] = None,
        rest_price: Optional[float] = None,
        slot_s: int = 300,
    ) -> Optional[Market]:
        """
        Convierte el JSON de Gamma API al modelo Market.
        """
        try:
            import datetime

            condition_id = str(mkt.get("conditionId", ""))
            if not condition_id:
                return None

            question = str(mkt.get("question", event.get("title", "")))

            # Token IDs — clobTokenIds[0]=Up, clobTokenIds[1]=Down
            raw_ids = mkt.get("clobTokenIds", "[]")
            if isinstance(raw_ids, str):
                clob_ids = json.loads(raw_ids)
            else:
                clob_ids = raw_ids

            if len(clob_ids) < 2:
                return None

            # Verifica outcomes para mapear Up/Down correctamente
            raw_outcomes = mkt.get("outcomes", '["Up", "Down"]')
            if isinstance(raw_outcomes, str):
                outcomes = json.loads(raw_outcomes)
            else:
                outcomes = raw_outcomes

            if str(outcomes[0]).lower() in ("up", "yes", "true"):
                token_id_up = str(clob_ids[0])
                token_id_down = str(clob_ids[1])
            else:
                token_id_up = str(clob_ids[1])
                token_id_down = str(clob_ids[0])

            # Tiempo de cierre
            end_date_str = mkt.get("endDate") or event.get("endDate", "")
            if not end_date_str:
                return None
            end_dt = datetime.datetime.fromisoformat(
                str(end_date_str).replace("Z", "+00:00")
            )
            end_time = end_dt.timestamp()

            if end_time <= time.time():
                return None

            # Calcular interval_start si no se pasó desde _fetch_event_by_slug
            # Prioridad: event.startTime > end_time - slot_s (usando slot_s autoritativo del slug)
            if interval_start is None:
                start_time_str = event.get("startTime") or event.get("startDate", "")
                if start_time_str:
                    try:
                        start_dt = datetime.datetime.fromisoformat(
                            str(start_time_str).replace("Z", "+00:00")
                        )
                        interval_start = start_dt.timestamp()
                    except Exception:
                        pass
                if interval_start is None:
                    # Usar slot_s del slug — CORRECTO para 5m (300) y 15m (900)
                    interval_start = end_time - slot_s

            # Precio de referencia (strike):
            # 1. Binance REST klines open (más preciso, solo si mercado ya inició)
            # 2. Binance WebSocket snapshot histórico
            # 3. Precio spot actual como fallback
            if rest_price is not None and rest_price > 0:
                reference_price = rest_price
            else:
                reference_price = self._get_reference_price(interval_start, symbol)

            return Market(
                market_id=condition_id,
                question=question,
                token_id_yes=token_id_up,
                token_id_no=token_id_down,
                reference_price=reference_price,
                end_time=end_time,
                status=MarketStatus.ACTIVE,
                symbol=symbol,
                direction="above",
                interval_start=interval_start,
                slot_seconds=slot_s,
            )

        except Exception as e:
            logger.debug(f"Error parseando mercado: {e}")
            return None

    def _get_reference_price(self, interval_start: float, symbol: str = "BTC") -> float:
        """
        Fallback: obtiene precio aproximado via WebSocket snapshot o spot actual.
        Solo se usa cuando Binance REST no está disponible (mercado futuro).
        """
        age = time.time() - interval_start

        # Intenta obtener precio histórico del WebSocket snapshot
        if self._price_history_fn is not None:
            try:
                historical = self._price_history_fn(symbol, interval_start)
            except TypeError:
                historical = self._price_history_fn(interval_start) if symbol == "BTC" else None
            if historical is not None:
                return historical

        # Precio spot actual como último recurso
        fn = self._spot_price_fns.get(symbol)
        spot = fn() if fn else None
        if spot is not None:
            if age > 30:
                logger.debug(
                    f"[{symbol}] Usando spot actual ${spot:,.2f} "
                    f"(mercado inició hace {age:.0f}s — estimación)"
                )
            return spot

        logger.warning(f"[{symbol}] Sin precio disponible — fallback 70000")
        return 70000.0
