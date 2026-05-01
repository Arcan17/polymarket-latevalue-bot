"""
Feed de precios Chainlink via Polymarket RTDS WebSocket.

wss://ws-live-data.polymarket.com  — topic: crypto_prices_chainlink
Es el MISMO feed que Polymarket usa para resolver los mercados BTC/ETH/SOL Up/Down.
Sin autenticación. Actualiza ~1 vez por segundo.

Mensaje recibido:
{
  "topic": "crypto_prices_chainlink",
  "type": "update",
  "timestamp": 1776289712281,
  "payload": {
    "symbol": "btc/usd",
    "timestamp": 1776289711000,
    "value": 74748.58,
    "full_accuracy_value": "74748580000000000000000"
  }
}
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Optional

import websockets

RTDS_URL = "wss://ws-live-data.polymarket.com"

SUBSCRIPTION = {
    "action": "subscribe",
    "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}],
}

# Mapeo symbol de Polymarket → symbol interno del bot
SYMBOL_MAP = {
    "btc/usd": "BTC",
    "eth/usd": "ETH",
    "sol/usd": "SOL",
    "xrp/usd": "XRP",
    "bnb/usd": "BNB",
}

HISTORY_SECONDS = 1800  # guardar 30 min de historial — cubre el inicio de mercados 15m con margen holgado


class RTDSFeed:
    """
    Feed Chainlink en tiempo real vía Polymarket RTDS WebSocket.
    Fuente de verdad para decisiones de trading.
    """

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._last_update: dict[str, float] = {}
        self._history: dict[str, deque] = {}
        self._running = False
        self._last_message_time: float = time.time()

    # ── Precio actual ──────────────────────────────────────────────
    def get_price(self, symbol: str) -> Optional[float]:
        """Último precio Chainlink recibido."""
        return self._prices.get(symbol.upper())

    def is_stale(self, symbol: str, max_age_s: float = 5.0) -> bool:
        """True si el precio no se actualizó en los últimos max_age_s segundos."""
        last = self._last_update.get(symbol.upper(), 0)
        return (time.time() - last) > max_age_s

    def get_price_before(self, symbol: str, timestamp: float) -> Optional[float]:
        """
        Último precio Chainlink cuyo timestamp sea <= timestamp dado.
        Esto es exactamente lo que Polymarket usa para el "Precio a superar":
        el último precio Chainlink registrado ANTES de o EN el inicio del intervalo.

        Retorna None si no hay ningún precio antes del timestamp en el historial.
        """
        result = self.get_price_before_with_ts(symbol, timestamp)
        return result[1] if result else None

    def get_price_before_with_ts(
        self, symbol: str, timestamp: float
    ) -> Optional[tuple[float, float]]:
        """
        Último precio Chainlink cuyo chainlink_ts sea <= timestamp.
        Retorna (chainlink_ts, price) o None.
        Usado para saber exactamente qué tick usó Polymarket como strike.
        """
        sym = symbol.upper()
        history = self._history.get(sym)
        if not history:
            return None

        best: Optional[tuple[float, float]] = None
        for chainlink_ts, price, _recv in history:
            if chainlink_ts <= timestamp:
                best = (chainlink_ts, price)  # deque ordenado → el último válido gana
            else:
                break  # pasamos el timestamp, no hay más candidatos

        return best

    def get_price_after(self, symbol: str, timestamp: float) -> Optional[float]:
        """
        Primer precio Chainlink cuyo chainlink_ts sea estrictamente > timestamp.

        Este es el precio que Polymarket usa como 'chainlink_inicio': el primer tick
        de Chainlink DESPUÉS del inicio del intervalo. Retorna None si no hay ningún
        tick post-timestamp en el historial (intervalo recién comenzado o RTDS sin datos).
        """
        result = self.get_price_after_with_ts(symbol, timestamp)
        return result[1] if result else None

    def get_price_after_with_ts(
        self, symbol: str, timestamp: float
    ) -> Optional[tuple[float, float]]:
        """
        Primer precio Chainlink cuyo chainlink_ts sea estrictamente > timestamp.
        Retorna (chainlink_ts, price) para que el llamador pueda verificar la
        antigüedad del tick (cuántos segundos después del inicio llegó).

        Si el bot arrancó mediado un intervalo, el primer tick disponible puede
        tener chainlink_ts muy posterior al inicio real — eso indica un strike
        potencialmente incorrecto; el llamador debe usar Binance REST en su lugar.
        """
        sym = symbol.upper()
        history = self._history.get(sym)
        if not history:
            return None

        for chainlink_ts, price, _recv in history:
            if chainlink_ts > timestamp:
                return chainlink_ts, price  # deque ordenado → primer candidato

        return None

    def get_price_at(
        self, symbol: str, timestamp: float, max_delta_s: float = 15.0
    ) -> Optional[float]:
        """
        Precio Chainlink más cercano al timestamp dado (por timestamp de Chainlink).
        Fallback cuando get_price_before no encuentra datos.
        """
        sym = symbol.upper()
        history = self._history.get(sym)
        if not history:
            return None

        best_price = None
        best_delta = float("inf")
        for chainlink_ts, price, _recv in history:
            delta = abs(chainlink_ts - timestamp)
            if delta < best_delta:
                best_delta = delta
                best_price = price

        if best_delta <= max_delta_s:
            return best_price
        return None

    # ── Volatilidad reciente ───────────────────────────────────────
    def get_vol_30s(self, symbol: str) -> float:
        """
        Retorna la variación máxima de precio en los últimos 30 segundos
        como fracción del precio actual (ej: 0.003 = 0.3%).
        Usado para filtrar entradas en momentos de spike.
        """
        sym = symbol.upper()
        history = self._history.get(sym)
        if not history or len(history) < 2:
            return 0.0

        cutoff = time.time() - 30.0
        # historial: (chainlink_ts, price, reception_time) — usar reception_time para ventana
        recent = [
            (chainlink_ts, p) for chainlink_ts, p, recv in history if recv >= cutoff
        ]
        if len(recent) < 2:
            return 0.0

        prices = [p for _, p in recent]
        hi, lo = max(prices), min(prices)
        if lo <= 0:
            return 0.0
        return (hi - lo) / lo

    # ── Loop principal ─────────────────────────────────────────────
    async def run(self) -> None:
        self._running = True
        self._last_message_time = time.time()
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                if self._running:
                    print(f"[RTDS] Reconectando... ({e})")
                    await asyncio.sleep(2)

    async def _connect(self) -> None:
        """Conexión con watchdog — reconecta si >30s sin mensajes."""
        STALE_TIMEOUT = 30.0

        async with websockets.connect(
            RTDS_URL,
            ping_interval=20,
            ping_timeout=30,
            additional_headers={"Origin": "https://polymarket.com"},
        ) as ws:
            await ws.send(json.dumps(SUBSCRIPTION))
            self._last_message_time = time.time()
            print("[RTDS] Conectado — feed Chainlink BTC/ETH/SOL activo")

            # Watchdog: reconecta si lleva >30s sin ningún mensaje
            async def _watchdog():
                while self._running:
                    await asyncio.sleep(10.0)
                    silence = time.time() - self._last_message_time
                    if silence > STALE_TIMEOUT:
                        print(
                            f"[RTDS] ⚠ Sin mensajes por {silence:.0f}s — reconectando..."
                        )
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        return

            watchdog = asyncio.create_task(_watchdog())
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    self._last_message_time = time.time()
                    try:
                        msg = json.loads(raw)
                        if (
                            msg.get("topic") == "crypto_prices_chainlink"
                            and msg.get("type") == "update"
                        ):
                            self._handle_update(msg)
                    except Exception:
                        pass
            finally:
                watchdog.cancel()

    def _handle_update(self, msg: dict) -> None:
        payload = msg.get("payload", {})
        raw_sym = payload.get("symbol", "")
        sym = SYMBOL_MAP.get(raw_sym.lower())
        if not sym:
            return

        price = payload.get("value")
        if not price or price <= 0:
            return

        price = float(price)
        now = time.time()

        # payload.timestamp = cuándo Chainlink registró este precio (ms)
        # Usamos ese timestamp para poder buscar el precio exacto al inicio del intervalo
        chainlink_ts_ms = payload.get("timestamp")
        chainlink_ts = (chainlink_ts_ms / 1000.0) if chainlink_ts_ms else now

        self._prices[sym] = price
        self._last_update[sym] = now

        # Historial: (chainlink_timestamp, price, reception_time)
        # chainlink_ts es cuándo Chainlink lo midió — más preciso que time.time()
        if sym not in self._history:
            self._history[sym] = deque()
        hist = self._history[sym]
        hist.append((chainlink_ts, price, now))

        # Limpiar entradas viejas (> HISTORY_SECONDS)
        cutoff = now - HISTORY_SECONDS
        while hist and hist[0][2] < cutoff:
            hist.popleft()

    def stop(self) -> None:
        self._running = False
