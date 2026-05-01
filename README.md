# Late Value Bot — Polymarket BTC Up/Down

Algorithmic trading bot for BTC Up/Down prediction markets on [Polymarket](https://polymarket.com). Detects price inefficiencies using Black-Scholes option pricing and real-time price feeds.

![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Polymarket](https://img.shields.io/badge/Platform-Polymarket-purple)
![Mode](https://img.shields.io/badge/Mode-Paper%20%7C%20Live-green)
![License](https://img.shields.io/badge/license-MIT-green?style=flat)

---

## How It Works

The bot monitors BTC Up/Down markets with less than 90 seconds to expiration. When the market price lags behind the real BTC price, it calculates a statistical edge using the Black-Scholes model and enters the favorable position automatically.

```
Real BTC Price (Binance / Chainlink)
           ↓
   Black-Scholes Model
           ↓
   Calculated P(YES) vs market price
           ↓
   If edge > 12% → automatic entry
           ↓
   Hold until expiration
```

---

## Demo

```
[Bot] Market found: BTC-UP @ $47,000  |  60s to expiration
[Bot] Binance spot: $46,900
[Bot] Black-Scholes → P(YES) = 0.131  |  Market price = $0.10
[Bot] Edge detected: 31% > MIN_EDGE (12%) ✓

[PAPER] Entering YES @ $0.10  |  Size: $1.00 USDC
...

[Bot] Market expiring in 5s...
[Bot] Final BTC price: $47,500 → YES wins
[PAPER] Exit @ $0.90  |  PnL: +$0.80 (+800%)
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.9+ with asyncio |
| Price feeds | Binance Spot API + Chainlink RTDS (WebSocket) |
| Trading platform | Polymarket CLOB API |
| Pricing model | Black-Scholes (adapted for binary options) |
| Dashboard | Rich (real-time terminal UI) |
| Notifications | Telegram Bot API |
| Logging | Structured audit trail |

---

## Architecture

```
polymarket_latevalue/
├── main.py                  # Entry point and main loop
├── dashboard.py             # Real-time terminal dashboard
├── config/
│   └── settings.py          # Configuration and parameters
├── feeds/
│   ├── crypto_feed.py       # BTC spot price (Binance)
│   ├── rtds_feed.py         # Chainlink RTDS feed
│   ├── market_discovery.py  # Active market discovery
│   └── orderbook_feed.py    # Polymarket order book
├── strategy/
│   ├── evaluator.py         # Black-Scholes + edge calculation
│   └── vol_estimator.py     # Volatility estimation
└── execution/
    └── executor.py          # Order execution (paper / live)
```

> See [ARCHITECTURE.md](ARCHITECTURE.md) for a full deep dive into the system design, data flows, and trading logic.

---

## Getting Started

```bash
# Clone the repository
git clone https://github.com/Arcan17/polymarket-latevalue-bot.git
cd polymarket-latevalue-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials
```

---

## Usage

```bash
# Paper mode — simulates trades with no real capital (recommended)
TRADING_MODE=PAPER python main.py

# Open the real-time dashboard (separate terminal)
python dashboard.py
```

---

## Configuration

Copy `.env.example` to `.env` and set the following:

| Variable | Description | Default |
|---|---|---|
| `TRADING_MODE` | `PAPER` or `LIVE` | `PAPER` |
| `MIN_EDGE` | Minimum edge to enter a trade (0.12 = 12%) | `0.12` |
| `ORDER_SIZE_USDC` | Order size in USDC | `1.0` |
| `MAX_DAILY_LOSS_USDC` | Kill switch: maximum daily loss | `5.0` |
| `ENTRY_WINDOW_S` | Only trade markets with less than N seconds left | `90` |

---

## Features

- **Paper mode** — Simulate trades with no real capital to validate the strategy before going live
- **Kill switch** — Automatically stops trading if daily loss exceeds the configured limit
- **Real-time dashboard** — Displays positions, PnL, and monitored markets in the terminal
- **Adaptive volatility** — Adjusts the Black-Scholes model based on current market conditions
- **Multi-feed** — Uses both Chainlink and Binance for higher accuracy and redundancy
- **Audit trail** — Every trade logged with timestamp, entry/exit prices, and PnL

---

## Risk Warning

This bot is a personal research and learning project. Trading on prediction markets carries risk of capital loss. Always use **PAPER mode** before considering real capital.

---

## Author

Bastian — Python Developer  
Viña del Mar, Chile
