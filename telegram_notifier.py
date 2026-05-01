"""
Telegram Notifier — envía alertas del bot a un chat de Telegram.

Configurar en .env:
  TELEGRAM_BOT_TOKEN=123456:ABCdef...
  TELEGRAM_CHAT_ID=123456789

Para obtener estos valores:
  1. Habla con @BotFather en Telegram → /newbot → copia el token
  2. Habla con @userinfobot → copia tu chat_id
"""

from __future__ import annotations

import asyncio
import time
import urllib.request
import urllib.parse
import json
import threading
from typing import Optional


class TelegramNotifier:
    """Envía mensajes a Telegram de forma no-bloqueante."""

    def __init__(self, token: str, chat_id: str):
        self.token = token.strip()
        self.chat_id = chat_id.strip()
        self._enabled = bool(token and chat_id and token != "disabled")
        self._last_daily_summary = ""  # fecha del último resumen enviado

    # ── API interna ────────────────────────────────────────────────────────
    def _send_sync(self, text: str) -> None:
        """Envío síncrono en un thread separado para no bloquear el event loop."""
        if not self._enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode(
                {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                }
            ).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass  # ignorar respuesta
        except Exception as e:
            print(f"[TELEGRAM] Error enviando mensaje: {e}")

    def send(self, text: str) -> None:
        """Envío asíncrono — lanza thread y regresa inmediatamente."""
        if not self._enabled:
            return
        t = threading.Thread(target=self._send_sync, args=(text,), daemon=True)
        t.start()

    # ── Eventos del bot ────────────────────────────────────────────────────

    def bot_started(
        self, version: str, mode: str, capital: float, min_edge: float, dead_zone: float
    ) -> None:
        icon = "🟢"
        mode_str = "📄 PAPER" if mode == "paper" else "💵 LIVE"
        self.send(
            f"{icon} <b>Bot arrancó</b> {version}\n"
            f"Modo: {mode_str}\n"
            f"Capital: <b>${capital:.2f}</b> | Edge mín: {min_edge:.0%} | Dead zone: {dead_zone:.2%}"
        )

    def bot_stopped(self, reason: str, total_pnl: float, capital: float) -> None:
        self.send(
            f"🔴 <b>Bot detenido</b>\n"
            f"Razón: {reason}\n"
            f"PnL total: <b>{total_pnl:+.2f} USDC</b> | Capital: ${capital:.2f}"
        )

    def trade_entry(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        edge: float,
        size: float,
        tte: float,
        mode: str,
    ) -> None:
        icon = "🎯"
        direction = "▲ YES" if side == "YES" else "▼ NO"
        mode_tag = "[PAPER]" if mode == "paper" else "[LIVE]"
        self.send(
            f"{icon} <b>Entrada {mode_tag}</b>\n"
            f"{symbol} {direction} | precio: {entry_price:.2f}\n"
            f"Edge: <b>{edge:.1%}</b> | Size: ${size:.2f} | T-{tte:.0f}s"
        )

    def trade_result(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        total_pnl: float,
        win_rate: float,
        resolved: int,
        exit_type: str,
    ) -> None:
        if pnl > 0:
            icon = "✅"
            verdict = "GANÓ"
        else:
            icon = "❌"
            verdict = "PERDIÓ"

        tp_tag = " (TP)" if exit_type == "TP" else ""
        direction = "▲ YES" if side == "YES" else "▼ NO"

        self.send(
            f"{icon} <b>{verdict}{tp_tag}</b> — {symbol} {direction}\n"
            f"Entrada: {entry_price:.2f} → Salida: {exit_price:.4g}\n"
            f"PnL: <b>{pnl:+.3f} USDC</b>\n"
            f"Acumulado: ${total_pnl:+.2f} | WR: {win_rate:.0%} ({resolved} trades)"
        )

    def kill_switch(self, daily_pnl: float, limit: float) -> None:
        self.send(
            f"🛑 <b>KILL SWITCH activado</b>\n"
            f"Pérdida diaria: <b>${daily_pnl:.2f}</b> (límite: ${limit:.2f})\n"
            f"Bot detenido hasta mañana."
        )

    def connection_lost(self, feed: str, seconds: int) -> None:
        self.send(
            f"⚠️ <b>Desconexión: {feed}</b>\n"
            f"Sin datos por {seconds}s — reconectando..."
        )

    def api_correction(
        self, symbol: str, old_result: str, new_result: str, pnl_diff: float
    ) -> None:
        self.send(
            f"🔄 <b>Corrección API</b> — {symbol}\n"
            f"RTDS dijo {old_result} → API dice <b>{new_result}</b>\n"
            f"Diferencia PnL: {pnl_diff:+.3f} USDC"
        )

    def daily_summary(
        self,
        date_str: str,
        wins: int,
        losses: int,
        daily_pnl: float,
        total_pnl: float,
        capital: float,
    ) -> None:
        # Evitar enviar el mismo resumen dos veces
        if self._last_daily_summary == date_str:
            return
        self._last_daily_summary = date_str

        resolved = wins + losses
        wr = wins / resolved if resolved > 0 else 0.0
        icon = "📊"
        self.send(
            f"{icon} <b>Resumen diario — {date_str}</b>\n"
            f"Trades: {resolved} | Wins: {wins} | Losses: {losses} | WR: {wr:.0%}\n"
            f"PnL hoy: <b>{daily_pnl:+.2f} USDC</b>\n"
            f"PnL total: {total_pnl:+.2f} USDC | Capital: <b>${capital:.2f}</b>"
        )
