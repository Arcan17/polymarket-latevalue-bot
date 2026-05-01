from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class MarketStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    RESOLVED = "RESOLVED"


class MarketType(str, Enum):
    CRYPTO = "crypto"
    SPORTS = "sports"
    ECON = "econ"
    OTHER = "other"


@dataclass
class Market:
    market_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    reference_price: float  # strike price (Chainlink al inicio del mercado)
    end_time: float
    status: MarketStatus = MarketStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    # Campos extra para late value
    market_type: MarketType = MarketType.CRYPTO
    symbol: str = "BTC"
    direction: str = "above"
    interval_start: float = (
        0.0  # timestamp exacto de inicio del intervalo (calculado de startTime)
    )
    slot_seconds: int = 300  # duración del slot en segundos: 300 (5m) o 900 (15m)

    @property
    def timeframe(self) -> str:
        return "15m" if self.slot_seconds == 900 else "5m"

    @property
    def seconds_to_expiry(self) -> float:
        return max(0.0, self.end_time - time.time())

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.end_time

    @property
    def years_to_expiry(self) -> float:
        return self.seconds_to_expiry / (365.25 * 24 * 3600)


@dataclass
class OrderbookLevel:
    price: float  # probabilidad 0..1
    size: float  # USDC


@dataclass
class OrderbookSnapshot:
    """Orderbook para un token (YES o NO)."""

    token_id: str
    bids: list[OrderbookLevel]  # sorted best (highest) first
    asks: list[OrderbookLevel]  # sorted best (lowest) first
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        return (b + a) / 2.0 if b and a else (b or a)


@dataclass
class Opportunity:
    """Una oportunidad de entrada detectada."""

    market: Market
    token_id: str  # YES o NO token a comprar
    token_side: str  # "YES" o "NO"
    our_prob: float  # probabilidad que calculamos nosotros
    market_price: float  # precio en Polymarket (lo que pagamos)
    edge: float  # our_prob - market_price (positivo = favorable)
    spot_price: float  # precio spot del activo en ese momento
    timestamp: float = field(default_factory=time.time)


@dataclass
class Position:
    market_id: str
    token_id: str
    token_side: str  # YES o NO
    entry_price: float
    size_usdc: float
    our_prob_at_entry: float
    spot_at_entry: float
    symbol: str = (
        ""  # BTC / ETH / SOL etc — guardado al crear para no perderlo al expirar el mercado
    )
    edge_at_entry: float = 0.0
    tte_at_entry: float = 0.0  # segundos al vencimiento cuando entramos
    vol_30s_at_entry: float = 0.0  # volatilidad realizada últimos 30s al entrar
    book_source: str = "WS"  # "WS" = WebSocket, "REST" = fallback REST
    strike_confirmed: bool = False  # strike confirmado via Chainlink o estimado
    end_time: float = (
        0.0  # timestamp UNIX del vencimiento del mercado — necesario para recuperación huérfana
    )
    strike: float = (
        0.0  # reference_price del mercado — necesario para liquidación huérfana
    )
    timestamp: float = field(default_factory=time.time)
    closed: bool = False
    exit_price: float = 0.0
    pnl: float = 0.0


@dataclass
class SessionStats:
    bets_placed: int = 0
    bets_won: int = 0
    bets_lost: int = 0
    bets_pending: int = 0
    total_wagered: float = 0.0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    best_edge: float = 0.0
    session_start: float = field(default_factory=time.time)

    @property
    def win_rate(self) -> float:
        resolved = self.bets_won + self.bets_lost
        return self.bets_won / resolved if resolved > 0 else 0.0

    @property
    def roi(self) -> float:
        return self.total_pnl / self.total_wagered if self.total_wagered > 0 else 0.0
