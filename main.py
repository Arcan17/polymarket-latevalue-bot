"""
Late Value Bot — entra en mercados BTC Up/Down de Polymarket
cuando el precio del mercado está rezagado respecto al precio real de BTC.

Estrategia:
  - Monitorea mercados BTC Up/Down con <90s al vencimiento
  - Calcula P(YES) con Black-Scholes y BTC spot real (Binance)
  - Si edge > 12%: compra el lado favorable con $1
  - Mantiene hasta vencimiento — Polymarket paga $1 si ganas
  - Kill switch: para si pierde $5 en el día
"""

from __future__ import annotations

import asyncio
import json
import signal
import time

from config.settings import settings
from data.models import SessionStats
from feeds.crypto_feed import CryptoFeed
from feeds.rtds_feed import RTDSFeed
from feeds.market_discovery import MarketDiscovery
from feeds.orderbook_feed import OrderbookFeed
from strategy.evaluator import Evaluator
from strategy.vol_estimator import make_vol_estimator
from execution.executor import PaperExecutor, LiveExecutor
from telegram_notifier import TelegramNotifier

STATE_FILE = "/tmp/latevalue_state.json"
STATS_FILE = (
    "/Users/bastian/Documents/polymarket_latevalue/stats.json"  # permanente, NO en /tmp
)
TRADES_FILE = "/Users/bastian/Documents/polymarket_latevalue/trades.log"  # historial completo de trades
PID_FILE = "/tmp/latevalue_bot.pid"  # evita instancias duplicadas

# Versión del bot — se actualiza con reset_version.py cuando hay cambios importantes.
# Se graba en cada trade para saber qué versión lo generó.
try:
    from pathlib import Path as _Path

    BOT_VERSION = _Path(__file__).parent.joinpath("VERSION").read_text().strip()
except Exception:
    BOT_VERSION = "unknown"


def _acquire_pid_lock() -> bool:
    """Retorna True si se pudo obtener el lock. False si ya hay otra instancia corriendo."""
    import os

    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            # Verificar si el proceso sigue vivo
            try:
                os.kill(old_pid, 0)  # signal 0 = solo verifica existencia
                print(
                    f"[ERROR] Ya hay una instancia corriendo (PID {old_pid}). Abortando."
                )
                print(f"        Para forzar: rm {PID_FILE} && python3 main.py")
                return False
            except (ProcessLookupError, PermissionError):
                pass  # proceso muerto — el PID file es obsoleto

        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as e:
        print(f"[WARN] No se pudo crear PID lock: {e}")
        return True  # continuar de todos modos


def _release_pid_lock() -> None:
    import os

    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(PID_FILE)
    except Exception:
        pass


