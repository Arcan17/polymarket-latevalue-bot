# Architecture — Late Value Bot (Polymarket BTC Up/Down)

## Overview

Late Value Bot is an algorithmic trading bot that identifies and exploits price inefficiencies in Polymarket's BTC Up/Down prediction markets using quantitative finance (Black-Scholes option pricing model) and real-time price feeds. The bot operates in paper-trading mode by default for safety, with a live-trading mode available for production use.

```
┌─────────────────────┐
│  Real-time Feeds    │
├─────────────────────┤
│ • Chainlink (RTDS)  │
│ • Binance (Spot)    │
│ • Polymarket CLOB   │
└────────┬────────────┘
         │
    ┌────▼──────────────────────────────────┐
    │     feeds/ (Data Ingestion Layer)     │
    │  ├─ crypto_feed.py   (Binance)       │
    │  ├─ rtds_feed.py     (Chainlink)     │
    │  ├─ market_discovery.py (CLOB)       │
    │  └─ orderbook_feed.py (Polymarket)   │
    └────┬───────────────────────────────────┘
         │
         ├─────────────────────────────────────┐
         │                                     │
    ┌────▼──────────────────┐      ┌──────────▼──────────┐
    │ strategy/ (Analysis)  │      │ config/ (Settings)  │
    │ ├─ evaluator.py      │      │ └─ settings.py     │
    │ │  (Black-Scholes)   │      │    (Parameters)     │
    │ └─ vol_estimator.py  │      └────────────────────┘
    │    (Volatility)       │
    └────┬──────────────────┘
         │
    ┌────▼──────────────────────────┐
    │  execution/                   │
    │  ├─ executor.py               │
    │  │  (Order execution)          │
    │  └─ position_manager.py (PnL) │
    └────┬───────────────────────────┘
         │
    ┌────▼──────────────────────────┐
    │ Dashboard & Monitoring        │
    │ ├─ dashboard.py (Terminal UI) │
    │ ├─ health_check.py (API)      │
    │ └─ audit.py (Compliance)      │
    └────────────────────────────────┘
```

## Key Components

### 1. **Real-time Data Feeds** (`feeds/`)

Concurrent data ingestion layer that pulls price data from multiple sources.

#### **Crypto Feed** (`feeds/crypto_feed.py`)
- **Source:** Binance Spot Price API (public, no auth)
- **Data:** BTC/USD spot price, updated every 1-2 seconds
- **Purpose:** Ground truth price for Black-Scholes calculation
- **Fallback:** Cached price + warning if feed stale

**Flow:**
```
┌─ asyncio.create_task(crypto_feed_loop())
│  └─ Every 2 sec:
│     1. httpx.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
│     2. Parse response → extract price
│     3. Broadcast to evaluator via queue
└─ Non-blocking (other feeds run in parallel)
```

#### **RTDS Feed** (`feeds/rtds_feed.py`)
- **Source:** Chainlink Real-time Data Service (RTDS) WebSocket
- **Data:** Blockchain-verified BTC price, lower latency
- **Purpose:** Alternative to Binance (better for on-chain traders)
- **Fallback:** Use Binance if Chainlink unavailable

**Advantages over Binance:**
- Lower latency (milliseconds)
- Blockchain-verified (cryptographic signature)
- Used by traders on-chain

#### **Market Discovery** (`feeds/market_discovery.py`)
- **Source:** Polymarket CLOB API (public)
- **Data:** Active BTC Up/Down markets with <90s to expiration
- **Purpose:** Identify tradeable opportunities
- **Refresh:** Every 10 seconds

**Flow:**
```
1. Query Polymarket CLOB: /markets?condition=BTC-UP-DOWN
2. Filter: time_to_expiration < 90 seconds
3. Return market list: [{id, yes_price, no_price, expiration, ...}]
```

#### **Orderbook Feed** (`feeds/orderbook_feed.py`)
- **Source:** Polymarket CLOB API WebSocket
- **Data:** Bid/ask prices and order book depth
- **Purpose:** Detect entry/exit opportunities
- **Latency:** <100ms

