from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Trading
    trading_mode: str = Field(default="paper")  # paper | live
    order_size_usdc: float = Field(default=1.0)
    min_edge: float = Field(default=0.12)  # min 12% edge para entrar
    max_daily_loss_usdc: float = Field(default=5.0)
    max_position_usdc: float = Field(default=10.0)  # máx por apuesta activa

    # Timing
    loop_interval_ms: int = Field(default=500)
    entry_window_s: float = Field(default=90.0)  # solo entrar con <90s restantes
    min_time_s: float = Field(default=20.0)  # no entrar con <20s restantes

    # Filtros de calidad
    dead_zone_pct: float = Field(
        default=0.001
    )  # no entrar si spot está a <0.1% del strike
    no_extra_margin: float = Field(
        default=0.02
    )  # +2% de edge requerido para lado NO (tie rule)
    max_spread: float = Field(default=0.10)  # máx spread ask-bid para entrar (10c)
    max_vol_30s_pct: float = Field(
        default=0.003
    )  # no entrar si BTC movió >0.3% en últimos 30s
    max_entry_price: float = Field(
        default=1.0
    )  # no comprar tokens más caros que este precio
    max_edge: float = Field(
        default=0.35
    )  # edge >35% = probable libro WS stale → validar con REST

    # Price feeds
    rtds_url: str = Field(
        default="wss://ws-live-data.polymarket.com"
    )  # Polymarket Chainlink feed

    # Polymarket credentials
    poly_private_key: str = Field(default="")
    poly_api_key: str = Field(default="")
    poly_api_secret: str = Field(default="")
    poly_api_passphrase: str = Field(default="")
    poly_funder_address: str = Field(default="")

    # Volatility model
    vol_lookback_seconds: int = Field(
        default=900
    )  # segundos de historial RTDS para vol realizada
    vol_multiplier: float = Field(default=1.5)  # multiplicador de seguridad
    vol_default: float = Field(default=0.80)  # fallback si no hay suficiente historial

    # Calibración ECE
    # Shrinkage hacia 0.5: p_cal = 0.5 + (p_raw - 0.5) * alpha
    # alpha=1.0 = sin corrección, alpha=0.90 = calibrado (medido en histórico Apr 2026)
    calibration_alpha: float = Field(default=0.90)

    # Take-profit anticipado (salida antes del vencimiento)
    # Si el token sube a este precio, vender y asegurar ganancia sin esperar los 5 min.
    # Ejemplo: entry=0.60, tp=0.88 → PnL ≈ +$0.43 en lugar de esperar +$0.67 o -$1.03
    # 0.0 = desactivado (mantener hasta vencimiento)
    take_profit_price: float = Field(default=0.0)

    # WebSocket
    clob_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market"
    )

    # Display
    starting_capital_usdc: float = Field(default=42.0)

    # Bet sizing automático
    # Cuando auto_bet_sizing=true, el bot apuesta bet_fraction% del capital actual
    # Ejemplo: capital=$200, bet_fraction=0.024 → apuesta=$4.80
    auto_bet_sizing: bool = Field(default=False)
    bet_fraction: float = Field(default=0.024)  # 2.4% del capital (= $1 en $42)
    bet_size_min: float = Field(default=1.0)  # mínimo siempre $1
    bet_size_max: float = Field(default=10.0)  # máximo $10 (no sobreexponer)

    # Telegram notifications
    telegram_bot_token: str = Field(
        default=""
    )  # token del bot de Telegram (@BotFather)
    telegram_chat_id: str = Field(default="")  # tu chat_id (@userinfobot)


settings = Settings()
