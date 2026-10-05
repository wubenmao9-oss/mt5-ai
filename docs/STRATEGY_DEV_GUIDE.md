# MT5-Ai Strategy Development Guide

This document describes the project architecture and API for implementing
new trading strategies compatible with this system.

---

## 1. Project Structure

```
MT4-Ai/
  main.py                    # Engine entry point
  run.py                     # Launches engine + web dashboard
  .env                       # Configuration (MT5 login, strategy)
  data/trades.db             # SQLite trade database
  src/
    strategy/
      __init__.py            # Exports all StrategyEngine classes
      base.py                # BaseStrategy ABC
      config.py              # StrategyConfig dataclass
      engine_v2.py           # V2: ADX+BB state machine (reference)
      engine_v3.py           # V3: V2 + daily bias filter
      engine_v4.py           # V4: V2 + optimization filters
      manager.py             # Strategy registration/activation
      recorder.py            # SQLite recorder
      indicators.py          # SMA, EMA, RSI, BB, ATR, MACD
    trading/
      mt5_executor.py        # MT5 order execution
    market_data/
      models.py              # Tick model
    notifier/
      email_alerter.py       # Email alerts
```

## 2. The BaseStrategy Interface (Required)

File: src/strategy/base.py

```python
class BaseStrategy(ABC):
    async def start(self)          # Called once on activation
    async def stop(self)           # Called on deactivation/shutdown
    async def on_tick(self, tick)  # Called on each tick
    def get_status(self) -> dict   # Return state for logging
```

## 3. Constructor Signature (Required)

Your __init__ must accept these 4 dependencies:

```python
def __init__(self, executor, config=None, recorder=None, emailer=None):
    self._exe = executor       # MT5Executor
    self._cfg = config         # StrategyConfig
    self._rec = recorder       # Recorder (SQLite)
    self._emailer = emailer    # EmailAlerter
```

## 4. Available APIs

### 4.1 Order Execution (self._exe)

| Method | Returns | Notes |
|--------|---------|-------|
| buy_market(symbol, vol) | dict | success, order, price |
| sell_market(symbol, vol) | dict | success, order, price |
| get_quote(symbol) | dict | bid, ask, spread |
| close_all_positions(symbol) | list[dict] | closed position results |
| modify_sl(symbol, ticket, sl) | dict | modify stop loss |
| cancel_all_pending(symbol) | list[dict] | cancel limit orders |

### 4.2 Historical Data (MT5 Direct)

Use inside MT5Executor._run_in_executor:

```python
def _fetch():
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 200)
    # rates[i] = (time, open, high, low, close, tick_vol, spread, real_vol)
    closes = [r[4] for r in rates]
    return closes
result = await MT5Executor._run_in_executor(_fetch)
```

Available TFs: TIMEFRAME_M1, M5, M15, M30, H1, H4, D1

### 4.3 Recording (self._rec)

```python
self._rec.record_decision(direction, strategy, price=p, reason=r)
self._rec.record_trade(direction, price, vol, strategy=s, sl=sl, tp=tp)  # returns trade_id
self._rec.close_trade(trade_id, close_price, profit)
```

### 4.4 Email (self._emailer)

```python
self._emailer.send_order(time, direction, strategy, volume)
self._emailer.send_close(time, direction, reason, pnl, comment)
self._emailer.send_silent_warning(reason, last_time, status)
```

### 4.5 Indicators (from .indicators)

```python
from .indicators import SMA, EMA, RSI, BollingerBands, ATR, MACD
sma = SMA(closes, period=20)
bb_mid, bb_up, bb_dn = BollingerBands(closes, 20, 2.0)
atr = ATR(high, low, close, 14)
```

### 4.6 StrategyConfig

```python
self._cfg.symbol       # "XAUUSD"
self._cfg.volume       # 0.01
self._cfg.strategy_name
self._cfg.strategy_version
```

## 5. Standard Architecture Pattern

Every strategy uses this flow:

```
Tick -> TickAggregator -> M5 Candle -> _process() -> Execute Trade
```

### 5.1 TickAggregator

Converts ticks to M5 candles. See engine_v2.py for full code.

```python
self._agg = TickAggregator(symbol, self._on_m5_candle)

async def on_tick(self, tick):
    self._agg.add_tick(tick)

async def _on_m5_candle(self, candle):
    self._m5_buf.append(candle)
    if len(self._m5_buf) > 200: self._m5_buf.pop(0)
    if len(self._m5_buf) >= 50: await self._process(candle)
```

### 5.2 Indicator Calculation

```python
async def _process(self, candle):
    closes = np.array([c["close"] for c in self._m5_buf])
    highs  = np.array([c["high"]  for c in self._m5_buf])
    lows   = np.array([c["low"]   for c in self._m5_buf])
    bb_m, bb_u, bb_l = BollingerBands(closes, 20, 2.0)
    atr = ATR(highs, lows, closes, 14)
    rsi = RSI(close, 14)
```

## 6. How to Register a New Strategy

Step 1: Create the file src/strategy/engine_v5.py
Step 2: Add to src/strategy/__init__.py:
```python
from .engine_v5 import StrategyEngineV5
```
Step 3: Add to main.py (around line 50):
```python
manager.register("V5", StrategyEngineV5)
```
Step 4: Set .env: ACTIVE_STRATEGY=V5
Step 5: Run: python run.py

## 7. Reference Strategy (V2 Engine)

The V2 engine (engine_v2.py) is the reference implementation with:
- TickAggregator -> M5 candles
- ADX+BB dual state machine (TRENDING / RANGING)
- Touch counters (3-tap limit per direction)
- H1 SMA20/SMA50 macro filter
- Session cooldown (avoid Asian/European session opens)
- Daily cleanup at 23:55 BJT
- Email notifications
- SQLite trade recording

Use it as your starting template.

## 8. Backtesting Framework

Run backtests with: python bt_final.py

The framework uses mt5 historical data directly:
```python
# Data format: (time, open, high, low, close, tick_vol, spread, real_vol)
r5 = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, count)
```

Available backtesters in project root:
- bt_final.py      # V2 vs V3 static vs V3 dynamic
- bt_v3_compare.py # Alternative comparison

## 9. Strategy Checklist

| Item | Required | Notes |
|------|----------|-------|
| Inherit BaseStrategy | Yes | Implement all abstract methods |
| __init__ | Yes | Accept executor, config, recorder, emailer |
| start() | Yes | Init aggregator, load history |
| stop() | Yes | Stop loops, flush aggregator |
| on_tick() | Yes | Feed tick to aggregator |
| get_status() | Yes | Return dict for logging |
| record_decision() | Recommended | Log each decision |
| record_trade() | Recommended | Log each open trade |
| close_trade() | Recommended | Log each closed trade |
| Email alerts | Recommended | Notify on orders/closes |
| Daily cleanup | Recommended | Close at 23:55 BJT |
| Session cooldown | Recommended | Skip certain hours |

---

*Generated for MT5-Ai project - give this document to any AI agent to develop compatible strategies.*