### 2. **Strategy Layer** (`strategy/`)

Core trading logic using quantitative finance.

#### **Volatility Estimator** (`strategy/vol_estimator.py`)
- **Purpose:** Estimate BTC price volatility for Black-Scholes model
- **Inputs:** 
  - Price history (last 15 minutes, 30-second candles)
  - Market conditions (trending/ranging)
- **Outputs:**
  - Annualized volatility (σ)
  - Confidence level
- **Formula:**
  ```
  σ = sqrt(variance(log_returns))
  = sqrt(sum((ln(P_t / P_t-1))^2) / n)
  ```

**Example:**
```
Price history: [46500, 46520, 46510, 46530, ...]
Returns: [0.43%, -0.21%, 0.43%, ...]
Volatility (σ): 0.045 (4.5% daily) → 73% annualized
```

#### **Evaluator** (`strategy/evaluator.py`)
- **Purpose:** Price a BTC Up/Down option using Black-Scholes
- **Inputs:**
  - S = Current spot price (from crypto_feed)
  - K = Strike price (usually spot at market creation)
  - T = Time to expiration (seconds → years)
  - σ = Volatility (from vol_estimator)
  - r = Risk-free rate (0% for crypto)
- **Output:**
  - P(YES) = Probability BTC goes above K
  - P(NO) = 1 - P(YES)

**Black-Scholes Formula:**

```
d1 = (ln(S/K) + (σ²/2)T) / (σ√T)
d2 = d1 - σ√T

N(x) = Cumulative normal distribution
P(YES) = N(d1)
```

**Example:**
```
Market: BTC Up or Down at $47,000 expiring in 60 seconds
S = $46,900 (current spot)
K = $47,000 (strike)
T = 60s / (365.25 × 24 × 3600) = 0.0000019 years
σ = 0.45 (annualized from vol_estimator)

Calculation:
d1 = (ln(46900/47000) + (0.45²/2) × 0.0000019) / (0.45 × √0.0000019)
   = (-0.00213 + 0.00000047) / 0.00189
   = -1.12

N(-1.12) = 0.131  ← Probability of UP
P(NO) = 1 - 0.131 = 0.869  ← Probability of DOWN

Expected prices if market is fair:
YES token = 0.131 × $1.00 = $0.131
NO token  = 0.869 × $1.00 = $0.869
```

**Detecting Edge:**

```
Market prices:
YES: $0.10 (underpriced)
NO:  $0.90 (correctly priced)

Fair value: YES = $0.131, NO = $0.869

Edge calculation:
edge_yes = (0.131 - 0.10) / 0.10 = 31% ✓ EDGE!

If MIN_EDGE = 12%, this trade is entered.
```

### 3. **Execution Layer** (`execution/`)

Places and manages orders on Polymarket.

#### **Executor** (`execution/executor.py`)
- **Modes:**
  - **Paper:** Simulates trades without real capital (default, safe)
  - **Live:** Real capital on Polymarket (use with caution)
- **Actions:**
  - Place market orders on YES or NO token
  - Track order status via Polymarket API
  - Cancel orders if timeout (>5s without fill)
- **Safety:**
  - Validates balance before trade
  - Blocks trades if daily loss limit exceeded
  - All trades logged with timestamp/entry/exit

**Flow:**
```
Paper Mode:
1. evaluator calculates edge > 12%
2. executor (paper) simulates trade
   - Deduct: order_size from balance
   - Record: entry_price, timestamp
   - No actual order sent

Live Mode:
1. evaluator calculates edge > 12%
2. executor (live) sends HTTP POST to Polymarket CLOB
   - POST /api/trades
   - Body: {market_id, amount, side}
3. Polymarket matches order immediately
4. Track: position, PnL, exit opportunity
```

#### **Position Manager** (`execution/position_manager.py`)
- **Tracks:**
  - Current positions (entry price, amount, market_id)
  - PnL (profit/loss)
  - Exit signals (market price crosses entry, market near expiration)
