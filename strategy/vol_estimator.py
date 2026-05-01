"""
Estimador de volatilidad realizada para el modelo de late value.

Calcula la volatilidad anualizada usando el historial de precios Chainlink
del RTDSFeed. Retornos logarítmicos de 1 minuto sobre los últimos N segundos.

Por qué importa:
  - Mercados tranquilos (vol=40%): BTC 0.3% arriba en 60s → P(YES) ~99%
    → el modelo puede entrar con confianza
  - Mercados volátiles (vol=120%): BTC 0.3% arriba en 60s → P(YES) ~85%
    → el modelo es más conservador (correcto)

Con vol fija (0.80) se pierde esta distinción.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from feeds.rtds_feed import RTDSFeed

# Límites para la vol estimada — evitar valores extremos que distorsionen el modelo
MIN_VOL = 0.20  # 20% anual mínimo
MAX_VOL = 2.50  # 250% anual máximo (BTC en crisis puede llegar a 200%+)


def estimate_realized_vol(
    feed: "RTDSFeed",
    symbol: str,
    lookback_s: int = 900,
    min_points: int = 5,
) -> float | None:
    """
    Calcula volatilidad realizada anualizada desde el historial RTDS.

    Método:
      1. Toma el historial de precios Chainlink del RTDSFeed
      2. Agrupa por minuto (último precio de cada minuto)
      3. Calcula retornos log por minuto
      4. Anualiza: vol_anual = std(retornos_1min) × √525960

    Retorna None si no hay suficientes datos (usa vol_default como fallback).

    Args:
        feed: RTDSFeed con historial de precios
        symbol: "BTC", "ETH", "SOL"
        lookback_s: segundos de historial a usar (default: 15 min)
        min_points: mínimo de puntos por minuto para calcular (default: 5)
    """
    sym = symbol.upper()
    history = feed._history.get(sym)
    if not history or len(history) < min_points:
        return None

    now = time.time()
    cutoff = now - lookback_s

    # Agrupar precios por minuto — último precio de cada minuto
    # chainlink_ts es el timestamp exacto de Chainlink (más preciso que reception_time)
    minute_prices: dict[int, float] = {}
    for chainlink_ts, price, recv in history:
        if recv < cutoff:
            continue
        if price <= 0:
            continue
        minute_key = int(chainlink_ts // 60)
        minute_prices[minute_key] = price  # sobreescribe → último del minuto

    if len(minute_prices) < min_points:
        return None

    # Retornos log entre minutos consecutivos
    sorted_minutes = sorted(minute_prices.keys())
    prices = [minute_prices[m] for m in sorted_minutes]

    returns = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            r = math.log(prices[i] / prices[i - 1])
            returns.append(r)

    if len(returns) < 4:  # mínimo 4 retornos para estimación confiable
        return None

    # Desviación estándar de retornos de 1 minuto (corrección de Bessel)
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)

    if variance <= 0:
        return None

    std_1min = math.sqrt(variance)

    # Anualizar: 1 minuto × 525960 minutos/año
    vol_annual = std_1min * math.sqrt(525960)

    # Clamp a rango razonable
    vol_annual = max(MIN_VOL, min(MAX_VOL, vol_annual))

    return vol_annual


def make_vol_estimator(feed: "RTDSFeed", lookback_s: int = 900):
    """
    Crea un callable(symbol) → float | None para pasar al Evaluator.
    Wrapper conveniente que captura el feed por referencia.

    Uso:
        estimator = make_vol_estimator(rtds_feed)
        evaluator = Evaluator(vol_estimator=estimator)
    """

    def _estimator(symbol: str) -> float | None:
        return estimate_realized_vol(feed, symbol, lookback_s=lookback_s)

    return _estimator
