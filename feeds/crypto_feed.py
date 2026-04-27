"""
Feed de precios crypto en tiempo real via Binance WebSocket.
Soporta BTC, ETH, SOL.

Snapshot de precio al inicio de cada intervalo de 5 minutos,
igual que Chainlink — así el strike del bot coincide con el real.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import websockets

SYMBOLS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt"]
INTERVAL = 300          # 5 minutos en segundos
KEEP_SLOTS = 12         # guardar los últimos 60 min (12 × 5min)


class CryptoFeed:
    def __init__(self) -> None:
        self._prices: dict[str, float] = {}            # symbol → último precio
        self._last_update: dict[str, float] = {}
        self._slot_prices: dict[str, dict[int, float]] = {}  # symbol → {slot_ts → price}
        self._current_slot: int = 0
        self._running = False

    # ── Precio actual ─────────────────────────────────────────
    def get_price(self, symbol: str) -> Optional[float]:
        """Último precio recibido."""
        return self._prices.get(symbol.upper())

    def is_stale(self, symbol: str, max_age_s: float = 3.0) -> bool:
        last = self._last_update.get(symbol.upper(), 0)
        return (time.time() - last) > max_age_s

    # ── Precio al inicio de un slot de 5 min ─────────────────
    def get_slot_price(self, symbol: str, slot_ts: int) -> Optional[float]:
        """
        Devuelve el precio registrado al inicio del slot (unix_ts múltiplo de 300).
        Esto aproxima el precio Chainlink que usa Polymarket como strike.
        Si no tenemos ese slot, devuelve el precio actual como fallback.
        """
        sym = symbol.upper()
        slot = (slot_ts // INTERVAL) * INTERVAL  # normalizar
        slot_map = self._slot_prices.get(sym, {})
        return slot_map.get(slot) or self._prices.get(sym)

    # ── Loop principal ─────────────────────────────────────────
    async def run(self) -> None:
        self._running = True
        streams = "/".join(f"{s}@trade" for s in SYMBOLS)
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    print(f"[CRYPTO] Conectado a Binance ({len(SYMBOLS)} pares)")
                    async for raw in ws:
                        if not self._running:
                            break
                        data = json.loads(raw)
                        stream = data.get("stream", "")
                        tick = data.get("data", {})
                        if not stream or not tick:
                            continue
                        symbol = stream.split("@")[0].replace("usdt", "").upper()
                        price = float(tick.get("p", 0))
                        if price <= 0:
                            continue

                        now = time.time()
                        self._prices[symbol] = price
                        self._last_update[symbol] = now

                        # Detectar inicio de nuevo slot de 5 min
                        current_slot = int(now // INTERVAL) * INTERVAL
                        if current_slot != self._current_slot:
                            self._current_slot = current_slot
                            self._snapshot_slot(current_slot)

            except Exception as e:
                if self._running:
                    print(f"[CRYPTO] Reconectando... ({e})")
                    await asyncio.sleep(2)

    def _snapshot_slot(self, slot_ts: int) -> None:
        """Guarda el precio actual de todos los símbolos al inicio del slot."""
        for sym, price in self._prices.items():
            if sym not in self._slot_prices:
                self._slot_prices[sym] = {}
            self._slot_prices[sym][slot_ts] = price

            # Limpiar slots viejos (solo mantener últimos KEEP_SLOTS)
            slots = sorted(self._slot_prices[sym].keys())
            if len(slots) > KEEP_SLOTS:
                for old in slots[:-KEEP_SLOTS]:
                    del self._slot_prices[sym][old]

        # Log para debug
        prices_str = " | ".join(
            f"{s}=${self._prices[s]:,.4f}" if s in ("XRP", "BNB") else f"{s}=${self._prices[s]:,.2f}"
            for s in ("BTC", "ETH", "SOL", "XRP", "BNB")
            if s in self._prices
        )
        import datetime
        dt = datetime.datetime.utcfromtimestamp(slot_ts).strftime("%H:%M")
        print(f"[CRYPTO] Snapshot slot {dt}UTC → {prices_str}")

    def stop(self) -> None:
        self._running = False