- **Actions:**
  - Close position when:
    - Market is trending away (exit loss after 30s)
    - Market is expiring (<5s left)
    - Daily loss limit exceeded

### 4. **Configuration** (`config/settings.py`)

Centralized parameters, loaded from environment variables.

**Key Settings:**

```python
class Settings:
    TRADING_MODE = "PAPER"  # or "LIVE"
    
    # Risk management
    MIN_EDGE = 0.12         # 12% minimum edge to enter
    ORDER_SIZE_USDC = 1.0   # $1 per trade
    MAX_DAILY_LOSS_USDC = 5.0  # Stop trading if lost $5 today
    
    # Market monitoring
    ENTRY_WINDOW_S = 90     # Only trade markets <90s to expiration
    CHECK_INTERVAL_S = 2    # Check for opportunities every 2s
    
    # Feeds
    BINANCE_API_URL = "https://api.binance.com"
    POLYMARKET_CLOB_URL = "https://clob.polymarket.com"
    CHAINLINK_RTDS_URL = "wss://..."
    
    # Notifications
    TELEGRAM_ENABLED = True
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
```

### 5. **Dashboard** (`dashboard.py`)

Real-time terminal UI showing bot activity.

**Displays:**
- Current BTC spot price (Binance + Chainlink)
- Active markets discovered
- Current positions (entry/exit)
- Today's PnL (realized + unrealized)
- Last 20 trades (timestamp, market, side, result)
- Bot health (CPU, memory, feed latency)

**Stack:** `rich` library for colored terminal output

### 6. **Monitoring & Logging** (`utils/logger.py`, `health_check.py`)

#### **Health Check API** (`health_check.py`)
- Endpoint: `GET http://localhost:8000/health`
- Response: `{"status": "healthy", "position_count": 2, "daily_pnl": 0.45}`
- Purpose: External monitoring (Pingdom, DataDog, etc.)

#### **Audit Log** (`audit.py`)
- Logs every trade with:
  - Timestamp
  - Market ID
  - Entry price, size, side
  - Exit price, PnL
  - Reason (manual, edge gone, expiration)
- Purpose: Compliance, debugging, strategy backtesting

### 7. **Notifications** (`telegram_notifier.py`)

Optional Telegram integration for alerts.

**Notifications:**
- Trade entry: "🟢 BTC Up edge 15% @ $47,100"
- Trade exit: "🟡 Exit: +$0.12 PnL"
- Daily summary: "📊 Today: 5 trades, +$0.87 PnL"
- Errors: "❌ Feed disconnected (Binance), using Chainlink"

---

## Data Flow

### Scenario 1: Market Entry (Detecting Edge)

```
1. crypto_feed: $46,900 BTC spot price → queue
2. market_discovery: Found BTC-UP market, 60s to expiration → queue
3. orderbook_feed: YES = $0.10, NO = $0.90 → queue
4. Main loop (every 2s):
   ├─ Retrieve feeds:
   │   S = 46900 (spot)
   │   K = 47000 (strike)
   │   T = 60s
   │   σ = 0.45 (from vol_estimator)
   │
   ├─ evaluator.calculate_fair_prices()
   │   └─ P(YES) = 0.131
   │
   ├─ Detect edge:
   │   market_yes = 0.10
   │   edge = (0.131 - 0.10) / 0.10 = 31%
   │   31% > MIN_EDGE (12%) ✓
   │
   ├─ Check balance:
   │   balance = $100 ✓ (enough for $1 trade)
   │
   ├─ Check daily loss:
   │   today_loss = -$2.50 (under limit of $5) ✓
   │
   ├─ Execute trade:
   │   executor.place_order(market_id=xyz, side=YES, size=1.0)
   │   (Paper mode: simulates. Live mode: sends to Polymarket)
   │
   ├─ Record position:
   │   positions[market_id] = {
   │       entry_price: 0.10,
   │       size: 1.0,
   │       entry_time: now(),
   │       balance: 99.90
   │   }
   │
   ├─ Notify:
   │   telegram.send("🟢 BTC UP edge 31% @ $46,900")
   │
   └─ Log:
       audit.log("ENTRY", market_id, side=YES, price=0.10, edge=0.31)
```

