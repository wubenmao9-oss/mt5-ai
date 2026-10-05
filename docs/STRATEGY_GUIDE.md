# MT5-Ai 策略指南

## 项目结构
```
MT4-Ai/
├── main.py                       # 主入口（当前使用 V2）
├── .env                          # 账号配置
├── STRATEGY_GUIDE.md             # ← 本文档
├── data/
│   ├── memory.json               # 交易记忆 (TradeRecord)
│   └── trades.db                 # SQLite 决策日志
└── src/
    ├── market_data/models.py     # Tick / KlineBar 数据模型
    ├── trading/mt5_executor.py   # MT5 执行器（单例）
    ├── notifier/
    │   └── email_alerter.py      # 邮件报警模块
    └── strategy/
        ├── config.py             # StrategyConfig (可调参数)
        ├── indicators.py         # 技术指标 (SMA, RSI, BB, ATR, ADX)
        ├── recorder.py           # SQLite 记录器
        ├── engine.py             # V1 旧版引擎（保留）
        ├── engine_v2.py          # ★ V2 双状态机引擎（当前在用）
        └── backtester.py         # V1 回测器
```

## 策略对比

| 特性 | V1 (engine.py) | V2 (engine_v2.py) ★ | V3 狙击 |
|------|---------------|-------------------|---------|
| 状态 | 保留 | **当前使用** | 实验性 |
| 核心指标 | SMA + RSI | **ADX(14) + BB(20,2)** | RSI(14)交叉 |
| 状态机 | 无 | **RANGING / TRENDING** | 无 |
| 入场逻辑 | 价格触BB + RSI | **ADX≥20 + BB突破+DI方向** | RSI超卖/买反转 |
| 止损 | ATR×1.5 | **入场±0.5×带宽 / 蜡烛极值** | 固定-$10 |
| 止盈 | ATR×3 | **中轨 / 2.5×SL** | 固定+$17 |
| 触碰锁定 | 无 | **3次触壁锁定方向** | 无 |
| 冷却机制 | 固定时间 | **交易后 + Session过滤 + 连败连胜** | 3单/天限额 |
| 0.01手回测(30d) | -17.80 | **+137.51** | 不稳定 |

## V2 策略详情 (当前在用)

### 核心参数 (src/strategy/config.py)
```python
volume = 0.01                     # 固定 0.01 手
trend_sma_fast = 20               # H1 快线
trend_sma_slow = 50               # H1 慢线
trend_rsi_period = 14             # H1 RSI
entry_bb_period = 20              # M5 布林带周期
entry_bb_std = 2.0                # 布林带标准差
entry_rsi_period = 7              # M5 RSI
rsi_overbought = 70.0
rsi_oversold = 30.0
cooldown_after_trade_minutes = 60
cooldown_no_signal_minutes = 60
cooldown_error_minutes = 15
cleanup_hour = 23                 # 风控清理时间
cleanup_minute = 55
```

### 双状态机逻辑
```
Tick → TickAggregator (5min K线)
                ↓
        指标计算 → ADX(14) + DI + BB(20,2) + ATR(14)
                ↓
        ┌─ ADX ≥ 20 + BB突破 + DI方向 + 实体>0.3×ATR
        │     └→ TRENDING: 顺势突破，SL=蜡烛极值
        │
        └─ ADX < 25 + 触布林带
              └→ RANGING: 均值回归，SL=0.5×带宽, TP=中轨
                    └→ 3次触壁锁定 + 带内衰减
```

### 冷却机制
| 类型 | 触发条件 | 时长 |
|------|---------|------|
| 交易后冷却 | 每笔平仓后 | 60分钟 |
| 无信号冷却 | 趋势不明 | 60分钟 |
| Session过滤 | 亚盘早盘/欧盘前/美盘前 | 30分钟 |
| 连败连胜冷却 | 3连胜 或 3连败 | 30分钟 |

### 风控
- 每天 23:55 自动撤销所有挂单 + 平仓
- Session 过滤避免低流动性时段交易

### 回测结果 (XAUUSD, 0.01手)
| 周期 | 交易数 | 胜率 | 净利润 | PF |
|------|--------|------|--------|----|
| 30天 | 518 | 54.2% | +137.51 | 1.05 |
| 60天 | 991 | 54.3% | +390.89 | 1.08 |
| 90天 | 1479 | 52.9% | +244.57 | 1.03 |

## 如何切换策略

当前 main.py 使用的是 V2 引擎 (StrategyEngineV2)。
如需切换回 V1，修改 main.py 的 import:
```python
# 当前 (V2):
from src.strategy import StrategyEngineV2, StrategyConfig

# 切换到 V1:
from src.strategy.engine import StrategyEngine
```
