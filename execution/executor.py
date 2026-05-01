"""
Executor — paper mode simula apuestas, live mode coloca órdenes reales.
En late value siempre compramos (BUY) y mantenemos hasta vencimiento.

Fees (Polymarket crypto taker):
  fee = shares × 0.072 × price × (1 - price)
      = size_usdc × 0.072 × (1 - price)      [simplificado]

Persistencia de posiciones (live):
  Las posiciones abiertas se escriben en LIVE_POSITIONS_FILE después de
  cada entrada. Si el bot se reinicia (crash, actualización), las posiciones
  sobreviven y se pueden recuperar al arrancar. Sin esto, un crash durante
  una posición abierta significa que jamás se registra el resultado.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

TAKER_FEE_RATE = 0.072  # Polymarket crypto taker fee

# Archivo de persistencia de posiciones live (en /tmp = no persiste entre reboots del sistema,
# pero sí entre reinicios del bot — que es lo que necesitamos)
LIVE_POSITIONS_FILE = Path("/tmp/live_positions.json")

from config.settings import settings
from data.models import Opportunity, Position


class PaperExecutor:
    """Simula ejecución sin dinero real."""

    def __init__(self) -> None:
        self._positions: list[Position] = []

    def enter(
        self,
        opp: Opportunity,
        size_usdc: float = None,
        tte: float = 0.0,
        vol_30s: float = 0.0,
        book_source: str = "WS",
        strike_confirmed: bool = False,
    ) -> Optional[Position]:
        """Simula entrada en una oportunidad."""
        size = size_usdc if size_usdc is not None else settings.order_size_usdc
        pos = Position(
            market_id=opp.market.market_id,
            token_id=opp.token_id,
            token_side=opp.token_side,
            entry_price=opp.market_price,
            size_usdc=size,
            our_prob_at_entry=opp.our_prob,
            spot_at_entry=opp.spot_price,
            symbol=opp.market.symbol,
            edge_at_entry=opp.edge,
            tte_at_entry=tte,
            vol_30s_at_entry=vol_30s,
            book_source=book_source,
            strike_confirmed=strike_confirmed,
            end_time=opp.market.end_time,
            strike=opp.market.reference_price,
        )
        self._positions.append(pos)
        print(
            f"[PAPER] ENTRADA: {opp.token_side} {opp.market.symbol} "
            f"strike=${opp.market.reference_price:,.0f} "
            f"@ {opp.market_price:.3f} | prob={opp.our_prob:.1%} "
            f"edge={opp.edge:.1%} | size=${size:.2f} | T={opp.market.seconds_to_expiry:.0f}s"
        )
        return pos

    def settle(
        self, pos: Position, spot_price: float, strike: float, direction: str
    ) -> float:
        """Liquida posición al vencimiento. Retorna PnL."""
        if direction == "above":
            won = spot_price >= strike
        else:
            won = spot_price <= strike

        if pos.token_side == "YES":
            outcome_win = won
        else:
            outcome_win = not won

        # Fee taker: size × 0.072 × (1 - price)
        fee = pos.size_usdc * TAKER_FEE_RATE * (1 - pos.entry_price)

        if outcome_win:
            # Paga $1 por share. Shares = size / entry_price
            shares = pos.size_usdc / pos.entry_price
            pnl = shares * 1.0 - pos.size_usdc - fee
        else:
            pnl = -pos.size_usdc - fee

        pos.pnl = pnl
        pos.exit_price = 1.0 if outcome_win else 0.0
        pos.closed = True
        return pnl

    def settle_early(self, pos: Position, sell_price: float) -> float:
        """
        Cierra la posición antes del vencimiento vendiendo el token al precio actual.
        Usado por el take-profit — asegura ganancia sin esperar los 5 minutos.

        PnL = shares × sell_price - size - fee_compra - fee_venta
        """
        shares = pos.size_usdc / pos.entry_price
        fee_buy = pos.size_usdc * TAKER_FEE_RATE * (1 - pos.entry_price)
        fee_sell = shares * sell_price * TAKER_FEE_RATE * (1 - sell_price)
        pnl = shares * sell_price - pos.size_usdc - fee_buy - fee_sell

        pos.pnl = pnl
        pos.exit_price = sell_price
        pos.closed = True
        print(
            f"[PAPER] 💰 TAKE-PROFIT: {pos.token_side} @ {sell_price:.3f} "
            f"(entrada {pos.entry_price:.3f}) | PnL: ${pnl:+.4f}"
        )
        return pnl

    def get_open_positions(self) -> list[Position]:
        return [p for p in self._positions if not p.closed]

    def get_all_positions(self) -> list[Position]:
        return list(self._positions)


def _position_to_dict(pos: Position) -> dict:
    """Serializa una Position a dict para persistencia JSON."""
    return {
        "market_id": pos.market_id,
        "token_id": pos.token_id,
        "token_side": pos.token_side,
        "entry_price": pos.entry_price,
        "size_usdc": pos.size_usdc,
        "our_prob_at_entry": pos.our_prob_at_entry,
        "spot_at_entry": pos.spot_at_entry,
        "symbol": pos.symbol,
        "edge_at_entry": pos.edge_at_entry,
        "tte_at_entry": pos.tte_at_entry,
        "vol_30s_at_entry": pos.vol_30s_at_entry,
        "book_source": pos.book_source,
        "strike_confirmed": pos.strike_confirmed,
        "end_time": pos.end_time,
        "strike": pos.strike,
        "timestamp": pos.timestamp,
    }


def _position_from_dict(d: dict) -> Position:
    """Deserializa un dict a Position."""
    return Position(
        market_id=d["market_id"],
        token_id=d["token_id"],
        token_side=d["token_side"],
        entry_price=d["entry_price"],
        size_usdc=d["size_usdc"],
        our_prob_at_entry=d["our_prob_at_entry"],
        spot_at_entry=d["spot_at_entry"],
        symbol=d.get("symbol", "?"),
        edge_at_entry=d.get("edge_at_entry", 0.0),
        tte_at_entry=d.get("tte_at_entry", 0.0),
        vol_30s_at_entry=d.get("vol_30s_at_entry", 0.0),
        book_source=d.get("book_source", "RECOVERED"),
        strike_confirmed=d.get("strike_confirmed", True),
        end_time=d.get("end_time", 0.0),
        strike=d.get("strike", 0.0),
        timestamp=d.get("timestamp", time.time()),
    )


class LiveExecutor:
    """
    Ejecutor real via py-clob-client.
    Coloca órdenes MARKET (FOK) en Polymarket CLOB.
    Polymarket resuelve en on-chain — settle() solo actualiza estado local.

    Persistencia: las posiciones abiertas se escriben en LIVE_POSITIONS_FILE
    para sobrevivir reinicios del bot. Llamar load_persisted_positions() al
    arrancar para recuperar posiciones de sesiones anteriores.
    """

    def __init__(self) -> None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.constants import POLYGON

        self._client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=settings.poly_private_key,
            creds=ApiCreds(
                api_key=settings.poly_api_key,
                api_secret=settings.poly_api_secret,
                api_passphrase=settings.poly_api_passphrase,
            ),
            signature_type=2,
            funder=settings.poly_funder_address,
        )
        self._positions: list[Position] = []
        print("[LIVE] Executor inicializado — conexión Polymarket CLOB lista")

    # ── Persistencia de posiciones ────────────────────────────────────────────

    def _save_positions(self) -> None:
        """Escribe todas las posiciones abiertas al archivo de persistencia."""
        try:
            open_pos = [_position_to_dict(p) for p in self._positions if not p.closed]
            with open(LIVE_POSITIONS_FILE, "w") as f:
                json.dump({"positions": open_pos, "saved_at": time.time()}, f)
        except Exception as e:
            print(f"[LIVE] ⚠ No se pudo guardar posiciones: {e}")

    def load_persisted_positions(self) -> list[Position]:
        """
        Carga posiciones abiertas de la sesión anterior (si existen).
        Llamar al arrancar el bot en modo live.
        Retorna la lista de posiciones recuperadas (puede estar vacía).
        """
        if not LIVE_POSITIONS_FILE.exists():
            return []
        try:
            with open(LIVE_POSITIONS_FILE) as f:
                data = json.load(f)
            raw = data.get("positions", [])
            saved_at = data.get("saved_at", 0)
            age_min = (time.time() - saved_at) / 60

            if not raw:
                return []

            # Solo recuperar si el archivo tiene menos de 30 min (mercados de 5m/15m ya vencieron)
            if age_min > 30:
                print(
                    f"[LIVE] Archivo de posiciones tiene {age_min:.0f} min — demasiado viejo, ignorando"
                )
                LIVE_POSITIONS_FILE.unlink(missing_ok=True)
                return []

            recovered = [_position_from_dict(d) for d in raw]
            for p in recovered:
                self._positions.append(p)

            print(
                f"[LIVE] 🔄 Recuperadas {len(recovered)} posición(es) de sesión anterior "
                f"(guardadas hace {age_min:.0f} min):"
            )
            for p in recovered:
                print(
                    f"[LIVE]   {p.token_side} {p.token_id[:16]}… entrada={p.entry_price:.3f} "
                    f"size=${p.size_usdc:.2f}"
                )
            return recovered

        except Exception as e:
            print(f"[LIVE] ⚠ Error al recuperar posiciones: {e}")
            return []

    def _test_connection(self) -> bool:
        """Verifica credenciales y pre-calienta la conexión HTTP/2."""
        try:
            import time

            t0 = time.time()
            orders = self._client.get_orders()
            latency_ms = (time.time() - t0) * 1000
            print(
                f"[LIVE] Conexión OK — latencia API: {latency_ms:.0f}ms "
                f"(órdenes activas: {len(orders) if orders else 0})"
            )

            # Segunda petición para confirmar keep-alive y medir latencia real
            t0 = time.time()
            self._client.get_orders()
            latency2_ms = (time.time() - t0) * 1000
            print(
                f"[LIVE] Conexión HTTP/2 pre-calentada — latencia keep-alive: {latency2_ms:.0f}ms"
            )

            if latency2_ms > 500:
                print(
                    f"[LIVE] ⚠ Latencia alta ({latency2_ms:.0f}ms) — considera migrar a servidor us-east-1"
                )
            elif latency2_ms < 50:
                print(f"[LIVE] ✓ Latencia excelente — servidor bien ubicado")
            return True
        except Exception as e:
            print(f"[LIVE] ERROR conexión: {e}")
            return False

    def enter(
        self,
        opp: Opportunity,
        size_usdc: float = None,
        tte: float = 0.0,
        vol_30s: float = 0.0,
        book_source: str = "WS",
        strike_confirmed: bool = False,
    ) -> Optional[Position]:
        """
        Coloca Market Order FOK (Fill or Kill) en Polymarket CLOB.
        FOK es ideal para late value: ejecuta inmediatamente al ask o cancela.
        amount = USDC a gastar (no shares).

        Se ejecuta en thread separado (run_in_executor) para no bloquear el event loop.
        Reintenta hasta 2 veces si el FOK se rechaza por precio movido.
        """
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        MAX_RETRIES = 2
        price = round(opp.market_price, 3)
        size = size_usdc if size_usdc is not None else settings.order_size_usdc

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                t0 = time.time()
                order_args = MarketOrderArgs(
                    token_id=opp.token_id,
                    amount=size,
                    side="BUY",
                    price=price,
                    order_type=OrderType.FOK,
                )
                t_sign = time.time()
                signed = self._client.create_market_order(order_args)
                t_post = time.time()
                resp = self._client.post_order(signed, OrderType.FOK)
                t_done = time.time()
                sign_ms = (t_sign - t0) * 1000
                build_ms = (t_post - t_sign) * 1000
                post_ms = (t_done - t_post) * 1000
                total_ms = (t_done - t0) * 1000

                if resp and resp.get("success"):
                    # Usar precio real del fill si la API lo devuelve
                    fill_price = (
                        float(resp.get("price", price)) if resp.get("price") else price
                    )
                    shares = size / fill_price
                    pos = Position(
                        market_id=opp.market.market_id,
                        token_id=opp.token_id,
                        token_side=opp.token_side,
                        entry_price=fill_price,  # precio real del fill, no el ask que vimos
                        size_usdc=size,
                        our_prob_at_entry=opp.our_prob,
                        spot_at_entry=opp.spot_price,
                        symbol=opp.market.symbol,
                        edge_at_entry=opp.edge,
                        tte_at_entry=tte,
                        vol_30s_at_entry=vol_30s,
                        book_source=book_source,
                        strike_confirmed=strike_confirmed,
                        end_time=opp.market.end_time,
                        strike=opp.market.reference_price,
                    )
                    self._positions.append(pos)
                    self._save_positions()  # persistir — sobrevive reinicios
                    order_id = resp.get("orderID", "?")
                    retry_note = f" (intento {attempt})" if attempt > 1 else ""
                    slip = fill_price - opp.market_price
                    slip_note = f" slippage={slip:+.3f}" if abs(slip) > 0.001 else ""
                    print(
                        f"[LIVE] ✓ ORDEN EJECUTADA{retry_note}: {opp.token_side} {opp.market.symbol} "
                        f"strike=${opp.market.reference_price:,.0f} "
                        f"@ {fill_price:.3f}{slip_note} | shares={shares:.4f} | edge={opp.edge:.1%} | "
                        f"orderID={order_id} | "
                        f"⏱ total={total_ms:.0f}ms (firma={build_ms:.0f}ms red={post_ms:.0f}ms)"
                    )
                    return pos

                # FOK rechazado — analizar motivo
                err = resp.get("errorMsg", str(resp)) if resp else "sin respuesta"

                # Si el precio se movió, subir precio ligeramente y reintentar
                # Solo si aún tenemos edge suficiente con el nuevo precio
                if attempt < MAX_RETRIES and any(
                    k in err.lower()
                    for k in ["price", "match", "fill", "fok", "no match"]
                ):
                    price = round(price + 0.01, 3)  # subir $0.01 para cruzar el spread
                    new_edge = opp.our_prob - price * (1 + 0.072 * (1 - price))
                    if new_edge < 0.05:  # si el edge cae a menos del 5% no vale la pena
                        print(
                            f"[LIVE] ✗ FOK rechazado, edge insuficiente con precio ajustado — skip"
                        )
                        return None
                    print(
                        f"[LIVE] ⚡ FOK rechazado ({err}) — reintentando @ {price:.3f}"
                    )
                    continue

                print(f"[LIVE] ✗ Orden rechazada (intento {attempt}): {err}")
                return None

            except Exception as e:
                print(f"[LIVE] Error al colocar orden (intento {attempt}): {e}")
                if attempt == MAX_RETRIES:
                    return None

        return None

    def settle(
        self, pos: Position, spot_price: float, strike: float, direction: str
    ) -> float:
        """
        Liquida posición en los registros locales.
        En modo live Polymarket ya pagó en cadena — esto solo actualiza stats.
        """
        if direction == "above":
            won = spot_price >= strike
        else:
            won = spot_price <= strike

        if pos.token_side == "YES":
            outcome_win = won
        else:
            outcome_win = not won

        fee = pos.size_usdc * TAKER_FEE_RATE * (1 - pos.entry_price)

        if outcome_win:
            shares = pos.size_usdc / pos.entry_price
            pnl = shares * 1.0 - pos.size_usdc - fee
        else:
            pnl = -pos.size_usdc - fee

        pos.pnl = pnl
        pos.exit_price = 1.0 if outcome_win else 0.0
        pos.closed = True
        self._save_positions()  # actualizar archivo — posición ya no aparece como abierta
        return pnl

    def settle_early(self, pos: Position, sell_price: float) -> float:
        """
        Cierra la posición antes del vencimiento en modo live.
        Coloca una orden MARKET SELL al precio actual del libro.
        """
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        shares = pos.size_usdc / pos.entry_price
        price = round(sell_price, 3)

        try:
            order_args = MarketOrderArgs(
                token_id=pos.token_id,
                amount=shares,
                side="SELL",
                price=price,
                order_type=OrderType.FOK,
            )
            signed = self._client.create_market_order(order_args)
            resp = self._client.post_order(signed, OrderType.FOK)

            if resp and resp.get("success"):
                fee_buy = pos.size_usdc * TAKER_FEE_RATE * (1 - pos.entry_price)
                fee_sell = shares * sell_price * TAKER_FEE_RATE * (1 - sell_price)
                pnl = shares * sell_price - pos.size_usdc - fee_buy - fee_sell
                pos.pnl = pnl
                pos.exit_price = sell_price
                pos.closed = True
                self._save_positions()  # actualizar archivo
                print(
                    f"[LIVE] 💰 TAKE-PROFIT ejecutado: {pos.token_side} @ {sell_price:.3f} "
                    f"(entrada {pos.entry_price:.3f}) | PnL: ${pnl:+.4f} | "
                    f"orderID={resp.get('orderID','?')}"
                )
                return pnl
            else:
                err = resp.get("errorMsg", str(resp)) if resp else "sin respuesta"
                print(f"[LIVE] ✗ Take-profit rechazado: {err}")
                return 0.0
        except Exception as e:
            print(f"[LIVE] Error take-profit: {e}")
            return 0.0

    def get_open_positions(self) -> list[Position]:
        return [p for p in self._positions if not p.closed]

    def get_all_positions(self) -> list[Position]:
        return list(self._positions)