### Scenario 2: Market Exit (Expiration)

```
1. orderbook_feed: BTC price went to $47,500 (above strike)
2. market_discovery: Time to expiration = 5s (< threshold)
3. Main loop:
   ├─ Position check:
   │   position still active? Yes
   │   time_to_expiration < 5? Yes → EXIT
   │
   ├─ Exit decision:
   │   If expiring on YES: close at current YES price (~$0.90)
   │   If expiring on NO: close at current NO price (~$0.10)
   │
   ├─ Calculate PnL:
   │   entry: $0.10, exit: $0.90
   │   pnl = (0.90 - 0.10) × 1.0 = +$0.80 ✓
   │
   ├─ Close position:
   │   executor.close_position(market_id)
   │
   ├─ Update state:
   │   positions[market_id].exit_time = now()
   │   positions[market_id].pnl = 0.80
   │   today_pnl += 0.80
   │
   ├─ Notify:
   │   telegram.send("🟡 Exit: +$0.80 PnL (8x on $1)")
   │
   └─ Log:
       audit.log("EXIT", market_id, side=YES, exit_price=0.90, pnl=0.80)
```

### Scenario 3: Kill Switch (Daily Loss Exceeded)

```
1. Current PnL today: -$5.01 (exceeded limit of $5.00)
2. Main loop:
   ├─ Check: daily_loss > MAX_DAILY_LOSS? Yes
   ├─ Action: FREEZE trading
   ├─ Notify: telegram.send("⛔ Daily loss limit reached. Paused.")
   └─ Log: audit.log("FREEZE", reason="daily_loss_exceeded")

Next trades: REJECTED (paused until next day at 00:00 UTC)
```

---

## Risk Management

### Position-level

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `ORDER_SIZE_USDC` | 1.0 | Never risk >1% per trade |
| `MIN_EDGE` | 12% | Only high-conviction trades |
| `ENTRY_WINDOW_S` | 90 | Avoid illiquid markets |
| `EXIT_AFTER_S` | 30 | Don't hold >30s if edge disappears |

### Portfolio-level

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `MAX_DAILY_LOSS_USDC` | 5.0 | Stop trading if down $5 |
| `MAX_POSITIONS` | 10 | Never hold >10 simultaneous trades |
| `MAX_EXPOSURE_USDC` | 50.0 | Never risk >$50 at once |

### Monitoring

```
Health metrics:
├─ Feed latency (Binance, Chainlink) < 500ms
├─ Market discovery latency < 1000ms
├─ Order execution latency < 2000ms
├─ Daily win rate > 40%
├─ Average edge > 15%
```

---

## Backtesting & Validation

The bot includes offline testing before live trading:

**Paper Mode Workflow:**
```
1. Run bot with TRADING_MODE=PAPER
2. Simulates all trades using real market prices
3. Accumulates fictitious PnL
4. Logs all decisions to audit.log
5. After 100 trades, analyze:
   - Win rate: aim >50%
   - Average edge: aim >15%
   - Drawdown: should stay <10%
6. If satisfied, switch to LIVE_MODE
```

**Example Paper Mode Output:**
```
[PAPER] Trade #1: BTC-UP market
Entry: YES @ $0.105 (edge 26%)
Exit: YES @ $0.890 (expiration)
PnL: +$0.785 (7.5x return)
---
[PAPER] Trade #2: BTC-DOWN market
Entry: NO @ $0.150 (edge 18%)
Exit: NO @ $0.120 (edge gone)
PnL: -$0.030 (loss)
---
Paper PnL today: +$0.755 on 2 trades
```

---

## Deployment

### Local Development

```bash
git clone https://github.com/Arcan17/polymarket-latevalue-bot.git
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: set TRADING_MODE=PAPER

python main.py
# In another terminal:
python dashboard.py
```

### Docker

```dockerfile
FROM python:3.9-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV TRADING_MODE=PAPER
CMD ["python", "main.py"]
```

