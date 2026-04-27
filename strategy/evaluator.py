"""
Evaluador de oportunidades de late value.

Para cada mercado crypto cerca del vencimiento:
  1. Calcula P(YES) con el modelo de opción digital (Black-Scholes)
  2. Compara con el precio de Polymarket
  3. Si edge > min_edge → genera una Opportunity

Modelo: P(YES) = N(d)  donde  d = ln(S/K) / (σ√T)
  S = precio spot actual (Chainlink vía RTDS)
  K = strike price del mercado
  σ = volatilidad realizada anualizada
  T = tiempo al vencimiento en años
  N = CDF normal estándar

Filtros adicionales:
  - Dead zone: no entrar si spot está a <0.1% del strike (demasiado cerca)
  - Volatilidad reciente: no entrar si BTC movió >0.3% en últimos 30s (spike)
  - Spread: no entrar si ask-bid spread > max_spread (baja liquidez)
  - Tie rule: NO solo gana si BTC < strike ESTRICTAMENTE → +2% edge extra para NO
"""
from __future__ import annotations

import math
from typing import Optional

from config.settings import settings
from data.models import Market, Opportunity


def norm_cdf(x: float) -> float:
    """CDF de la distribución normal estándar."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def digital_option_prob(
    spot: float,
    strike: float,
    t_seconds: float,
    vol_annual: float,
    direction: str = "above",
) -> float:
    """
    Probabilidad de que spot esté above/below strike al vencimiento.
    Returns probability in [0, 1].

    Tie rule: "above" (YES) gana si BTC >= strike (empate va a YES).
    "below" (NO) necesita BTC < strike ESTRICTAMENTE.
    """
    if t_seconds <= 0:
        if direction == "above":
            return 1.0 if spot >= strike else 0.0
        else:
            return 1.0 if spot < strike else 0.0

    t_years = t_seconds / (365.25 * 24 * 3600)
    sqrt_t = math.sqrt(t_years)

    if sqrt_t < 1e-10 or vol_annual < 1e-10:
        if direction == "above":
            return 1.0 if spot >= strike else 0.0
        else:
            return 1.0 if spot < strike else 0.0

    d = math.log(spot / strike) / (vol_annual * sqrt_t)

    if direction == "above":
        return norm_cdf(d)
    else:
        return norm_cdf(-d)


TAKER_FEE_RATE = 0.072  # Polymarket crypto taker fee (7.2%)


class Evaluator:
    """
    Evalúa oportunidades de late value para mercados crypto.
    """

    def __init__(self, vol_estimator=None) -> None:
        self._vol_estimator = vol_estimator  # callable(symbol) → float | None

    def evaluate(
        self,
        market: Market,
        spot_price: float,
        yes_ask: Optional[float],
        no_ask: Optional[float],
        yes_bid: Optional[float] = None,
        no_bid: Optional[float] = None,
        vol_30s: float = 0.0,
    ) -> Optional[Opportunity]:
        """
        Evalúa si hay oportunidad de late value en este mercado.

        Filtros:
          1. Ventana temporal: min_time_s < tte < entry_window_s
          2. Dead zone: spot dentro de dead_zone_pct del strike → skip
          3. Volatilidad reciente: vol_30s > max_vol_30s_pct → skip (spike)
          4. Spread: ask - bid > max_spread → skip (baja liquidez)
          5. Edge YES: our_prob - yes_effective >= min_edge
          6. Edge NO: our_prob_no - no_effective >= min_edge + no_extra_margin (tie rule)

        Retorna Opportunity si hay edge suficiente, None si no.
        """
        tte = market.seconds_to_expiry

        # Filtro 1: Ventana temporal
        if tte > settings.entry_window_s or tte < settings.min_time_s:
            return None

        if market.reference_price <= 0 or spot_price <= 0:
            return None

        # Filtro 2: Dead zone — si estamos demasiado cerca del strike,
        # la probabilidad depende demasiado de la precisión del precio y
        # el lag del oráculo puede generar falsas señales
        distance_pct = abs(spot_price - market.reference_price) / market.reference_price
        if distance_pct < settings.dead_zone_pct:
            return None

        # Filtro 3: Volatilidad reciente — protección contra spikes
        if vol_30s > settings.max_vol_30s_pct:
            return None

        # Obtener volatilidad para el modelo
        vol_source = "default"
        vol = settings.vol_default
        if self._vol_estimator:
            estimated = self._vol_estimator(market.symbol)
            if estimated:
                vol = estimated * settings.vol_multiplier
                vol_source = f"realizada×{settings.vol_multiplier} ({estimated:.0%}→{vol:.0%})"

        # Calcular probabilidades (Black-Scholes digital option)
        raw_prob_yes = digital_option_prob(
            spot=spot_price,
            strike=market.reference_price,
            t_seconds=tte,
            vol_annual=vol,
            direction=market.direction,
        )

        # Calibración ECE: shrinkage hacia 0.5 para corregir sobreestimación del modelo
        # p_cal = 0.5 + (p_raw - 0.5) * alpha   (alpha < 1 = más conservador)
        alpha = settings.calibration_alpha
        our_prob_yes = 0.5 + (raw_prob_yes - 0.5) * alpha
        our_prob_no = 1.0 - our_prob_yes

        spot_above_strike = spot_price >= market.reference_price

        # YES extra margin: YES tiene 79% WR vs NO 94% WR histórico.
        # Requerir +3% más de edge en YES para compensar la menor fiabilidad.
        YES_EXTRA_MARGIN = 0.03

        # Evaluar lado YES (spot ya está arriba del strike → apostar a que se mantiene)
        # Filtro direccional: solo comprar YES si spot >= strike.
        # Evita apostar a reversión cuando el mercado está en contra.
        # Precio mínimo 0.10: bloquear solo casos extremos (mercado 90%+ seguro del NO)
        if yes_ask is not None and yes_ask >= 0.10 and spot_above_strike:
            # Filtro spread
            if yes_bid is None or (yes_ask - yes_bid) <= settings.max_spread:
                yes_effective = yes_ask * (1 + TAKER_FEE_RATE * (1 - yes_ask))
                edge_yes = our_prob_yes - yes_effective
                if edge_yes >= settings.min_edge + YES_EXTRA_MARGIN:
                    print(
                        f"[EDGE] YES {market.symbol} "
                        f"spot=${spot_price:,.2f} strike=${market.reference_price:,.2f} "
                        f"T={tte:.0f}s vol={vol_source} "
                        f"prob={our_prob_yes:.1%} ask={yes_ask:.3f} edge={edge_yes:.1%}"
                    )
                    return Opportunity(
                        market=market,
                        token_id=market.token_id_yes,
                        token_side="YES",
                        our_prob=our_prob_yes,
                        market_price=yes_ask,
                        edge=edge_yes,
                        spot_price=spot_price,
                    )

        # Evaluar lado NO (spot ya está abajo del strike → apostar a que se mantiene)
        # Filtro direccional: solo comprar NO si spot < strike.
        # no_extra_margin eliminado: NO tiene 94% WR histórico, no necesita penalización extra.
        if no_ask is not None and no_ask >= 0.10 and not spot_above_strike:
            if no_bid is None or (no_ask - no_bid) <= settings.max_spread:
                no_effective = no_ask * (1 + TAKER_FEE_RATE * (1 - no_ask))
                edge_no = our_prob_no - no_effective
                if edge_no >= settings.min_edge:
                    print(
                        f"[EDGE] NO  {market.symbol} "
                        f"spot=${spot_price:,.2f} strike=${market.reference_price:,.2f} "
                        f"T={tte:.0f}s vol={vol_source} "
                        f"prob={our_prob_no:.1%} ask={no_ask:.3f} edge={edge_no:.1%}"
                    )
                    return Opportunity(
                        market=market,
                        token_id=market.token_id_no,
                        token_side="NO",
                        our_prob=our_prob_no,
                        market_price=no_ask,
                        edge=edge_no,
                        spot_price=spot_price,
                    )

        return None
