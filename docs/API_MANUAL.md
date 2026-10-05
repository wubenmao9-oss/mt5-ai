# MT5-Ai Backend API Reference

> This document is intended for frontend developers. All endpoints return JSON.
> CORS is already configured for all origins (`allow_origins=["*"]`).
> Base URL: `http://localhost:8000`

---

## Table of Contents

1. [GET /api/status - System Status](#1-get-apistatus---system-status)
2. [GET /api/positions - Active Positions](#2-get-apipositions---active-positions)
3. [POST /api/killswitch - Emergency Killswitch](#3-post-apikillswitch---emergency-killswitch)
4. [GET /api/config - Strategy Config](#4-get-apiconfig---strategy-config)
5. [POST /api/config - Hot Update Config](#5-post-apiconfig---hot-update-config)
6. [GET /api/trades - Trade Records](#6-get-apitrades---trade-records)
7. [GET /api/trades/stats - Trade Statistics](#7-get-apitradesstats---trade-statistics)
8. [GET /api/klines - K-Line Data](#8-get-apiklines---k-line-data)
9. [GET /api/decisions - Decision Markers](#9-get-apidecisions---decision-markers)
10. [GET / - Static Frontend](#10-get---static-frontend)
11. [Appendix: State Values and Enums](#11-appendix-state-values-and-enums)

---

## 1. GET /api/status - System Status

Polling endpoint. Returns heartbeat, account data, risk control state, and engine status.

**This endpoint merges three data sources:**
- Shared engine state file (written by engine process every 30s)
- Live MT5 account info (fetched by web process directly)
- SQLite database trade statistics

### Response 200

```json
{
  "status": "success",
  "data": {
    "engine_state": "RUNNING",
    "engine_info": {
      "state": "RANGING",
      "streak": "0W/0L",
      "silence_reason": "",
      "position": null,
      "consecutive_losses": 0
    },
    "daily_pnl": 5.20,
    "daily_trades": 3,
    "daily_max_drawdown": -2.10,
    "macro_bias": "BULLISH",
    "cooldown_active": false,
    "last_error": "",
    "account": {
      "balance": 200.00,
      "equity": 205.20,
      "margin_free": 190.50,
      "margin_level": 500.0,
      "leverage": 500,
      "currency": "USD",
      "server": "TradeMaxGlobal-Demo",
      "name": "jiacong wu"
    },
    "stats": {
      "total_trades": 87,
      "wins": 42,
      "win_rate": 48.3,
      "total_pnl": -153.20
    },
    "timestamp": "2026-07-10T15:48:00"
  }
}
```

### Field Reference

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `engine_state` | string | shared file | `INIT` / `RUNNING` / `SLEEP` / `KILLED` / `ERROR` |
| `engine_info.state` | string | shared file | Strategy-specific mode: `RANGING` / `TRENDING` / `SCALPING` / `INIT` |
| `engine_info.streak` | string | shared file | Win/loss streak, e.g. `"3W/1L"` |
| `engine_info.silence_reason` | string | shared file | Reason engine is not trading (empty = normal) |
| `engine_info.position` | int/null | shared file | Current open position ticket, or `null` |
| `engine_info.consecutive_losses` | int | shared file | Count of consecutive losing trades |
| `daily_pnl` | float | shared file | Today realized PnL in USD |
| `daily_trades` | int | shared file | Today trade count |
| `daily_max_drawdown` | float | shared file | Today max drawdown in USD |
| `macro_bias` | string | shared file | H1 trend: `BULLISH` / `BEARISH` / `NEUTRAL` |
| `cooldown_active` | bool | shared file | `true` if engine is in cooldown/sleep |
| `last_error` | string | shared file | Last error message (empty = no error) |
| `account` | object/null | MT5 live | MT5 account info. `null` if connection failed |
| `account.balance` | float | MT5 live | Account balance |
| `account.equity` | float | MT5 live | Account equity |
| `account.margin_free` | float | MT5 live | Free margin |
| `account.margin_level` | float | MT5 live | Margin level percentage |
| `account.leverage` | int | MT5 live | Account leverage |
| `account.currency` | string | MT5 live | Account currency (e.g. `"USD"`) |
| `account.server` | string | MT5 live | Broker server name |
| `account.name` | string | MT5 live | Account owner name |
| `stats` | object | SQLite | Historical trade statistics |
| `stats.total_trades` | int | SQLite | Total closed trades |
| `stats.wins` | int | SQLite | Winning trades count |
| `stats.win_rate` | float | SQLite | Win rate percentage |
| `stats.total_pnl` | float | SQLite | Total PnL from all closed trades |
| `timestamp` | string | server | ISO-8601 UTC timestamp |

### Frontend Polling Recommendation

| Field | Rate | Notes |
|-------|------|-------|
| `/api/status` | every 3-5 seconds | Main dashboard heartbeat |

---

## 2. GET /api/positions - Active Positions

Returns all currently open positions from MT5.

### Response 200

```json
{
  "status": "success",
  "data": [
    {
      "ticket": 404970449,
      "symbol": "XAUUSD",
      "direction": "BUY",
      "volume": 0.01,
      "open_price": 4114.69,
      "current_price": 4115.20,
      "sl": 4104.69,
      "tp": 4124.69,
      "profit": 0.51,
      "swap": -0.02,
      "commission": -0.17,
      "open_time": "2026-07-10T15:48:00",
      "duration_min": 12.5,
      "sl_status": "initial",
      "magic": 0,
      "comment": ""
    }
  ]
}
```

### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `ticket` | int | MT5 order ticket number |
| `symbol` | string | Trading symbol (e.g. `"XAUUSD"`) |
| `direction` | string | `"BUY"` or `"SELL"` |
| `volume` | float | Lot size |
| `open_price` | float | Entry price |
| `current_price` | float | Current market price |
| `sl` | float/null | Current stop loss price. `null` = no SL |
| `tp` | float/null | Current take profit price. `null` = no TP |
| `profit` | float | Floating PnL in USD |
| `swap` | float | Overnight swap charges |
| `commission` | float | Commission charged |
| `open_time` | string | ISO-8601 open time |
| `duration_min` | float | Minutes since open |
| `sl_status` | string | SL state: `"initial"` / `"break_even"` / `"profit_lock"` |
| `magic` | int | MT5 Magic Number (strategy identifier) |
| `comment` | string | Order comment |

### sl_status Values

| Value | Meaning | Trigger |
|-------|---------|---------|
| `"initial"` | Original SL, not modified | New position |
| `"break_even"` | SL moved to break-even | Profit >= 10 USD |
| `"profit_lock"` | SL moved to lock profit | Profit >= 12 USD |

### Frontend Polling Recommendation

| Field | Rate | Notes |
|-------|------|-------|
| `/api/positions` | every 5-10 seconds | Active position cards |

---

## 3. POST /api/killswitch - Emergency Killswitch

Closes all open positions, cancels all pending orders, and sets engine state to `KILLED`.

### Response 200

```json
{
  "status": "success",
  "closed_positions": 1,
  "cancelled_orders": 3
}
```

| Field | Type | Description |
|-------|------|-------------|
| `closed_positions` | int | Number of positions successfully closed |
| `cancelled_orders` | int | Number of pending orders cancelled |
| `message` | string | Error message (only on failure) |

### Frontend Usage

```javascript
// Killswitch button handler
async function killswitch() {
  if (!confirm("Kill all positions?")) return;
  const res = await fetch("/api/killswitch", { method: "POST" });
  const data = await res.json();
  alert(`Closed ${data.closed_positions} positions`);
}
```

---

## 4. GET /api/config - Strategy Config

Returns the current strategy configuration parameters.

### Response 200

```json
{
  "symbol": "XAUUSD",
  "volume": 0.01,
  "trend_sma_fast": 20,
  "trend_sma_slow": 50,
  "entry_bb_period": 20,
  "entry_bb_std": 2.0,
  "active_strategy": "B2"
}
```

If engine is not started, returns a minimal fallback:
```json
{
  "symbol": "XAUUSD",
  "volume": 0.01,
  "_note": "Read-only fallback"
}
```

---

## 5. POST /api/config - Hot Update Config

Dynamically update strategy parameters without restarting the engine.

### Request

```json
[
  { "key": "volume", "value": 0.02 },
  { "key": "entry_bb_period", "value": 30 }
]
```

### Response 200

```json
{
  "status": "ok",
  "updated": ["volume", "entry_bb_period"]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `updated` | array[string] | List of successfully updated parameter names |
| HTTP 503 | - | Strategy not started |

### Notes

- Type coercion is automatic: the new value is cast to the existing parameter type.
- If a parameter name does not exist or cast fails, it is silently skipped.
- Changes take effect on the next tick/candle processing cycle.

---

## 6. GET /api/trades - Trade Records

Returns historical decision records from SQLite. Uses the `decisions` table (strategy decisions).

### Query Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 100 | Number of records to return |
| `offset` | 0 | Pagination offset |

### Response 200

```json
[
  {
    "id": 1,
    "timestamp": "2026-07-10T15:30:00",
    "direction": "SELL",
    "strategy": "B2",
    "price": 4112.50,
    "volume": 0.01,
    "order_id": 404970449,
    "reason": "BB touch + RSI oversold",
    "indicators": "SMA_F=4115.0 SMA_S=4110.5 RSI=44.2",
    "profit": 1.23,
    "close_time": "2026-07-10T15:45:00",
    "hold_minutes": 15.0
  }
]
```

---

## 7. GET /api/trades/stats - Trade Statistics

Aggregated statistics from the `trades` table (closed trades only).

### Response 200

```json
{
  "total_trades": 87,
  "wins": 42,
  "win_rate": 48.3,
  "total_pnl": -153.20
}
```

---

## 8. GET /api/klines - K-Line Data

Fetches historical OHLCV data from MT5.

### Query Parameters

| Parameter | Default | Options | Description |
|-----------|---------|---------|-------------|
| `symbol` | XAUUSD | any MT5 symbol | Trading symbol |
| `timeframe` | M5 | M1, M5, M15, M30, H1, H4, D1 | Candle period |
| `count` | 200 | 1-1000 | Number of candles |

### Response 200

```json
[
  {
    "time": 1783551234,
    "open": 4114.50,
    "high": 4116.20,
    "low": 4113.80,
    "close": 4115.10,
    "volume": 1234
  }
]
```

### Frontend Usage (Lightweight Charts)

```javascript
import { createChart } from "lightweight-charts";
const chart = createChart(document.getElementById("chart"), { width: 800, height: 400 });
const series = chart.addCandlestickSeries();
const data = await fetch("/api/klines?symbol=XAUUSD&timeframe=M5&count=200").then(r => r.json());
series.setData(data.map(d => ({
  time: d.time,
  open: d.open,
  high: d.high,
  low: d.low,
  close: d.close,
})));
```

---

## 9. GET /api/decisions - Decision Markers

Returns strategy decision points for overlay on K-line charts.

### Query Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 50 | Number of records |
| `offset` | 0 | Pagination offset |

### Response 200

```json
[
  {
    "timestamp": "2026-07-10T15:30:00",
    "direction": "range_buy",
    "price": 4112.50,
    "reason": "BB touch + RSI divergence"
  }
]
```

### direction Values

| Value | Meaning |
|-------|---------|
| `"range_buy"` | Ranging mode buy signal |
| `"range_sell"` | Ranging mode sell signal |
| `"trend_buy"` | Trend mode buy signal |
| `"trend_sell"` | Trend mode sell signal |
| `"limit_buy"` | Pending limit buy order |
| `"limit_sell"` | Pending limit sell order |

---

## 10. GET / - Static Frontend

Serves `static/index.html` if it exists.

If no frontend has been built, returns a plain text HTML fallback.

---

## 11. Appendix: State Values and Enums

### engine_state

| Value | Meaning | Trigger |
|-------|---------|---------|
| `INIT` | Engine just started | On startup |
| `RUNNING` | Normal operation | After strategy activation |
| `SLEEP` | Target reached / quota exhausted | Daily profit >= 50 USD or trades >= 3 |
| `KILLED` | Emergency killswitch triggered | Via POST /api/killswitch |
| `ERROR` | Unexpected error | Engine catches exception |

### macro_bias

| Value | Meaning |
|-------|---------|
| `BULLISH` | H1 SMA(20) > SMA(50) + RSI > 50 |
| `BEARISH` | H1 SMA(20) < SMA(50) + RSI < 50 |
| `NEUTRAL` | H1 direction unclear |

### sl_status

| Value | Meaning | Condition |
|-------|---------|-----------|
| `initial` | Original SL, not moved | Default |
| `break_even` | SL moved to break-even | Floating profit >= 10 USD |
| `profit_lock` | SL moved to lock profit | Floating profit >= 12 USD |

---

## Error Response Format

All endpoints return consistent error format on failure:

```json
{
  "status": "error",
  "message": "Human-readable error description"
}
```

For endpoints that normally return `data`, errors may include it:

```json
{
  "status": "error",
  "message": "MT5 connection failed",
  "data": []
}
```

---

## CORS Configuration

- Origins: `*` (all origins allowed)
- Methods: `GET`, `POST`, `OPTIONS`
- Headers: `*` (all headers allowed)
- Credentials: `true`

No preflight concerns for the listed endpoints.
No additional headers needed for most fetch API calls.

---

## Network Diagram

```
┌─────────────────────────────────┐
│          run.py                 │
│  (Launcher - parent process)    │
└────────────┬────────────────────┘
             │
    ┌────────┴────────┐
    ▼                  ▼
┌────────────┐  ┌────────────┐
│  main.py   │  │  web.py    │
│  (Engine)  │  │  (FastAPI) │
│            │  │            │
│  - MT5     │  │  - MT5     │
│  - Strategy│  │    (lazy)  │
│  - Trades  │  │  - API     │
│            │  │  - Static  │
└──────┬─────┘  └──────┬─────┘
       │                │
       │  Shared JSON   │
       │  state file    │
       └──────┬─────────┘
              ▼
       ┌──────────────┐
       │ engine_state │
       │   .json      │
       └──────────────┘
```

The engine process (main.py) writes its state to `data/engine_state.json` every 30 seconds.
The web process (web.py) reads this file on each `/api/status` call.
Live MT5 data (positions, account info, klines) is fetched directly by the web process.

---

## Frontend Integration Checklist

- [ ] Poll `/api/status` every 3-5s → render dashboard header
- [ ] Poll `/api/positions` every 5-10s → render position cards/table
- [ ] Call `POST /api/killswitch` on emergency button (with confirmation)
- [ ] Call `GET /api/trades` on history page
- [ ] Call `GET /api/klines` on review chart page
- [ ] Overlay `GET /api/decisions` markers on chart
- [ ] Use `POST /api/config` for strategy parameter panel

---

## Change Log

| Date | Version | Changes |
|------|---------|---------|
| 2026-07-10 | 1.0 | Initial API reference, shared state file IPC architecture |