```bash
docker build -t polymarket-bot .
docker run -e TRADING_MODE=PAPER polymarket-bot
```

---

## Performance Characteristics

| Operation | Latency |
|-----------|---------|
| Fetch BTC spot (Binance) | <50ms |
| Fetch market list | <100ms |
| Black-Scholes calculation | <1ms |
| Edge detection | <5ms |
| Order placement (Polymarket) | <500ms |
| **Total decision-to-execution** | **<700ms** |

---

## Failure Modes & Recovery

| Failure | Impact | Recovery |
|---------|--------|----------|
| Binance API down | No price data | Fall back to Chainlink RTDS |
| Chainlink RTDS down | No price verification | Fall back to Binance |
| Polymarket CLOB down | Can't execute | Pause bot, retry every 30s |
| Network latency >2s | Stale prices | Skip this market |
| Daily loss limit hit | Bot pauses | Automatic at 00:00 UTC |

---

## Monitoring Dashboard

The bot provides real-time visibility via `dashboard.py`:

```
┌─ Late Value Bot v1.0 ──────────────────────────────────┐
│                                                         │
│ Status: RUNNING (PAPER MODE)                          │
│ Uptime: 2h 34m                                         │
│                                                         │
│ PRICE FEEDS                                            │
│ ├─ Binance: $46,920.50 (100ms)  ✓                    │
│ └─ Chainlink: $46,921.10 (45ms)  ✓                   │
│                                                         │
│ MARKETS DISCOVERED: 3 active                          │
│ ├─ BTC-UP @ 47000 (60s left) — YES:$0.10, NO:$0.90  │
│ ├─ BTC-DOWN @ 46500 (45s left) — YES:$0.15, NO:$0.85│
│ └─ BTC-UP @ 47500 (120s left) — YES:$0.08, NO:$0.92 │
│                                                         │
│ POSITIONS (2 open)                                     │
│ ├─ Market MLC-UP #1: +$0.78 YTD (exp in 58s)         │
│ └─ Market MLC-DOWN #2: -$0.03 (exp in 43s)           │
│                                                         │
│ DAILY PnL: +$1.23 (42 trades, 64% win rate)          │
│                                                         │
│ LAST 5 TRADES                                          │
│ 10:34 → ENTRY BTC-UP @ $0.125, edge 22%              │
│ 10:32 → EXIT BTC-DOWN, +$0.15 PnL                    │
│ 10:31 → ENTRY BTC-DOWN @ $0.145, edge 18%            │
│ 10:29 → EXIT BTC-UP, +$0.62 PnL                      │
│ 10:27 → ENTRY BTC-UP @ $0.108, edge 28%              │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## Code Quality & Safety

- **Type hints:** 100% coverage (mypy strict mode)
- **Tests:** Unit tests for vol_estimator + evaluator
- **Linting:** Code passes black + flake8
- **Logging:** Every trade + decision logged
- **Audit trail:** Immutable record of all trades

---

## Security Considerations

### Private Keys
- **Never** hardcoded in source code
- Always in `.env` (git-ignored)
- Only loaded at runtime
- Rotated regularly on production

### API Keys
- Polymarket API key stored in `.env`
- Binance: public API (no secret needed)
- Chainlink: public WebSocket (no auth)

### Rate Limiting
- Polymarket CLOB: ~100 requests/sec (sufficient)
- Binance: ~1200 requests/min (ample)
- Chainlink RTDS: WebSocket (no rate limit)

---

## Future Enhancements

### Short-term
- [ ] Multi-market simultaneous trading (currently 1 per check)
- [ ] Weighted edge calculation (time-to-expiration penalty)
- [ ] Price impact modeling (slippage estimation)

### Medium-term
- [ ] Machine learning volatility prediction
- [ ] Market-making mode (both sides simultaneously)
- [ ] Integration with other exchanges (Manifold, Metaculus)

### Long-term
- [ ] Decentralized bot (run on chain)
- [ ] Portfolio optimization (Sharpe ratio maximization)
- [ ] Team collaboration (shared capital pool)
