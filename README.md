# Late Value Bot — Polymarket BTC Up/Down

Bot de trading algorítmico para mercados de predicción BTC Up/Down en [Polymarket](https://polymarket.com). Detecta ineficiencias de precio usando valoración Black-Scholes y feeds de precio en tiempo real.

![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Polymarket](https://img.shields.io/badge/Platform-Polymarket-purple)
![Mode](https://img.shields.io/badge/Mode-Paper%20%7C%20Live-green)

## Como funciona

El bot monitorea mercados BTC Up/Down con menos de 90 segundos al vencimiento. Cuando el precio del mercado está rezagado respecto al precio real de BTC, calcula una ventaja estadística (edge) usando el modelo Black-Scholes y entra en la posición favorable.

```
Precio BTC real (Binance/Chainlink)
           ↓
   Modelo Black-Scholes
           ↓
   P(YES) calculado vs precio mercado
           ↓
   Si edge > 12% → entrada automática
           ↓
   Mantiene hasta vencimiento
```

## Stack tecnico

- **Python 3.9+** — asyncio para feeds concurrentes
- **Chainlink + Binance** — precio BTC spot en tiempo real
- **Polymarket CLOB API** — ejecucion de ordenes
- **Black-Scholes** — modelo de valoracion de opciones adaptado
- **Rich** — dashboard en terminal en tiempo real

## Arquitectura

```
polymarket_latevalue/
├── main.py              # Entry point y loop principal
├── dashboard.py         # Dashboard terminal en tiempo real
├── config/              # Configuracion y settings
├── feeds/               # Feeds de datos en tiempo real
│   ├── crypto_feed.py   # Precio BTC (Binance)
│   ├── rtds_feed.py     # Feed Chainlink
│   ├── market_discovery.py  # Descubrimiento de mercados
│   └── orderbook_feed.py    # Order book Polymarket
├── strategy/            # Logica de trading
│   ├── evaluator.py     # Black-Scholes + calculo de edge
│   └── vol_estimator.py # Estimacion de volatilidad
└── execution/           # Ejecucion de ordenes
```

## Instalacion

```bash
# Clonar repositorio
git clone https://github.com/Arcan17/polymarket-latevalue-bot.git
cd polymarket-latevalue-bot

# Crear ambiente virtual
python -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Configurar variables de entorno
cp .env.example .env
# Editar .env con tus credenciales
```

## Uso

```bash
# Modo paper (sin dinero real, recomendado para empezar)
TRADING_MODE=PAPER python main.py

# Ver dashboard
python dashboard.py
```

## Configuracion

Copia `.env.example` a `.env` y configura:

| Variable | Descripcion | Default |
|---|---|---|
| `TRADING_MODE` | PAPER o LIVE | PAPER |
| `MIN_EDGE` | Edge minimo para entrar (0.12 = 12%) | 0.12 |
| `ORDER_SIZE_USDC` | Tamano de orden en USDC | 1.0 |
| `MAX_DAILY_LOSS_USDC` | Kill switch: perdida maxima diaria | 5.0 |
| `ENTRY_WINDOW_S` | Ventana de entrada en segundos | 90 |

## Caracteristicas

- **Modo paper**: Simula trades sin dinero real para validar estrategia
- **Kill switch**: Para automaticamente si pierde mas del limite diario
- **Dashboard en tiempo real**: Muestra posiciones, PnL y mercados monitoreados
- **Volatilidad adaptativa**: Ajusta el modelo segun condiciones del mercado
- **Multi-feed**: Usa Chainlink y Binance para mayor precision

## Disclaimer

Este bot es un proyecto personal de investigacion y aprendizaje. El trading en mercados de prediccion conlleva riesgo de perdida de capital. Usar en modo PAPER antes de considerar capital real.

## Autor

Bastian — Python Developer
Vina del Mar, Chile
