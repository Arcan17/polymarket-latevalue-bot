"""
Orderbook feed for Polymarket CLOB markets.

Subscribes to the CLOB WebSocket for real-time orderbook updates.
Maintains the current best bid/ask per token.

Each market has two tokens (YES, NO). We maintain separate orderbooks.

WebSocket protocol:
  - Connect to: wss://ws-subscriptions-clob.polymarket.com/ws/market
  - Subscribe with: {"type": "subscribe_market", "assets": [token_id, ...]}
  - Receive: price_change, book events

NOTE: The WS message format is based on Polymarket's documented API.
      If messages arrive in a different schema, update _process_message().
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from config.settings import settings
from data.models import OrderbookLevel, OrderbookSnapshot
from utils.logger import get_logger

logger = get_logger(__name__)


class OrderbookFeed:
    """
    Maintains live orderbook snapshots for a set of token IDs.

    Usage:
        feed = OrderbookFeed()
        feed.subscribe(token_id_yes)
        feed.subscribe(token_id_no)
        asyncio.create_task(feed.run())
        ...
        book = feed.get_book(token_id)
    """

    def __init__(self) -> None:
        self._books: dict[str, OrderbookSnapshot] = {}
        self._subscribed: set[str] = set()
        self._running = False
        self._reconnect_delay = 1.0
        self._ws = None
        self._pending_subscriptions: set[str] = set()
        self._last_message_time: float = time.time()
        self._STALE_TIMEOUT = 30.0  # reconectar si no llega nada en 30s
        self._RESUB_INTERVAL = 60.0  # re-suscribir cada 60s para refrescar snapshots

    def subscribe(self, token_id: str) -> None:
        self._subscribed.add(token_id)
        self._pending_subscriptions.add(token_id)

    def unsubscribe(self, token_id: str) -> None:
        self._subscribed.discard(token_id)
        self._books.pop(token_id, None)

    def get_book(self, token_id: str) -> Optional[OrderbookSnapshot]:
        return self._books.get(token_id)

    def is_stale(self, token_id: str, max_age_s: float = 5.0) -> bool:
        book = self._books.get(token_id)
        if book is None:
            return True
        return (time.time() - book.timestamp) > max_age_s

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                logger.info("OrderbookFeed cancelled.")
                break
            except Exception as e:
                logger.error(
                    f"OrderbookFeed error: {e}. Reconnecting in {self._reconnect_delay}s"
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)
            else:
                self._reconnect_delay = 1.0

    async def _connect(self) -> None:
        import websockets  # type: ignore

        logger.info("OrderbookFeed connecting...")
        async with websockets.connect(
            settings.clob_ws_url,
            ping_interval=20,  # enviar ping cada 20s para mantener la conexión viva
            ping_timeout=10,  # reconectar si no hay pong en 10s
        ) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0
            self._last_message_time = time.time()

            # Subscribe to all known tokens
            all_tokens = list(self._subscribed)
            if all_tokens:
                await self._send_subscribe(ws, all_tokens)
            self._pending_subscriptions.clear()

            logger.info(
                f"OrderbookFeed connected, subscribed to {len(all_tokens)} tokens."
            )

            # Task 1: flush pending subscriptions cuando no llegan mensajes
            async def _subscription_flusher():
                last_resub = time.time()
                while self._running:
                    await asyncio.sleep(1.0)
                    # Suscribir tokens nuevos
                    if self._pending_subscriptions:
                        new_tokens = list(self._pending_subscriptions)
                        self._pending_subscriptions.clear()
                        try:
                            await self._send_subscribe(ws, new_tokens)
                        except Exception:
                            pass
                    # Re-suscribir todos cada 60s para forzar snapshots frescos
                    if time.time() - last_resub > self._RESUB_INTERVAL:
                        all_subs = list(self._subscribed)
                        if all_subs:
                            try:
                                await self._send_subscribe(ws, all_subs)
                                logger.debug(
                                    f"OrderbookFeed: re-suscripción periódica ({len(all_subs)} tokens)"
                                )
                            except Exception:
                                pass
                        last_resub = time.time()

            # Task 2: watchdog — reconectar si no llegan mensajes en STALE_TIMEOUT segundos
            async def _watchdog():
                while self._running:
                    await asyncio.sleep(10.0)
                    silence = time.time() - self._last_message_time
                    if silence > self._STALE_TIMEOUT:
                        logger.warning(
                            f"OrderbookFeed: sin mensajes por {silence:.0f}s — reconectando..."
                        )
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        return  # sale de _connect → el loop de run() reconecta

            flusher = asyncio.create_task(_subscription_flusher())
            watchdog = asyncio.create_task(_watchdog())
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    self._last_message_time = (
                        time.time()
                    )  # cualquier mensaje = conexión viva
                    try:
                        msg = json.loads(raw)
                        self._process_message(msg)
                    except Exception as e:
                        logger.debug(f"OrderbookFeed parse error: {e}")
            finally:
                flusher.cancel()
                watchdog.cancel()

    async def _send_subscribe(self, ws, token_ids: list[str]) -> None:
        # Polymarket CLOB WS subscription format (verified April 2026):
        # {"assets_ids": ["token_id", ...], "type": "market"}
        payload = json.dumps(
            {
                "assets_ids": token_ids,
                "type": "market",
            }
        )
        await ws.send(payload)
        logger.debug(f"Subscribed to {len(token_ids)} tokens")

    def _process_message(self, msg: dict) -> None:
        """
        Handle CLOB WebSocket messages (format verified April 2026).

        Book message:
          {"event_type": "book", "asset_id": "...", "bids": [...], "asks": [...], ...}

        Price-change message:
          {"event_type": "price_change", "market": "...", "price_changes": [
              {"asset_id": "...", "side": "BUY"|"SELL", "price": "0.10", "size": "100"},
              ...
          ]}

        Each entry in price_changes with size=0 means remove that level.
        """
        msg_type = msg.get("event_type") or msg.get("type", "")

        if msg_type == "book":
            # Full book snapshot — bids/asks are lists of {price, size} dicts
            token_id = str(msg.get("asset_id", ""))
            if not token_id:
                return
            bids = self._parse_levels(msg.get("bids", []))
            asks = self._parse_levels(msg.get("asks", []))
            # Sort: bids descending (best bid first), asks ascending (best ask first)
            bids.sort(key=lambda l: -l.price)
            asks.sort(key=lambda l: l.price)
            self._books[token_id] = OrderbookSnapshot(
                token_id=token_id,
                bids=bids,
                asks=asks,
                timestamp=time.time(),
            )
            logger.debug(
                f"Book snapshot {token_id[:10]}: "
                f"best_bid={bids[0].price:.3f} best_ask={asks[0].price:.3f}"
                if bids and asks
                else f"Book snapshot {token_id[:10]}: empty"
            )

        elif msg_type == "price_change":
            # Incremental updates — each change has its own asset_id
            price_changes = msg.get("price_changes", [])
            for change in price_changes:
                token_id = str(change.get("asset_id", ""))
                if not token_id or token_id not in self._books:
                    continue
                side = (change.get("side") or "").upper()
                try:
                    price = float(change["price"])
                    size = float(change["size"])
                except (KeyError, ValueError):
                    continue
                book = self._books[token_id]
                if side == "BUY":
                    book.bids = [l for l in book.bids if l.price != price]
                    if size > 0:
                        book.bids.append(OrderbookLevel(price=price, size=size))
                        book.bids.sort(key=lambda l: -l.price)
                elif side == "SELL":
                    book.asks = [l for l in book.asks if l.price != price]
                    if size > 0:
                        book.asks.append(OrderbookLevel(price=price, size=size))
                        book.asks.sort(key=lambda l: l.price)
                book.timestamp = time.time()

    def _parse_levels(self, raw_levels: list) -> list[OrderbookLevel]:
        """
        Parse raw level list into OrderbookLevel objects.
        Polymarket sends: [{"price": "0.65", "size": "100"}, ...]
        """
        levels = []
        for lvl in raw_levels:
            try:
                price = float(lvl["price"])
                size = float(lvl["size"])
                if size > 0:
                    levels.append(OrderbookLevel(price=price, size=size))
            except (KeyError, ValueError):
                continue
        return levels

    def inject_rest_book(
        self, token_id: str, ask: float | None, bid: float | None
    ) -> None:
        """
        Almacena precios obtenidos por REST cuando el WS aún no tiene snapshot.
        El WS sobreescribirá automáticamente cuando llegue un mensaje real.
        Solo inyecta si no hay datos WS frescos (< 5s).
        """
        existing = self._books.get(token_id)
        if existing and (time.time() - existing.timestamp) < 5.0:
            return  # WS tiene datos frescos, no sobreescribir
        asks = [OrderbookLevel(price=ask, size=1.0)] if ask else []
        bids = [OrderbookLevel(price=bid, size=1.0)] if bid else []
        self._books[token_id] = OrderbookSnapshot(
            token_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )

    def stop(self) -> None:
        self._running = False