class LateValueBot:

    def __init__(self) -> None:
        self.crypto_feed = CryptoFeed()
        # RTDSFeed: feed Chainlink exacto de Polymarket (misma fuente que usa para resolver)
        self.rtds_feed = RTDSFeed()
        self.ob_feed = OrderbookFeed()
        self.discovery = MarketDiscovery(
            spot_price_fns={
                "BTC": lambda: self._get_price("BTC"),
                "ETH": lambda: self._get_price("ETH"),
                "SOL": lambda: self._get_price("SOL"),
                "XRP": lambda: self._get_price("XRP"),
                "BNB": lambda: self._get_price("BNB"),
            },
            price_history_fn=lambda symbol, t: self.crypto_feed.get_slot_price(
                symbol, int(t)
            ),
            # _rtds_strike: primer tick Chainlink post-inicio, solo si llegó
            # en los primeros 30s (guard de antigüedad — descarta ticks de
            # arranque tardío que no reflejan el precio real al inicio).
            rtds_price_at_fn=lambda symbol, t: self._rtds_strike(symbol, t),
        )
        self.evaluator = Evaluator(
            vol_estimator=make_vol_estimator(
                self.rtds_feed,
                lookback_s=settings.vol_lookback_seconds,
            )
        )
        self.executor = (
            PaperExecutor() if settings.trading_mode == "paper" else LiveExecutor()
        )
        self.stats = self._load_stats()
        self._markets = {}  # market_id → Market
        self._entered = set()  # market_ids ya procesados
        self._entered_slots: dict[str, int] = (
            {}
        )  # slot_key → número de entradas en ese slot
        self._rest_book_last_fetch: dict[str, float] = (
            {}
        )  # token_id → timestamp último fetch REST
        self._running = False
        self._last_trades: list[dict] = []  # últimas 10 operaciones
        # Capital base: en live se obtiene de Polymarket al arrancar; en paper usa settings
        self._live_balance_usdc: float | None = (
            None  # balance real de Polymarket (live only)
        )
        self._session_pnl_offset: float = (
            self.stats.total_pnl
        )  # PnL acumulado antes de esta sesión
        # Strike candidates: market_id → (first_seen_ts, latest_price)
        # Esperamos 3s antes de confirmar para capturar el mismo tick que Polymarket
        self._strike_candidates: dict[str, tuple[float, float]] = {}
        self._api_strike_pending: dict[str, float] = (
            {}
        )  # market_id → timestamp del último intento
        self._startup_time: float = time.time()  # para warm-up al iniciar
        self._warmup_logged: bool = False  # evita spam en logs durante warm-up
        # Telegram
        self.tg = TelegramNotifier(
            token=getattr(settings, "telegram_bot_token", ""),
            chat_id=getattr(settings, "telegram_chat_id", ""),
        )
        # Verificación post-liquidación: market_id → datos para confirmar con API
        self._pending_settle_verify: dict[str, dict] = {}
        # Sesión HTTP persistente para REST book fetches — evita crear nueva TCP connection cada vez
        self._http_session: "aiohttp.ClientSession | None" = None
        # Control del write_state — no escribir más de 1 vez por segundo (dashboard refresca a 1Hz)
        self._last_state_write: float = 0.0
        # Tiempo del último pre-warm de books (para pre-calentar antes de la ventana de entrada)
        self._book_prewarm_ts: dict[str, float] = {}  # token_id → último prewarm

    def _fetch_live_balance(self) -> float | None:
        """Obtiene el balance USDC real de la cuenta de Polymarket (solo modo live)."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            result = self.executor._client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL, signature_type=2
                )
            )
            # El balance viene en micro-USDC (6 decimales)
            raw = result.get("balance", "0")
            return float(raw) / 1_000_000
        except Exception as e:
            print(f"[LIVE] No se pudo obtener balance: {e}")
            return None

    def _current_capital(self) -> float:
        """Capital actual — live usa balance real + PnL sesión, paper usa capital inicial + PnL total."""
        if self._live_balance_usdc is not None:
            return self._live_balance_usdc + (
                self.stats.total_pnl - self._session_pnl_offset
            )
        return settings.starting_capital_usdc + self.stats.total_pnl

    def _dynamic_bet_size(self) -> float:
        """
        Calcula el tamaño de apuesta según el capital actual.

        - auto_bet_sizing=false (default): siempre usa ORDER_SIZE_USDC del .env
        - auto_bet_sizing=true: apuesta BET_FRACTION% del capital actual
          redondeado a $0.50, entre BET_SIZE_MIN y BET_SIZE_MAX

        Ejemplo con $200 de capital y bet_fraction=0.024:
          $200 × 2.4% = $4.80 → redondeado a $5.00
        """
        if not settings.auto_bet_sizing:
            return settings.order_size_usdc

        capital = self._current_capital()
        raw = capital * settings.bet_fraction
        # Redondear a múltiplos de $0.50 para números limpios
        rounded = round(raw * 2) / 2
        # Aplicar límites
        bet = max(settings.bet_size_min, min(settings.bet_size_max, rounded))
        return bet

    def _get_price(self, symbol: str) -> float | None:
        """
        Precio principal: Chainlink vía RTDS (misma fuente que Polymarket para resolver).
        Fallback: Binance WebSocket si el RTDS está stale (>5s sin actualizar).
        """
        rtds_price = self.rtds_feed.get_price(symbol)
        if rtds_price and not self.rtds_feed.is_stale(symbol, max_age_s=10.0):
            return rtds_price
        # Fallback a Binance si RTDS no está disponible aún
        return self.crypto_feed.get_price(symbol)

    async def start(self) -> None:
        print("=" * 55)
        print(f"  LATE VALUE BOT {BOT_VERSION} | modo={settings.trading_mode.upper()}")
        print(
            f"  Capital: ${settings.starting_capital_usdc} | "
            f"Apuesta: ${settings.order_size_usdc} | "
            f"Min edge: {settings.min_edge:.0%}"
        )
        print("=" * 55)
        self.tg.bot_started(
            version=BOT_VERSION,
            mode=settings.trading_mode,
            capital=self._current_capital(),
            min_edge=settings.min_edge,
            dead_zone=settings.dead_zone_pct,
        )

        # Verificar conexión y obtener balance real en modo live
        if settings.trading_mode == "live":
            print("[LIVE] Verificando conexión con Polymarket...")
            if not self.executor._test_connection():
                print("[LIVE] ABORTANDO — credenciales inválidas o sin fondos")
                return

            # Obtener balance USDC real de la cuenta para mostrarlo en dashboard
            self._live_balance_usdc = self._fetch_live_balance()
            if self._live_balance_usdc is not None:
                print(f"[LIVE] Balance USDC en cuenta: ${self._live_balance_usdc:.2f}")
                if self._live_balance_usdc < settings.bet_size_min:
                    print(
                        f"[LIVE] ⚠ Balance bajo (${self._live_balance_usdc:.2f}) — "
                        f"mínimo para apostar ${settings.bet_size_min:.2f}"
                    )

            # Recuperar posiciones abiertas de sesión anterior (crash recovery)
            recovered = self.executor.load_persisted_positions()
            for pos in recovered:
                self._entered.add(pos.market_id)

            # Advertencia si Take-Profit está activo en live
            # EV(esperar vencimiento) > EV(TP) porque TP cobra fees dobles.
            # Recomendación: TAKE_PROFIT_PRICE=0.0 en live.
            if settings.take_profit_price > 0:
                print(
                    f"[LIVE] ⚠⚠ TAKE_PROFIT_PRICE={settings.take_profit_price} activo en live."
                )
                print(
                    f"[LIVE]    EV de esperar al vencimiento es mayor que TP (fees dobles)."
                )
                print(f"[LIVE]    Considera: TAKE_PROFIT_PRICE=0.0 en .env")

        self._running = True

        tasks = [
            asyncio.create_task(self.crypto_feed.run(), name="crypto_feed"),
            asyncio.create_task(self.rtds_feed.run(), name="rtds_feed"),
            asyncio.create_task(self.ob_feed.run(), name="ob_feed"),
            asyncio.create_task(self._main_loop(), name="main_loop"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _main_loop(self) -> None:
        import aiohttp

        # Sesión HTTP persistente — reutiliza conexión TCP para todos los REST book fetches
        # Evita el overhead de TCP handshake (~100-200ms) en cada llamada
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        self._http_session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=2),
        )
        interval = settings.loop_interval_ms / 1000.0

        # Esperar precios iniciales — primero RTDS (Chainlink), luego Binance como fallback
        ALL_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "BNB")
        for _ in range(40):
            prices = {
                s: self._get_price(s) for s in ALL_SYMBOLS[:3]
            }  # BTC/ETH/SOL obligatorios
            if all(prices.values()):
                break
            await asyncio.sleep(0.5)

        for sym in ALL_SYMBOLS:
            p = self._get_price(sym)
            rtds = self.rtds_feed.get_price(sym)
            if p:
                src = (
                    "RTDS/Chainlink"
                    if rtds and not self.rtds_feed.is_stale(sym)
                    else "Binance"
                )
                print(f"[PRICE] {sym}: ${p:,.2f} ({src})")
            else:
                print(f"[PRICE] {sym}: sin precio aún (se actualizará en segundos)")

        # Primer discovery de mercados
        await self._refresh_markets()

        last_refresh = time.time()

        while self._running:
            loop_start = time.time()

            try:
                # Kill switch
                if self.stats.daily_pnl <= -settings.max_daily_loss_usdc:
                    print(
                        f"[KILL] Pérdida diaria ${self.stats.daily_pnl:.2f} — detenido"
                    )
                    self.tg.kill_switch(
                        self.stats.daily_pnl, settings.max_daily_loss_usdc
                    )
                    break

                # Refresh mercados cada 30s
                if time.time() - last_refresh > 30:
                    await self._refresh_markets()
                    last_refresh = time.time()

                # Resumen diario Telegram a medianoche
                today_str = time.strftime("%Y-%m-%d")
                if time.strftime("%H:%M") == "00:00":
                    self.tg.daily_summary(
                        date_str=today_str,
                        wins=self.stats.bets_won,
                        losses=self.stats.bets_lost,
                        daily_pnl=self.stats.daily_pnl,
                        total_pnl=self.stats.total_pnl,
                        capital=self._current_capital(),
                    )

                # Capturar strikes de mercados que acaban de empezar
                self._capture_strikes()

                # Pre-calentar books antes de la ventana de entrada
                # Lanza fetches REST para mercados que están a 60s de entrar en ventana
                # Así cuando llega el momento de entrar el book ya está disponible
                prewarm_window = settings.entry_window_s + 60
                for market in list(self._markets.values()):
                    tte = market.seconds_to_expiry
                    if tte < prewarm_window and market.market_id not in self._entered:
                        asyncio.create_task(self._prewarm_books(market))

                # Liquidar vencidos
                await self._settle_expired()

                # Take-profit anticipado (salida antes del vencimiento)
                await self._check_take_profit()

                # Evaluar oportunidades
                await self._evaluate_markets()

                # Escribir estado para dashboard — throttle 1s (dashboard refresca a 1Hz)
                now_ts = time.time()
                if now_ts - self._last_state_write >= 1.0:
                    self._write_state()
                    self._last_state_write = now_ts

            except Exception as e:
                import traceback

                print(f"[ERROR] Loop exception: {e}")
                traceback.print_exc()

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _refresh_markets(self) -> None:
        new = await self.discovery.refresh()
        for m in new:
            self._markets[m.market_id] = m
            self.ob_feed.subscribe(m.token_id_yes)
            self.ob_feed.subscribe(m.token_id_no)
        # También sincronizar desde caché del discovery
        for m in self.discovery.active_markets:
            if m.market_id not in self._markets:
                self._markets[m.market_id] = m
                self.ob_feed.subscribe(m.token_id_yes)
                self.ob_feed.subscribe(m.token_id_no)

    # ── Helpers de precio ─────────────────────────────────────────────────────

    def _rtds_strike(
        self, symbol: str, interval_start: float, max_delay: float = 30.0
    ) -> "float | None":
        """
        Retorna el último tick Chainlink con timestamp <= interval_start.

        Esto es exactamente lo que Polymarket usa como "Precio a superar":
        el último precio Chainlink registrado ANTES DE o EN el inicio del intervalo.

        Retorna None solo si no hay ningún tick anterior al inicio en el
        historial (bot arrancó mediado el intervalo sin datos previos).
        En ese caso _capture_strikes delega a Binance REST.

        Ejemplo:
          Intervalo inicia 16:30:00 — último tick Chainlink: 16:29:59 = $75,647
          get_price_before → $75,647 ✓  (mismo precio que Polymarket muestra)
        """
        return self.rtds_feed.get_price_before(symbol, interval_start)

    # ──────────────────────────────────────────────────────────────────────────

    def _capture_strikes(self) -> None:
        """
        Captura el strike al inicio de cada intervalo.

        Prioridad:
          1. RTDS Chainlink — último tick ANTES del inicio del intervalo.
             Esto es exactamente lo que Polymarket usa como "Precio a superar".
             Disponible inmediatamente si el bot llevaba corriendo antes del inicio.
          2. Binance REST klines — open price de la vela 1min al inicio exacto.
             Se activa cuando no hay tick pre-inicio en historial (bot arrancó
             mediado el intervalo) y han pasado ≥60s para que la vela cierre.
          3. Precio spot actual — fallback provisional en los primeros 30s si
             no hay datos históricos (bot arrancó justo al inicio del intervalo).
        """
        now = time.time()
        SETTLE_DELAY = 3.0  # esperar 3s antes de confirmar con precio live (Caso 3)

        for market in list(self._markets.values()):
            if market.market_id in self.discovery._price_confirmed:
                continue

            # interval_start exacto del modelo (poblado desde event.startTime en market_discovery)
            # Para 5m: end_time - 300  |  Para 15m: end_time - 900
            interval_start = (
                market.interval_start
                if market.interval_start > 0
                else market.end_time - market.slot_seconds
            )
            elapsed = now - interval_start

            # Ventana máxima de captura = slot completo - 60s de margen
            # 5m → 240s  |  15m → 840s
            CAPTURE_WINDOW = market.slot_seconds - 60

            if elapsed < 0 or elapsed >= CAPTURE_WINDOW:
                self._strike_candidates.pop(market.market_id, None)
                continue

            mid = market.market_id
            rtds_stale = self.rtds_feed.is_stale(market.symbol, 10.0)
            rtds_price = self.rtds_feed.get_price(market.symbol)

            # ── Caso 0: historial RTDS disponible → último tick PRE-inicio ────
            # get_price_before(interval_start) es exactamente el precio que
            # Polymarket usa como "Precio a superar" (strike).
            # Retorna None solo si el bot arrancó sin datos previos al intervalo.
            hist_price = self._rtds_strike(market.symbol, interval_start)
            if hist_price:
                # Para el log: buscar cuántos segundos antes del inicio fue ese tick
                pre_result = self.rtds_feed.get_price_before_with_ts(
                    market.symbol, interval_start
                )
                age_s = (interval_start - pre_result[0]) if pre_result else 0.0
                old = market.reference_price
                market.reference_price = hist_price
                self.discovery._price_confirmed.add(mid)
                self._strike_candidates.pop(mid, None)
                print(
                    f"[STRIKE] {market.symbol} {market.question[-25:]} "
                    f"strike=${hist_price:,.2f} (Chainlink-RTDS ✓ -{age_s:.1f}s antes) "
                    f"Δ${hist_price - old:+.2f} vs estimado"
                )
                continue

            # ── Caso 1: tick tardío / mercado descubierto tarde / RTDS stale ──
            # Incluye el caso de arranque tardío donde _rtds_strike devolvió None.
            # Cooldown de 45s entre reintentos — permite que la vela Binance cierre
            # antes de que sea válida (la vela 1min necesita ≥60s desde interval_start).
            API_RETRY_COOLDOWN = 45.0
            if elapsed >= 30.0 or rtds_stale:
                last_attempt = self._api_strike_pending.get(mid, 0)
                if (now - last_attempt) >= API_RETRY_COOLDOWN:
                    self._api_strike_pending[mid] = now
                    asyncio.create_task(self._fetch_strike_from_api(market))
                continue

            # ── Caso 2: captura normal con RTDS en los primeros 3s ────────────
            # Fallback si historial aún no tiene datos (raro al inicio del bot)
            if not rtds_price:
                continue

            if mid not in self._strike_candidates:
                self._strike_candidates[mid] = (now, rtds_price)
                continue

            first_seen_ts, _ = self._strike_candidates[mid]
            self._strike_candidates[mid] = (first_seen_ts, rtds_price)

            if elapsed < SETTLE_DELAY:
                continue

            # Confirmar strike RTDS tick actual
            _, final_price = self._strike_candidates.pop(mid)
            old = market.reference_price
            market.reference_price = final_price
            self.discovery._price_confirmed.add(mid)
            print(
                f"[STRIKE] {market.symbol} {market.question[-25:]} "
                f"strike=${final_price:,.2f} "
                f"(Δ${final_price - old:+.2f} vs estimado)"
            )

    async def _fetch_strike_from_api(self, market) -> None:
        """
        Obtiene el strike correcto cuando _capture_strikes no puede usar RTDS.
        Casos: bot arrancó mediado el intervalo, RTDS stale, mercado descubierto tarde.

        Prioridad:
          1. Tick RTDS fresco (delay ≤30s) — misma fuente que Polymarket.
          2. Binance REST klines — open price real al inicio del intervalo.
             Diferencia típica vs Chainlink: < $10 en BTC, < $0.05 en SOL/XRP.
          3. Último tick pre-inicio (get_price_before) — al menos centrado en el tiempo.
          4. Precio spot actual — último recurso, puede estar alejado del inicio.
        """
        import aiohttp

        mid = market.market_id
        try:
            interval_start = (
                market.interval_start
                if market.interval_start > 0
                else market.end_time - market.slot_seconds
            )

            # ── 1. Tick RTDS fresco post-inicio (guard de antigüedad ≤30s) ────
            hist_price = self._rtds_strike(
                market.symbol, interval_start, max_delay=30.0
            )
            if hist_price:
                pre_result = self.rtds_feed.get_price_before_with_ts(
                    market.symbol, interval_start
                )
                age_s = (interval_start - pre_result[0]) if pre_result else 0.0
                old = market.reference_price
                market.reference_price = hist_price
                self.discovery._price_confirmed.add(mid)
                print(
                    f"[STRIKE] {market.symbol} {market.question[-25:]} "
                    f"strike=${hist_price:,.2f} (RTDS pre-inicio -{age_s:.1f}s ✓) "
                    f"Δ${hist_price - old:+.2f} vs estimado"
                )
                return

            # ── 2. Binance REST klines — precio real al inicio del intervalo ──
            # Usamos el open de la vela 1min que empieza en interval_start.
            # La vela debe estar cerrada (interval_start < ahora - 60s).
            BINANCE_SYMBOLS = {
                "BTC": "BTCUSDT",
                "ETH": "ETHUSDT",
                "SOL": "SOLUSDT",
                "XRP": "XRPUSDT",
            }
            binance_sym = BINANCE_SYMBOLS.get(market.symbol.upper())
            if binance_sym and interval_start < time.time() - 60:
                try:
                    start_ms = int(interval_start * 1000)
                    klines_url = (
                        f"https://api.binance.com/api/v3/klines"
                        f"?symbol={binance_sym}&interval=1m"
                        f"&startTime={start_ms}&limit=1"
                    )
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            klines_url, timeout=aiohttp.ClientTimeout(total=5)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if data:
                                    binance_price = float(data[0][1])  # open price
                                    old = market.reference_price
                                    market.reference_price = binance_price
                                    self.discovery._price_confirmed.add(mid)
                                    print(
                                        f"[STRIKE] {market.symbol} {market.question[-25:]} "
                                        f"strike=${binance_price:,.2f} (Binance-REST ✓) "
                                        f"Δ${binance_price - old:+.2f} vs estimado"
                                    )
                                    return
                except Exception:
                    pass  # continúa al siguiente fallback

            # ── 3. Último tick pre-inicio — mejor que nada ────────────────────
            pre_price = self.rtds_feed.get_price_before(market.symbol, interval_start)
            if pre_price:
                old = market.reference_price
                market.reference_price = pre_price
                self.discovery._price_confirmed.add(mid)
                print(
                    f"[STRIKE] {market.symbol} {market.question[-25:]} "
                    f"strike=${pre_price:,.2f} (pre-inicio ⚠️) "
                    f"Δ${pre_price - old:+.2f} vs estimado"
                )
                return

            # ── 4. Precio spot actual — NO confirmar ─────────────────────────
            # Si solo tenemos spot actual, actualizamos la estimación pero NO
            # marcamos como confirmado — el siguiente intento (45s cooldown)
            # intentará Binance REST de nuevo cuando la vela ya haya cerrado.
            current = self.rtds_feed.get_price(market.symbol)
            if current and not self.rtds_feed.is_stale(market.symbol, 5.0):
                old = market.reference_price
                market.reference_price = current
                # No llamar _price_confirmed.add → reintentará con Binance REST en 45s
                print(
                    f"[STRIKE] {market.symbol} {market.question[-25:]} "
                    f"strike=${current:,.2f} (spot provisional ⏳) "
                    f"— esperando Binance kline..."
                )

        except Exception:
            pass
        # No limpiar _api_strike_pending aquí — el timestamp queda para el cooldown de 45s

    async def _fetch_rest_book(
        self, token_id: str
    ) -> tuple[float | None, float | None]:
        """
        Fallback REST para obtener best_ask / best_bid cuando el WS aún no tiene snapshot.
        Usa sesión HTTP persistente (reutiliza TCP connection — ~100-200ms más rápido que
        crear nueva sesión cada vez).
        """
        try:
            session = self._http_session
            if session is None or session.closed:
                import aiohttp

                session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
            asks = data.get("asks", [])
            bids = data.get("bids", [])
            # Filtrar precios placeholder (0.01/0.99 = sin liquidez real)
            real_asks = [
                float(a["price"])
                for a in asks
                if float(a.get("price", 0)) > 0.02 and float(a.get("size", 0)) > 0
            ]
            real_bids = [
                float(b["price"])
                for b in bids
                if float(b.get("price", 0)) > 0.02 and float(b.get("size", 0)) > 0
            ]
            best_ask = min(real_asks) if real_asks else None
            best_bid = max(real_bids) if real_bids else None
            return best_ask, best_bid
        except Exception:
            return None, None

    async def _prewarm_books(self, market) -> None:
        """
        Pre-calienta el book de un mercado antes de entrar en la ventana de entrada.
        Se llama cuando T < entry_window_s + 60s para que los libros estén listos
        al entrar en la ventana real (< entry_window_s).
        Evita desperdiciar los primeros 100-300ms de la ventana esperando el REST fetch.
        """
        now_ts = time.time()
        for tid in (market.token_id_yes, market.token_id_no):
            book = self.ob_feed.get_book(tid)
            last_prewarm = self._book_prewarm_ts.get(tid, 0)
            # Solo si: no hay datos WS Y no hemos pre-calentado en los últimos 15s
            if (book is None or (now_ts - book.timestamp) > 8) and (
                now_ts - last_prewarm
            ) > 15:
                self._book_prewarm_ts[tid] = (
                    now_ts  # marcar antes del await para evitar duplicados
                )
                ask_r, bid_r = await self._fetch_rest_book(tid)
                if ask_r:
                    self.ob_feed.inject_rest_book(tid, ask_r, bid_r)
                    self._rest_book_last_fetch[tid] = now_ts

    async def _evaluate_markets(self) -> None:
        # Warm-up: esperar hasta el inicio del siguiente slot de 5 min después del arranque.
        # Así el RTDS tiene historial limpio desde el inicio del slot y el strike es fiable.
        SLOT_S = 300  # slots de 5 minutos
        SLOT_BUFFER = 15  # segundos extra tras el inicio del slot
        next_slot = (int(self._startup_time // SLOT_S) + 1) * SLOT_S
        wait_until = next_slot + SLOT_BUFFER

        if time.time() < wait_until:
            if not self._warmup_logged:
                slot_str = time.strftime("%H:%M:%S", time.localtime(next_slot))
                remaining = int(wait_until - time.time())
                print(
                    f"[BOT] ⏳ Warm-up: esperando al próximo slot ({slot_str} + {SLOT_BUFFER}s buffer) — {remaining}s restantes..."
                )
                self._warmup_logged = True
            return
        elif self._warmup_logged:
            print("[BOT] ✅ Warm-up completado — comenzando a evaluar mercados.")
            self._warmup_logged = False

        # Recopilar todas las oportunidades y entrar las mejores por intervalo.
        # Regla: máximo MAX_PER_SLOT trades por slot de 5 min, y solo 1 por símbolo/slot.
        # Permite capturar BTC + ETH + SOL en el mismo intervalo si todos tienen edge.
        MAX_PER_SLOT = 3  # máx 3 trades por intervalo (1 por cripto)
        candidates: list = []  # (opp, market)

        # Buffer de latencia al tiempo mínimo.
        # Live: una orden FOK tarda ~200-600ms en round-trip — necesitamos margen.
        # Paper: aplicamos el mismo buffer para simular condiciones reales de live.
        # Así los resultados de paper reflejan lo que pasaría con dinero real.
        latency_buffer = 0.6
        effective_min_time = settings.min_time_s + latency_buffer

        # Mínimo TTE basado en datos: TTE 30-60s tiene WR=62% y PnL negativo.
        # TTE <30s y TTE >60s son rentables. Bloqueamos la ventana mala.
        MIN_TTE_FILTER = 65.0  # no entrar entre 30-65s al vencimiento

        for market in list(self._markets.values()):
            tte = market.seconds_to_expiry

            # Solo en ventana de oportunidad
            if tte > settings.entry_window_s or tte < effective_min_time:
                continue

            # Filtro TTE 30-65s: zona de pérdidas demostrada en datos (WR=62%, PnL=-$3.25)
            # Fuera de esta ventana el bot tiene WR>79% consistente.
            if effective_min_time < tte < MIN_TTE_FILTER:
                continue

            # No repetir el mismo mercado
            if market.market_id in self._entered:
                continue

            # Precio spot: Chainlink vía RTDS (primario) o Binance (fallback)
            spot_price = self._get_price(market.symbol)
            if not spot_price:
                continue

            # Verificar que tenemos precio fresco
            rtds_ok = self.rtds_feed.get_price(
                market.symbol
            ) is not None and not self.rtds_feed.is_stale(market.symbol, 10.0)
            binance_ok = not self.crypto_feed.is_stale(market.symbol)
            if not rtds_ok and not binance_ok:
                continue

            # Corregir referencia si se guardó el fallback
            if market.reference_price == 70000.0 or market.reference_price <= 0:
                market.reference_price = spot_price

            # Obtener orderbook del WS
            book_yes = self.ob_feed.get_book(market.token_id_yes)
            book_no = self.ob_feed.get_book(market.token_id_no)

            yes_ask = book_yes.best_ask if book_yes else None
            yes_bid = book_yes.best_bid if book_yes else None
            no_ask = book_no.best_ask if book_no else None
            no_bid = book_no.best_bid if book_no else None

            # Fallback REST si el WS aún no tiene snapshot para este mercado.
            # Fetches YES y NO en PARALELO (asyncio.gather) → misma latencia que 1 fetch.
            # Rate-limit: máximo 1 fetch REST por token cada 8s.
            if yes_ask is None or no_ask is None:
                now_ts = time.time()
                tokens_to_fetch = []
                for tid in (market.token_id_yes, market.token_id_no):
                    book = self.ob_feed.get_book(tid)
                    book_age = (now_ts - book.timestamp) if book else 999
                    last_fetch = self._rest_book_last_fetch.get(tid, 0)
                    if book_age > 8 and (now_ts - last_fetch) > 8:
                        tokens_to_fetch.append(tid)
                        self._rest_book_last_fetch[tid] = (
                            now_ts  # marcar antes del await
                        )

                if tokens_to_fetch:
                    # Fetch todos los tokens pendientes en paralelo
                    results = await asyncio.gather(
                        *[self._fetch_rest_book(tid) for tid in tokens_to_fetch],
                        return_exceptions=True,
                    )
                    for tid, result in zip(tokens_to_fetch, results):
                        if isinstance(result, tuple) and result[0] is not None:
                            self.ob_feed.inject_rest_book(tid, result[0], result[1])

                # Re-leer tras inyección
                book_yes = self.ob_feed.get_book(market.token_id_yes)
                book_no = self.ob_feed.get_book(market.token_id_no)
                yes_ask = book_yes.best_ask if book_yes else None
                yes_bid = book_yes.best_bid if book_yes else None
                no_ask = book_no.best_ask if book_no else None
                no_bid = book_no.best_bid if book_no else None

            if yes_ask is None and no_ask is None:
                continue

            # Detectar si el book vino de REST o WS (para auditoría)
            now_ts2 = time.time()
            yes_rest = (
                now_ts2 - self._rest_book_last_fetch.get(market.token_id_yes, 0)
            ) < 10
            no_rest = (
                now_ts2 - self._rest_book_last_fetch.get(market.token_id_no, 0)
            ) < 10
            book_src = "REST" if (yes_rest or no_rest) else "WS"

            # Strike confirmado via Chainlink o solo estimado
            strike_ok = market.market_id in self.discovery._price_confirmed

            # Bloquear entrada si el strike no está confirmado
            if not strike_ok:
                continue

            # Si RTDS stale, necesitamos al menos Binance fresco como fallback
            # (spot_price ya usa Binance como fallback en _get_price)
            if self.rtds_feed.is_stale(market.symbol, max_age_s=10.0):
                if self.crypto_feed.is_stale(market.symbol):
                    continue  # ni RTDS ni Binance — sin precio confiable
                # RTDS stale pero Binance OK → seguir con precio Binance

            # Volatilidad reciente
            vol_30s = self.rtds_feed.get_vol_30s(market.symbol)

            # Evaluar edge
            opp = self.evaluator.evaluate(
                market,
                spot_price,
                yes_ask,
                no_ask,
                yes_bid=yes_bid,
                no_bid=no_bid,
                vol_30s=vol_30s,
            )
            if opp:
                # ── Guardia de libro stale ────────────────────────────────────
                # Edge >35% con token a 10-20¢ = casi siempre libro WS desactualizado.
                # En live esa orden FOK sería rechazada (precio ya se movió).
                # Validar con REST antes de entrar: si el ask real está muy diferente
                # al WS, la oportunidad no existe y el trade no llenaría.
                if opp.edge > settings.max_edge:
                    now_ts3 = time.time()
                    print(
                        f"[MAX_EDGE] {market.symbol} edge={opp.edge:.1%} > {settings.max_edge:.0%} "
                        f"— validando con REST (posible libro WS stale)..."
                    )
                    fresh_ask, fresh_bid = await self._fetch_rest_book(opp.token_id)
                    if fresh_ask is not None:
                        self.ob_feed.inject_rest_book(
                            opp.token_id, fresh_ask, fresh_bid
                        )
                        self._rest_book_last_fetch[opp.token_id] = now_ts3
                        # Re-leer books actualizados
                        book_yes = self.ob_feed.get_book(market.token_id_yes)
                        book_no = self.ob_feed.get_book(market.token_id_no)
                        yes_ask2 = book_yes.best_ask if book_yes else None
                        yes_bid2 = book_yes.best_bid if book_yes else None
                        no_ask2 = book_no.best_ask if book_no else None
                        no_bid2 = book_no.best_bid if book_no else None
                        # Re-evaluar con precio fresco
                        opp = self.evaluator.evaluate(
                            market,
                            spot_price,
                            yes_ask2,
                            no_ask2,
                            yes_bid=yes_bid2,
                            no_bid=no_bid2,
                            vol_30s=vol_30s,
                        )
                        if opp is None:
                            print(
                                f"[MAX_EDGE] ⛔ Libro WS era stale "
                                f"(ask WS={yes_ask or no_ask:.3f} → REST={fresh_ask:.3f}) — descartado."
                            )
                        else:
                            print(
                                f"[MAX_EDGE] ✅ Edge real confirmado: {opp.edge:.1%} "
                                f"(ask REST={fresh_ask:.3f})"
                            )
                    else:
                        # No se pudo obtener REST → descartar por precaución
                        print(
                            f"[MAX_EDGE] ⚠ No se pudo obtener REST para validar "
                            f"edge={opp.edge:.1%} — descartado."
                        )
                        opp = None

                # Hard cap: bloquear solo cuando el token está casi sin valor (<0.25)
                # y el edge es absurdamente alto — señal de modelo roto en precio extremo.
                # Si REST confirmó edge >50% con precio >0.25 → es una oportunidad real
                # (libro stale que no actualizó, no modelo roto).
                HARD_MAX_EDGE = 0.65
                MIN_PRICE_FOR_HIGH_EDGE = 0.25
                if opp and opp.edge > HARD_MAX_EDGE:
                    print(
                        f"[MAX_EDGE] 🚫 Hard cap: edge={opp.edge:.1%} > {HARD_MAX_EDGE:.0%} "
                        f"— descartado (precio={opp.market_price:.2f} inusual)."
                    )
                    opp = None
                elif (
                    opp
                    and opp.edge > 0.50
                    and opp.market_price < MIN_PRICE_FOR_HIGH_EDGE
                ):
                    print(
                        f"[MAX_EDGE] 🚫 Token barato: precio={opp.market_price:.2f} < {MIN_PRICE_FOR_HIGH_EDGE} "
                        f"con edge={opp.edge:.1%} — modelo poco confiable en zona extrema."
                    )
                    opp = None

                # Filtro precio bajo: tokens < 0.45 tienen WR=50% con edge <30%.
                # Solo entrar en tokens baratos si el edge es suficientemente alto (>30%)
                # para compensar la menor fiabilidad del modelo en esa zona de precio.
                if opp and opp.market_price < 0.45 and opp.edge < 0.30:
                    print(
                        f"[SKIP] {market.symbol} precio bajo ep={opp.market_price:.2f} "
                        f"edge={opp.edge:.1%} < 30% — WR insuficiente en esta zona."
                    )
                    opp = None

                if opp:
                    candidates.append((opp, market, book_src, strike_ok, vol_30s))

        # Ordenar por edge descendente — entra primero la más rentable
        candidates.sort(key=lambda x: x[0].edge, reverse=True)

        for opp, market, book_src, strike_ok, vol_30s_entry in candidates:
            slot = int(market.end_time // 300) * 300
            slot_key = f"{slot}"  # clave de slot (tiempo)
            sym_slot = f"{market.symbol}_{slot}"  # clave símbolo+slot (no repetir misma cripto)

            # No repetir el mismo símbolo en el mismo slot
            if sym_slot in self._entered_slots:
                continue

            # Filtro de precio de entrada — no comprar tokens demasiado caros (payout bajo)
            if opp.market_price > settings.max_entry_price:
                print(
                    f"[SKIP] {market.symbol} entry={opp.market_price:.2f} > max={settings.max_entry_price:.2f} (payout insuficiente)"
                )
                continue

            # Máximo MAX_PER_SLOT entradas por intervalo de 5 min
            slot_count = sum(
                1
                for k in self._entered_slots
                if k.endswith(f"_{slot}") or k == slot_key
            )
            # Contar cuántas entradas hay en este slot
            entries_this_slot = sum(
                1 for k in self._entered_slots if k.split("_")[-1] == str(slot)
            )
            if entries_this_slot >= MAX_PER_SLOT:
                continue

            # Calcular tamaño de apuesta — fijo o dinámico según config
            bet_size = self._dynamic_bet_size()

            # Verificar límite de exposición activa
            open_pos = self.executor.get_open_positions()
            at_risk = sum(p.size_usdc for p in open_pos)
            if at_risk + bet_size > settings.max_position_usdc:
                print(f"[SKIP] Exposición máxima alcanzada (${at_risk:.2f})")
                continue

            # En live: verificar balance USDC real antes de cada entrada.
            # Evita colocar órdenes que serán rechazadas por fondos insuficientes.
            # Se actualiza solo si hay posibilidad de que haya cambiado (cada entrada).
            if settings.trading_mode == "live":
                real_balance = self._fetch_live_balance()
                if real_balance is not None:
                    self._live_balance_usdc = real_balance
                    if real_balance < bet_size:
                        print(
                            f"[LIVE] ⚠ Balance insuficiente: ${real_balance:.2f} < ${bet_size:.2f} — stop"
                        )
                        break  # no tiene sentido revisar más candidatos

            # Guardia final anti-duplicado: marcar ANTES de entrar para evitar
            # doble entrada si este método se llama de forma concurrente en el
            # mismo loop tick (el await REST puede ceder control al event loop).
            if market.market_id in self._entered:
                continue
            self._entered.add(market.market_id)
            self._entered_slots[sym_slot] = slot

            # ENTRAR
            entry_kwargs = dict(
                size_usdc=bet_size,
                tte=market.seconds_to_expiry,
                vol_30s=vol_30s_entry,
                book_source=book_src,
                strike_confirmed=strike_ok,
            )
            if settings.trading_mode == "live":
                loop = asyncio.get_event_loop()
                pos = await loop.run_in_executor(
                    None, lambda: self.executor.enter(opp, **entry_kwargs)
                )
            else:
                pos = self.executor.enter(opp, **entry_kwargs)
            if pos:
                # _entered y _entered_slots ya registrados antes del enter()
                self.stats.bets_placed += 1
                self.stats.total_wagered += bet_size
                if opp.edge > self.stats.best_edge:
                    self.stats.best_edge = opp.edge
                if settings.auto_bet_sizing:
                    print(
                        f"[BET] Apuesta dinámica: ${bet_size:.2f} "
                        f"({settings.bet_fraction:.1%} de ${self._current_capital():.2f})"
                    )
                self.tg.trade_entry(
                    symbol=market.symbol,
                    side=opp.side,
                    entry_price=opp.market_price,
                    edge=opp.edge,
                    size=bet_size,
                    tte=market.seconds_to_expiry,
                    mode=settings.trading_mode,
                )

    async def _fetch_settlement_price(
        self, symbol: str, end_time: float
    ) -> float | None:
        """
        Obtiene el precio Binance al momento del vencimiento vía REST klines.
        Usa el open price de la vela de 1min que empieza en end_time.
        end_time es siempre múltiplo de 300 → también múltiplo de 60 → inicio de vela.
        Espera hasta 70s después del vencimiento para que la vela esté disponible.
        """
        import aiohttp

        sym_map = {
            "BTC": "BTCUSDT",
            "ETH": "ETHUSDT",
            "SOL": "SOLUSDT",
            "XRP": "XRPUSDT",
            "BNB": "BNBUSDT",
        }
        binance_symbol = sym_map.get(symbol.upper())
        if not binance_symbol:
            return None

        # Necesitamos que el candle haya cerrado (60s después del open)
        wait_until = end_time + 65
        if time.time() < wait_until:
            return None  # todavía no está disponible, fallback al WebSocket

        start_ms = int(end_time * 1000)
        url = (
            f"https://api.binance.com/api/v3/klines"
            f"?symbol={binance_symbol}&interval=1m"
            f"&startTime={start_ms}&limit=1"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            if not data:
                return None
            open_price = float(data[0][1])
            return open_price
        except Exception:
            return None

    async def _check_take_profit(self) -> None:
        """
        Revisa posiciones abiertas y cierra anticipadamente si el token
        alcanzó el precio objetivo (take_profit_price).

        Lógica:
          - Si take_profit_price = 0 → desactivado
          - Busca el best_bid actual del token (precio al que podemos vender)
          - Si best_bid >= take_profit_price → vender ahora, asegurar ganancia
          - Solo aplica si el mercado aún no venció

        Por qué best_bid y no best_ask:
          Nosotros VENDEMOS → compramos a ask cuando entramos, vendemos a bid cuando salimos.
        """
        tp = settings.take_profit_price
        if tp <= 0:
            return

        now = time.time()
        for pos in self.executor.get_open_positions():
            market = self._markets.get(pos.market_id)
            if not market or market.is_expired:
                continue

            # Obtener precio bid actual del token que tenemos
            book = self.ob_feed.get_book(pos.token_id)
            if not book or not book.best_bid:
                continue

            bid = book.best_bid
            if bid < tp:
                continue

            # ── TAKE-PROFIT ──────────────────────────────────────────
            tte_left = market.end_time - now
            pnl = self.executor.settle_early(pos, bid)
            if pnl == 0.0 and settings.trading_mode == "live":
                continue  # orden rechazada en live — no registrar

            self.stats.total_pnl += pnl
            self.stats.daily_pnl += pnl

            if pnl > 0:
                self.stats.bets_won += 1
                result = "WIN"
            else:
                self.stats.bets_lost += 1
                result = "LOSS"

            resolved = self.stats.bets_won + self.stats.bets_lost
            wr = self.stats.bets_won / resolved if resolved > 0 else 0.0

            spot_now = self._get_price(market.symbol) or pos.spot_at_entry
            print(
                f"[TP] 💰 {market.symbol} {pos.token_side} | "
                f"entrada={pos.entry_price:.3f} → TP={bid:.3f} | "
                f"T-{tte_left:.0f}s | PnL: ${pnl:+.4f} | "
                f"Total: ${self.stats.total_pnl:+.4f} | WR: {wr:.0%} ({resolved})"
            )

            # Log en trades.log con settle_source = "TAKE-PROFIT"
            self._log_trade(
                pos, market, spot_now, "TAKE-PROFIT", pnl, result, token_exit_price=bid
            )
            self._save_stats()

            self.tg.trade_result(
                symbol=market.symbol,
                side=pos.token_side,
                entry_price=pos.entry_price,
                exit_price=bid,
                pnl=pnl,
                total_pnl=self.stats.total_pnl,
                win_rate=wr,
                resolved=resolved,
                exit_type="TP",
            )

            # Guardar para dashboard
            timeframe = market.timeframe
            self._last_trades.append(
                {
                    "time": time.strftime("%H:%M:%S"),
                    "market_id": market.market_id,
                    "symbol": market.symbol,
                    "side": pos.token_side,
                    "strike": market.reference_price,
                    "exit_price": bid,
                    "entry_price": pos.entry_price,
                    "pnl": pnl,
                    "result": result,
                    "exit_type": "TP",
                    "timeframe": timeframe,
                }
            )
            self._last_trades = self._last_trades[-10:]

    async def _settle_expired(self) -> None:
        """Liquida posiciones de mercados vencidos.

        Fuente de precio al vencimiento (prioridad):
          1. RTDS Chainlink tick DESPUÉS del vencimiento — espera hasta 8s para
             capturar el tick post-expiración que Polymarket usa on-chain.
          2. RTDS Chainlink tick anterior al vencimiento — si no llega tick nuevo.
          3. Binance REST kline open — fallback si RTDS no tiene datos.
          4. Binance WebSocket — último recurso.

        El tick post-vencimiento es más preciso porque Polymarket usa el PRIMER
        tick Chainlink DESPUÉS del timestamp de cierre para resolver on-chain.
        """
        now = time.time()

        # ── Limpieza periódica de estructuras en memoria ──────────────────────
        # Se ejecuta cada vez que se liquidan vencidos (~50ms), pero las
        # condiciones de tiempo evitan trabajo innecesario en cada tick.
        stale_cutoff = now - 900  # 15 min — holgura para mercados de 5m y 15m

        # _entered_slots: slot → timestamp  (deduplicación por símbolo/slot)
        self._entered_slots = {
            k: v for k, v in self._entered_slots.items() if v > now - 600
        }

        # _entered: market_ids ya procesados — limpiar IDs de mercados expirados
        expired_mids = {mid for mid, m in self._markets.items() if m.is_expired}
        self._entered -= expired_mids

        # _markets: eliminar mercados expirados hace >15min (seguridad: no borrar justo al expirar)
        very_old = {
            mid for mid, m in self._markets.items() if m.end_time < stale_cutoff
        }
        for mid in very_old:
            self._markets.pop(mid, None)
            self.discovery._price_confirmed.discard(mid)
            self.discovery._price_source.pop(mid, None)
            self._strike_candidates.pop(mid, None)
            self._api_strike_pending.pop(mid, None)

        # _rest_book_last_fetch y _book_prewarm_ts: limpiar tokens de mercados ya eliminados
        active_token_ids: set[str] = set()
        for m in self._markets.values():
            active_token_ids.add(m.token_id_yes)
            active_token_ids.add(m.token_id_no)
        self._rest_book_last_fetch = {
            k: v for k, v in self._rest_book_last_fetch.items() if k in active_token_ids
        }
        self._book_prewarm_ts = {
            k: v for k, v in self._book_prewarm_ts.items() if k in active_token_ids
        }

        open_pos = self.executor.get_open_positions()
        for pos in open_pos:
            market = self._markets.get(pos.market_id)

            # ── Posición huérfana: el mercado fue purgado de _markets sin liquidar ──
            # Ocurre cuando RTDS cae justo al vencimiento y el mercado expira sin datos.
            # Después de 15 min se purga de _markets, dejando la posición colgada.
            # Solución: liquidar con Binance REST usando el end_time guardado en la posición.
            if not market:
                # pos.end_time y pos.strike ahora siempre existen (guardados al entrar)
                orphan_end_time = pos.end_time or getattr(pos, "market_end_time", None)
                if orphan_end_time and (now - orphan_end_time) > 60:
                    symbol = pos.symbol or "BTC"
                    print(
                        f"[SETTLE] ⚠ Posición huérfana {symbol} — mercado purgado. Liquidando con Binance REST..."
                    )
                    btc = await self._fetch_settlement_price(symbol, orphan_end_time)
                    if btc:
                        strike = pos.strike or getattr(pos, "reference_price", None)
                        if strike:
                            pnl = self.executor.settle(pos, btc, strike, "above")
                            self.stats.daily_pnl += pnl
                            self.stats.total_pnl += pnl
                            result = "✓ GANÓ" if pnl > 0 else "✗ PERDIÓ"
                            if pnl > 0:
                                self.stats.bets_won += 1
                            else:
                                self.stats.bets_lost += 1
                            print(
                                f"[SETTLE] {result} huérfana | {symbol} strike={strike:.2f} | close={btc:.2f} | PnL: ${pnl:+.4f}"
                            )
                            self._save_stats()
                            self.tg.trade_result(
                                symbol,
                                pos.token_side,
                                pos.entry_price,
                                btc,
                                pnl,
                                self.stats.total_pnl,
                                self.stats.win_rate,
                                self.stats.bets_placed,
                                "EXP",
                            )
                        else:
                            print(
                                f"[SETTLE] ⚠ No se pudo obtener strike para posición huérfana {symbol} — descartando"
                            )
                            self.executor.settle(
                                pos, pos.spot_at_entry, pos.spot_at_entry, "above"
                            )
                    else:
                        print(
                            f"[SETTLE] ⚠ Binance REST falló para posición huérfana {symbol} — reintentando en próximo ciclo"
                        )
                continue

            if not market.is_expired:
                continue

            # Esperar hasta 8s después del vencimiento para el tick post-expiración
            time_since_expiry = now - market.end_time
            if time_since_expiry < 8.0:
                continue  # esperar más

            symbol = market.symbol if market else "BTC"

            # 1. Último tick Chainlink AT O ANTES del vencimiento.
            # Polymarket resuelve on-chain llamando latestRoundData() en end_time:
            # el contrato ve el último precio Chainlink ya publicado, es decir el
            # tick con chainlink_ts <= end_time — simétrico al strike (chainlink_inicio).
            # Simetría: inicio=get_price_before(interval_start) | final=get_price_before(end_time)
            rtds_pre = self.rtds_feed.get_price_before(symbol, market.end_time)

            # 2. Primer tick RTDS después del vencimiento (fallback si no hay datos previos)
            rtds_post = self.rtds_feed.get_price_after(symbol, market.end_time)

            # 3. Binance REST (solo si ya pasaron 65s)
            binance_close = None
            if not rtds_pre and not rtds_post:
                binance_close = await self._fetch_settlement_price(
                    symbol, market.end_time
                )

            # Elegir mejor precio disponible
            if rtds_pre:
                btc = rtds_pre
                src = "RTDS-pre"
            elif rtds_post:
                btc = rtds_post
                src = "RTDS-post"
            elif binance_close:
                btc = binance_close
                src = "Binance-REST"
            else:
                btc = self.crypto_feed.get_price(symbol) or pos.spot_at_entry
                src = "WS-fallback"

            # Log de divergencia
            binance_ws = self.crypto_feed.get_price(symbol)
            if binance_ws and abs(btc - binance_ws) > 20:
                print(
                    f"[DIVERGENCIA] {symbol} settle={btc:,.2f} Binance={binance_ws:,.2f} Δ${btc-binance_ws:+.2f} ({src})"
                )

            pnl = self.executor.settle(pos, btc, market.reference_price, "above")

            self.stats.daily_pnl += pnl
            self.stats.total_pnl += pnl

            if pnl > 0:
                self.stats.bets_won += 1
                result = "✓ GANÓ"
            else:
                self.stats.bets_lost += 1
                result = "✗ PERDIÓ"

            resolved = self.stats.bets_won + self.stats.bets_lost
            win_rate = self.stats.bets_won / resolved if resolved > 0 else 0.0
            print(
                f"[SETTLE] {result} | {market.symbol} strike=${market.reference_price:,.2f} | "
                f"close=${btc:,.2f} ({src}) | PnL: ${pnl:+.4f} | "
                f"Total: ${self.stats.total_pnl:+.4f} | "
                f"Win: {win_rate:.0%} ({resolved} resueltas)"
            )
            trade_result = "WIN" if pnl > 0 else "LOSS"

            # Guardar para dashboard (últimas 10 en memoria)
            timeframe = market.timeframe
            self._last_trades.append(
                {
                    "time": time.strftime("%H:%M:%S"),
                    "market_id": market.market_id,
                    "symbol": market.symbol,
                    "side": pos.token_side,
                    "strike": market.reference_price,
                    "exit_price": btc,
                    "entry_price": pos.entry_price,
                    "pnl": pnl,
                    "result": trade_result,
                    "exit_type": "EXP",
                    "timeframe": timeframe,
                }
            )
            self._last_trades = self._last_trades[-10:]  # solo últimas 10

            # Log permanente con todos los detalles (trades.log)
            self._log_trade(pos, market, btc, src, pnl, trade_result)

            self._save_stats()  # persistir al disco

            self.tg.trade_result(
                symbol=market.symbol,
                side=pos.token_side,
                entry_price=pos.entry_price,
                exit_price=btc,
                pnl=pnl,
                total_pnl=self.stats.total_pnl,
                win_rate=win_rate,
                resolved=resolved,
                exit_type="EXP",
            )

            # Lanzar verificación API en background (90s después confirma resultado real)
            token_id = getattr(pos, "token_id", None)
            if token_id and market.market_id:
                asyncio.create_task(
                    self._verify_settle_via_api(
                        market_id=market.market_id,
                        token_id=token_id,
                        logged_result=trade_result,
                        logged_pnl=pnl,
                        symbol=market.symbol,
                        strike=market.reference_price,
                        entry_price=pos.entry_price,
                        size=pos.size_usdc,
                    )
                )

    async def _verify_settle_via_api(
        self,
        market_id: str,
        token_id: str,
        logged_result: str,
        logged_pnl: float,
        symbol: str,
        strike: float,
        entry_price: float,
        size: float,
    ) -> None:
        """
        Verifica el resultado real del mercado consultando la API de Polymarket
        90 segundos después de la liquidación RTDS.

        Si el resultado difiere del loggeado (WIN↔LOSS), corrige stats y trades.log.
        Esto garantiza que el PnL acumulado refleja lo que Polymarket realmente pagó.
        """
        import aiohttp

        # Reintentar hasta que el mercado esté resuelto on-chain (máx 10 intentos, cada 60s).
        # Polymarket puede tardar 2-5 min en resolver después del vencimiento.
        import aiohttp

        MAX_ATTEMPTS = 10
        RETRY_INTERVAL = 60

        winning_token = None
        token_prices_debug: dict = {}  # para diagnóstico
        for attempt in range(MAX_ATTEMPTS):
            await asyncio.sleep(RETRY_INTERVAL)
            try:
                url = f"https://clob.polymarket.com/markets/{market_id}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                token_prices_debug = {
                    t["token_id"]: float(t.get("price", 0))
                    for t in data.get("tokens", [])
                }
                for t in data.get("tokens", []):
                    if float(t.get("price", 0)) >= 0.99:
                        winning_token = t["token_id"]
                        break
                if winning_token is not None:
                    break
            except Exception as e:
                print(
                    f"[API-SETTLE] Error consultando mercado {symbol} intento {attempt+1}: {e}"
                )

        if winning_token is None:
            # Loguear precios si tenemos datos pero ninguno ≥ 0.99
            if token_prices_debug:
                prices_str = ", ".join(
                    f"{tid[-8:]}={p:.4f}" for tid, p in token_prices_debug.items()
                )
                print(
                    f"[API-SETTLE] {symbol} no se pudo resolver tras {MAX_ATTEMPTS} intentos. "
                    f"Precios finales: {prices_str}"
                )
            return  # no se pudo resolver en ~10 minutos

        # Diagnóstico: loguear siempre los precios y qué token ganó
        prices_str = ", ".join(
            f"{tid[-8:]}=WIN✓" if tid == winning_token else f"{tid[-8:]}={p:.4f}"
            for tid, p in token_prices_debug.items()
        )
        our_token_short = token_id[-8:]
        api_won = winning_token == token_id
        api_result = "WIN" if api_won else "LOSS"
        print(
            f"[API-SETTLE] {symbol} strike=${strike:,.2f} | "
            f"Tokens: [{prices_str}] | "
            f"Nuestro: {our_token_short} | "
            f"API→{api_result} | RTDS→{logged_result}"
        )

        if api_result == logged_result:
            return  # correcto ✓

        # ── Resultado incorrecto → corregir ──────────────────────────────
        try:
            fee = size * 0.072 * (1 - entry_price)
            if api_won:
                real_pnl = (size / entry_price) - size - fee
            else:
                real_pnl = -size - fee

            pnl_diff = real_pnl - logged_pnl

            if logged_result == "WIN":
                self.stats.bets_won -= 1
                self.stats.bets_lost += 1
            else:
                self.stats.bets_lost -= 1
                self.stats.bets_won += 1

            self.stats.total_pnl += pnl_diff
            self.stats.daily_pnl += pnl_diff
            self._save_stats()

            # Usar market_id para match preciso, con fallback a symbol+pnl
            for lt in self._last_trades:
                if lt.get("market_id") == market_id or (
                    lt.get("symbol") == symbol
                    and abs(lt.get("pnl", 0) - logged_pnl) < 0.01
                ):
                    lt["pnl"] = real_pnl
                    lt["result"] = api_result
                    break

            direction = f"{logged_result}→{api_result}"
            print(
                f"[API-SETTLE] ⚠ CORRECCIÓN {symbol} | "
                f"{direction} | "
                f"PnL: ${logged_pnl:+.4f} → ${real_pnl:+.4f} (Δ${pnl_diff:+.4f}) | "
                f"Total corregido: ${self.stats.total_pnl:+.4f}"
            )
            self.tg.api_correction(symbol, logged_result, api_result, pnl_diff)

            correction_entry = {
                "entry_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": "API_CORRECTION",
                "market_id": market_id,
                "token_id": token_id,
                "symbol": symbol,
                "strike": strike,
                "original_result": logged_result,
                "corrected_result": api_result,
                "original_pnl": logged_pnl,
                "corrected_pnl": real_pnl,
                "pnl_diff": pnl_diff,
                "winning_token": winning_token,
                "token_prices": token_prices_debug,
            }
            with open(TRADES_FILE, "a") as f:
                f.write(json.dumps(correction_entry) + "\n")

        except Exception as e:
            print(f"[API-SETTLE] ERROR en corrección {symbol}: {e}")

    @staticmethod
    def _load_stats() -> SessionStats:
        """Carga stats acumuladas del archivo persistente (sobrevive reinicios)."""
        try:
            with open(STATS_FILE) as f:
                d = json.load(f)
            s = SessionStats()
            s.bets_placed = d.get("bets_placed", 0)
            s.bets_won = d.get("bets_won", 0)
            s.bets_lost = d.get("bets_lost", 0)
            s.total_wagered = d.get("total_wagered", 0.0)
            s.total_pnl = d.get("total_pnl", 0.0)
            s.best_edge = d.get("best_edge", 0.0)
            # daily_pnl siempre empieza en 0 (kill switch diario)
            print(
                f"[STATS] Cargadas: {s.bets_placed} apuestas | PnL=${s.total_pnl:+.4f}"
            )
            return s
        except Exception:
            return SessionStats()

    def _save_stats(self) -> None:
        """Guarda stats al disco para persistir entre reinicios."""
        try:
            with open(STATS_FILE, "w") as f:
                json.dump(
                    {
                        "bets_placed": self.stats.bets_placed,
                        "bets_won": self.stats.bets_won,
                        "bets_lost": self.stats.bets_lost,
                        "total_wagered": self.stats.total_wagered,
                        "total_pnl": self.stats.total_pnl,
                        "best_edge": self.stats.best_edge,
                    },
                    f,
                )
        except Exception as e:
            print(f"[STATS] ⚠ Error guardando stats: {e}")

    def _log_trade(
        self,
        pos,
        market,
        settle_price: float,
        settle_source: str,
        pnl: float,
        result: str,
        token_exit_price: float = None,
    ) -> None:
        """
        Escribe una línea JSON en trades.log con todos los datos del trade.
        Formato JSONL (una línea por trade) — fácil de leer y analizar después.
        """
        try:
            # Tiempo de vida de la posición (entrada → vencimiento)
            hold_seconds = (
                market.end_time - pos.timestamp
                if market.end_time > pos.timestamp
                else 0
            )
            # Movimiento del precio desde entrada hasta cierre
            price_move_pct = (
                abs(settle_price - pos.spot_at_entry) / pos.spot_at_entry
                if pos.spot_at_entry > 0
                else 0
            )
            entry = {
                # ── Identificación ──────────────────────────────────
                "entry_time": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(pos.timestamp)
                ),
                "settle_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": settings.trading_mode.upper(),
                "market_id": market.market_id,
                "token_id": pos.token_id,
                "symbol": market.symbol,
                "side": pos.token_side,
                # ── Precios ──────────────────────────────────────────
                "strike": round(market.reference_price, 4),
                "spot_entry": round(pos.spot_at_entry, 4),
                "entry_price": round(pos.entry_price, 4),
                "settle_price": round(settle_price, 4),
                "price_move_pct": round(price_move_pct * 100, 3),  # % movimiento spot
                # ── Modelo ───────────────────────────────────────────
                "our_prob": round(pos.our_prob_at_entry, 4),
                "edge": round(pos.edge_at_entry, 4),
                "vol_30s": round(pos.vol_30s_at_entry * 100, 4),  # % volatilidad
                # ── Contexto de entrada ──────────────────────────────
                "tte_entry_s": round(
                    pos.tte_at_entry, 1
                ),  # segundos al vencer al entrar
                "hold_s": round(hold_seconds, 1),  # cuánto tiempo estuvo abierta
                "book_source": pos.book_source,  # "WS" o "REST"
                "strike_confirmed": pos.strike_confirmed,  # Chainlink o estimado
                # ── Resultado ────────────────────────────────────────
                "settle_source": settle_source,
                "result": result,
                "pnl": round(pnl, 4),
                "size_usdc": round(pos.size_usdc, 2),
                "total_pnl": round(self.stats.total_pnl, 4),
                # ── Contexto adicional ───────────────────────────────
                "timeframe": market.timeframe,
                "token_exit_price": (
                    round(token_exit_price, 4) if token_exit_price is not None else None
                ),
                # ── Validación lógica ────────────────────────────────
                "spot_vs_strike": (
                    "above" if pos.spot_at_entry >= market.reference_price else "below"
                ),
                "close_vs_strike": (
                    "above" if settle_price >= market.reference_price else "below"
                ),
                "correct_direction": (
                    (pos.token_side == "YES" and settle_price >= market.reference_price)
                    or (
                        pos.token_side == "NO" and settle_price < market.reference_price
                    )
                ),
                # ── Versión del bot ──────────────────────────────────
                # Permite saber exactamente con qué lógica se generó este trade.
                # Cuando cambies el bot, correr reset_version.py para datos limpios.
                "bot_version": BOT_VERSION,
            }
            with open(TRADES_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _write_state(self) -> None:
        """Escribe estado actual a JSON para el dashboard."""
        try:
            open_pos = self.executor.get_open_positions()
            # Precio primario: Chainlink RTDS; fallback Binance
            btc = self._get_price("BTC") or 0.0
            eth = self._get_price("ETH") or 0.0
            sol = self._get_price("SOL") or 0.0
            xrp = self._get_price("XRP") or 0.0
            # Precios Binance (para comparar divergencia en dashboard)
            btc_binance = self.crypto_feed.get_price("BTC") or 0.0
            eth_binance = self.crypto_feed.get_price("ETH") or 0.0
            sol_binance = self.crypto_feed.get_price("SOL") or 0.0
            # Fuente activa
            rtds_active = self.rtds_feed.get_price(
                "BTC"
            ) is not None and not self.rtds_feed.is_stale("BTC", 10.0)
            resolved = self.stats.bets_won + self.stats.bets_lost
            state = {
                "updated": time.strftime("%H:%M:%S"),
                "mode": settings.trading_mode.upper(),
                "btc_price": btc,
                "eth_price": eth,
                "sol_price": sol,
                "xrp_price": xrp,
                "btc_binance": btc_binance,
                "eth_binance": eth_binance,
                "sol_binance": sol_binance,
                "take_profit_price": settings.take_profit_price,
                "price_source": "Chainlink" if rtds_active else "Binance",
                "uptime_s": int(time.time() - self.stats.session_start),
                "stats": {
                    "bets_placed": self.stats.bets_placed,
                    "bets_won": self.stats.bets_won,
                    "bets_lost": self.stats.bets_lost,
                    "win_rate": self.stats.bets_won / resolved if resolved > 0 else 0.0,
                    "total_wagered": self.stats.total_wagered,
                    "total_pnl": self.stats.total_pnl,
                    "daily_pnl": self.stats.daily_pnl,
                    "best_edge": self.stats.best_edge,
                    "roi": (
                        self.stats.total_pnl / self.stats.total_wagered
                        if self.stats.total_wagered > 0
                        else 0.0
                    ),
                    # Capital real:
                    # - Live: balance inicial Polymarket + PnL de esta sesión
                    # - Paper: capital configurado + PnL total acumulado
                    "capital": (
                        (
                            self._live_balance_usdc
                            + (self.stats.total_pnl - self._session_pnl_offset)
                        )
                        if self._live_balance_usdc is not None
                        else (settings.starting_capital_usdc + self.stats.total_pnl)
                    ),
                },
                "open_positions": [
                    {
                        "symbol": p.symbol
                        or self._markets.get(
                            p.market_id, type("M", (), {"symbol": "?"})()
                        ).symbol,
                        "side": p.token_side,
                        "entry_price": p.entry_price,
                        "size": p.size_usdc,
                        "our_prob": p.our_prob_at_entry,
                        "spot_entry": p.spot_at_entry,
                    }
                    for p in open_pos
                ],
                "markets": [
                    {
                        "question": m.question[-45:],
                        "tte_s": int(m.seconds_to_expiry),
                        "strike": m.reference_price,
                        "symbol": m.symbol,
                        "strike_ok": m.market_id in self.discovery._price_confirmed,
                        "yes_ask": (
                            self.ob_feed.get_book(m.token_id_yes)
                            or type("B", (), {"best_ask": None})()
                        ).best_ask,
                        "no_ask": (
                            self.ob_feed.get_book(m.token_id_no)
                            or type("B", (), {"best_ask": None})()
                        ).best_ask,
                        # 15m slots terminan en múltiplos de 900; 5m solo en múltiplos de 300
                        "timeframe": m.timeframe,
                    }
                    for m in sorted(self._markets.values(), key=lambda x: x.end_time)
                    if not m.is_expired
                ][:16],
                "last_trades": self._last_trades,
            }
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception:
            pass

    async def _shutdown(self) -> None:
        self._running = False
        self.crypto_feed.stop()
        self.rtds_feed.stop()
        self.ob_feed.stop()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        (
            await self.discovery._session.close()
            if hasattr(self.discovery, "_session")
            else None
        )

        self.tg.bot_stopped(
            reason="señal de cierre",
            total_pnl=self.stats.total_pnl,
            capital=self._current_capital(),
        )

        print("\n" + "=" * 55)
        print("  RESUMEN FINAL")
        print("=" * 55)
        resolved = self.stats.bets_won + self.stats.bets_lost
        roi = (
            self.stats.total_pnl / self.stats.total_wagered
            if self.stats.total_wagered > 0
            else 0.0
        )
        print(
            f"  Apuestas:    {self.stats.bets_placed} ({self.stats.bets_won}W / {self.stats.bets_lost}L)"
        )
        print(f"  Win rate:    {self.stats.win_rate:.0%}")
        print(f"  Apostado:    ${self.stats.total_wagered:.2f}")
        print(f"  PnL total:   ${self.stats.total_pnl:+.4f}")
        print(f"  ROI:         {roi:+.1%}")
        print(f"  Mejor edge:  {self.stats.best_edge:.1%}")
        print("=" * 55)


def handle_signal(sig, frame):
    for task in asyncio.all_tasks():
        task.cancel()


async def main():
    if not _acquire_pid_lock():
        return  # ya hay otra instancia — salir sin hacer nada
    try:
        bot = LateValueBot()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s, None))
        await bot.start()
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    asyncio.run(main())
