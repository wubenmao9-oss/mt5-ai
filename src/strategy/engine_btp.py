"""BTP: 超短线强弱动能交易 (M1入场超单)

回测最优参数 (FxPro GOLD, 点差0.23, 41天 M1数据):
  TP=0.50 SL=0.40 Hold=3根 评分≥1 无智能出场
  1062笔 WR51.8% PnL+90.7U PF1.44 28/13盈日
  日均+2.21U 翻仓150U→241U(+60.4%)

三周期框架:
  M15: 关键位 (Swing High/Low)
  M5:  方向 (EMA20+FVG)
  M1:  入场 (动能枯竭+发力确认)

出场条件:
  1. TP=0.50 / SL=0.40 (固定, 基于实际成交价ask/bid, broker挂单)
  2. 智能出场已禁用 (回测显示无智能更好)
  3. 快速止盈: 盈利≥TP的80% → 离场
  4. 持仓超180秒 → 强制平仓
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np

try:
    from ..market_data.models import Tick
    from ..trading.mt5_executor import MT5Executor
    from .config import StrategyConfig
    from .recorder import Recorder
    from ..notifier.email_alerter import EmailAlerter
    from .base import BaseStrategy
    from .indicators import EMA, ATR
except ImportError:
    from src.market_data.models import Tick
    from src.trading.mt5_executor import MT5Executor
    from src.strategy.config import StrategyConfig
    from src.strategy.recorder import Recorder
    from src.notifier.email_alerter import EmailAlerter
    from src.strategy.base import BaseStrategy
    from src.strategy.indicators import EMA, ATR

logger = logging.getLogger(__name__)

# ── 风控 (150U / 0.01手 XAUUSD) ─────────────────────────────────
ACCOUNT_SIZE = 150.0
MAX_VOLUME = 0.01
MAX_DAILY_LOSS = 6.0
DAILY_TARGET = 8.0
MAX_DAILY_TRADES = 50       # 超单高频
CONSEC_LOSS_LIMIT = 5

# ── 出场参数 (FxPro GOLD点差0.23回测最优) ───────────────────────
TP_DIST = 0.50              # 固定止盈 0.50价格点 (net 0.27 after spread)
SL_DIST = 0.40              # 固定止损 0.40价格点 (> spread 0.23)
MAX_HOLD_SEC = 180          # 最大持仓3根M1=180秒
MIN_SCORE = 1              # 评分≥1才入场 (回测: +90.7U vs +86.6U)
STAGNATE_SEC = 3            # 3秒不动→停滞 (已禁用, 回测显示无智能更好)
STAGNATE_MOVE = 0.02        # 价格变动<0.02算"不动"
SMART_EXIT_BODY_RATIO = 0.2 # 小实体: body < ATR*0.2
SMART_EXIT_CROSS_HI = 2     # 均线缠绕: 5根内穿越>=2
SMART_EXIT_CROSS_LO = 1     # 均线稳定: 5根内穿越<=1
SMART_EXIT_ENABLED = False  # 智能出场已禁用 (回测: 无智能+2.4U)

# ── 数据目录 ──────────────────────────────────────────────────────
def _btp_data_dir():
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "data"
    try:
        p = Path(__file__).resolve().parent.parent.parent / "data"
        if p.exists(): return p
    except Exception: pass
    return Path.cwd() / "data"

BTP_DATA_DIR = _btp_data_dir()
DAILY_STATE_FILE = BTP_DATA_DIR / "btp_daily_state.json"


# ═══════════════════════════════════════════════════════════════════
# 工具函数 (回测共用)
# ═══════════════════════════════════════════════════════════════════
def _body(c): return abs(c["c"] - c["o"])
def _is_bull(c): return c["c"] >= c["o"]
def _is_bear(c): return c["c"] < c["o"]
def _wick_up(c): return c["h"] - max(c["o"], c["c"])
def _wick_dn(c): return min(c["o"], c["c"]) - c["l"]
def _range(c): return c["h"] - c["l"]
def _is_engulfing(prev, curr):
    if _is_bull(curr) and _is_bear(prev):
        return curr["c"] > prev["o"] and curr["o"] < prev["c"]
    if _is_bear(curr) and _is_bull(prev):
        return curr["c"] < prev["o"] and curr["o"] > prev["c"]
    return False


# ═══════════════════════════════════════════════════════════════════
# FVG 检测
# ═══════════════════════════════════════════════════════════════════
def detect_fvg(bars, lookback=30):
    fvgs = []
    n = len(bars)
    start = max(2, n - lookback)
    for i in range(start, n):
        if bars[i-2]["h"] < bars[i]["l"]:
            fvgs.append({"type": "bull", "high": bars[i]["l"], "low": bars[i-2]["h"], "idx": i})
        elif bars[i-2]["l"] > bars[i]["h"]:
            fvgs.append({"type": "bear", "high": bars[i-2]["l"], "low": bars[i]["h"], "idx": i})
    return fvgs


# ═══════════════════════════════════════════════════════════════════
# 关键位 (Swing High/Low)
# ═══════════════════════════════════════════════════════════════════
def detect_key_levels(bars, lookback=50, strength=3):
    levels = []
    n = len(bars)
    start = max(strength, n - lookback)
    end = n - strength
    for i in range(start, end):
        is_hi = all(bars[i]["h"] >= bars[i-j]["h"] for j in range(1, strength+1)) and \
                all(bars[i]["h"] >= bars[i+j]["h"] for j in range(1, strength+1))
        if is_hi: levels.append({"type": "resist", "price": bars[i]["h"]})
        is_lo = all(bars[i]["l"] <= bars[i-j]["l"] for j in range(1, strength+1)) and \
                all(bars[i]["l"] <= bars[i+j]["l"] for j in range(1, strength+1))
        if is_lo: levels.append({"type": "support", "price": bars[i]["l"]})
    if not levels: return levels
    merged = [levels[0]]
    for lv in levels[1:]:
        if abs(lv["price"] - merged[-1]["price"]) < 1.5:
            if lv["type"] != merged[-1]["type"]:
                merged[-1]["type"] = "both"
        else:
            merged.append(lv)
    return merged


# ═══════════════════════════════════════════════════════════════════
# 方向判断 (用于 M5)
# ═══════════════════════════════════════════════════════════════════
def judge_direction(bars, ema20, idx, lookback=8):
    """判断方向: UP/DOWN/RANGING"""
    if idx < lookback: return "RANGING"
    recent = bars[idx-lookback+1:idx+1]
    ev = ema20[idx-lookback+1:idx+1]
    if any(np.isnan(v) for v in ev): return "RANGING"

    above = sum(1 for b, e in zip(recent, ev) if b["c"] > e)
    below = sum(1 for b, e in zip(recent, ev) if b["c"] < e)

    crosses = sum(1 for i in range(1, len(recent))
                  if (recent[i-1]["c"]-ev[i-1])*(recent[i]["c"]-ev[i]) < 0)
    if crosses >= 3: return "RANGING"

    bull_bars = [b for b in recent if _is_bull(b)]
    bear_bars = [b for b in recent if _is_bear(b)]

    highs_up = len(bull_bars) >= 3 and all(bull_bars[i]["h"] >= bull_bars[i-1]["h"] for i in range(1, len(bull_bars)))
    lows_up = len(bull_bars) >= 3 and all(bull_bars[i]["l"] >= bull_bars[i-1]["l"] for i in range(1, len(bull_bars)))
    highs_dn = len(bear_bars) >= 3 and all(bear_bars[i]["h"] <= bear_bars[i-1]["h"] for i in range(1, len(bear_bars)))
    lows_dn = len(bear_bars) >= 3 and all(bear_bars[i]["l"] <= bear_bars[i-1]["l"] for i in range(1, len(bear_bars)))

    if above >= lookback * 0.7 and (highs_up or lows_up): return "UP"
    if below >= lookback * 0.7 and (highs_dn or lows_dn): return "DOWN"
    return "RANGING"


# ═══════════════════════════════════════════════════════════════════
# M1 动能枯竭检测
# ═══════════════════════════════════════════════════════════════════
def detect_exhaustion_strict(bars, idx, atr_val):
    """动能枯竭检测。

    做多: 前面>=2根连续阴线(实体递减/小实体/长下影) + 当前阳线发力
    做空: 反之
    返回: "buy" / "sell" / None
    """
    if idx < 5: return None
    curr = bars[idx]
    signal = None

    # ── 做多: 空头枯竭 + 多头发力 ──
    if _is_bull(curr) and _body(curr) > atr_val * 0.15:
        bear_run = []
        for j in range(idx - 1, max(idx - 7, -1), -1):
            if _is_bear(bars[j]):
                bear_run.append((j, bars[j]))
            else:
                break

        if len(bear_run) >= 2:
            sizes = [_body(b) for _, b in bear_run]
            declining = len(sizes) >= 2 and all(sizes[i] > sizes[i+1] for i in range(len(sizes)-1))
            last_small = sizes[-1] < atr_val * 0.4
            last_wick = _wick_dn(bear_run[-1][1])
            last_range = _range(bear_run[-1][1])
            wick_reject = last_range > 0 and last_wick / last_range > 0.5
            exhausted = declining or last_small or wick_reject

            curr_body = _body(curr)
            prev_bear_body = _body(bear_run[-1][1])
            if exhausted and (curr_body > prev_bear_body * 1.2 or _is_engulfing(bear_run[-1][1], curr)):
                signal = "buy"

    # ── 做空: 多头枯竭 + 空头发力 ──
    if _is_bear(curr) and _body(curr) > atr_val * 0.15:
        bull_run = []
        for j in range(idx - 1, max(idx - 7, -1), -1):
            if _is_bull(bars[j]):
                bull_run.append((j, bars[j]))
            else:
                break

        if len(bull_run) >= 2:
            sizes = [_body(b) for _, b in bull_run]
            declining = len(sizes) >= 2 and all(sizes[i] > sizes[i+1] for i in range(len(sizes)-1))
            last_small = sizes[-1] < atr_val * 0.4
            last_wick = _wick_up(bull_run[-1][1])
            last_range = _range(bull_run[-1][1])
            wick_reject = last_range > 0 and last_wick / last_range > 0.5
            exhausted = declining or last_small or wick_reject

            curr_body = _body(curr)
            prev_bull_body = _body(bull_run[-1][1])
            if exhausted and (curr_body > prev_bull_body * 1.2 or _is_engulfing(bull_run[-1][1], curr)):
                if signal == "buy":
                    signal = None
                else:
                    signal = "sell"

    return signal


# ═══════════════════════════════════════════════════════════════════
# M1 K线聚合器 (从tick聚合)
# ═══════════════════════════════════════════════════════════════════
class M1Agg:
    def __init__(self, cb):
        self._cb = cb; self._c = None

    def add_tick(self, t):
        ts = t.timestamp or datetime.now(timezone.utc)
        m1 = self._round_m1(ts)
        if self._c is None or m1 > self._c["t"]:
            old = self._c
            self._c = {"t": m1, "o": t.bid, "h": t.bid, "l": t.bid, "c": t.bid, "v": 1}
            if old and old["v"] > 0:
                asyncio.create_task(self._cb(old))
        else:
            c = self._c
            c["h"] = max(c["h"], t.bid); c["l"] = min(c["l"], t.bid)
            c["c"] = t.bid; c["v"] += 1

    def flush(self):
        if self._c and self._c["v"] > 0:
            asyncio.create_task(self._cb(self._c))
        self._c = None

    @staticmethod
    def _round_m1(d):
        ts = int(d.timestamp()); ts = ts - (ts % 60)
        return datetime.fromtimestamp(ts, tz=timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# 策略主体
# ═══════════════════════════════════════════════════════════════════
class StrategyEngineBTP(BaseStrategy):
    """BTP — M1入场超单, TP=0.50 SL=0.40 (FxPro GOLD)"""

    def __init__(self, executor, config=None, recorder=None, emailer=None):
        self._exe = executor
        self._cfg = config or StrategyConfig()
        self._cfg.volume = MAX_VOLUME
        self._rec = recorder or Recorder()
        self._emailer = emailer
        self._running = False
        self._agg = None

        # M1 buffer (从tick聚合)
        self._m1_buf = []
        self._m1_e20 = np.array([])
        self._m1_at = np.array([])
        self._ema_cross_count = 0  # 最近5根M1的EMA20穿越次数

        # M5/M15 (从MT5预加载)
        self._m5_buf = []
        self._m15_buf = []

        # 日风控
        self._trade_count = 0
        self._daily_pnl = 0.0
        self._daily_loss = 0.0
        self._consec_loss = 0
        self._last_day = -1

        # 持仓状态
        self._pos_ticket = None
        self._pos_direction = None
        self._pos_entry = 0.0
        self._pos_open_ts = 0.0
        self._pos_last_price = 0.0
        self._pos_last_move_ts = 0.0
        self._active_trade_id = None

        self._ct = None
        self._last_m5_refresh = 0.0
        self._last_m15_refresh = 0.0
        self._last_tick = None  # 最新tick, 用于TP/SL基于实际成交价计算

    async def start(self):
        self._running = True
        self._agg = M1Agg(self._on_m1)
        self._load_daily_state()
        await self._preload()
        self._ct = asyncio.create_task(self._cleanup_loop())
        logger.info("BTP started: %s M1超单 TP=%.2f SL=%.2f Hold=%ds Score≥%d", 
                     self._cfg.symbol, TP_DIST, SL_DIST, MAX_HOLD_SEC, MIN_SCORE)

    async def stop(self):
        self._running = False
        if self._agg: self._agg.flush()
        if self._ct:
            self._ct.cancel()
            try: await asyncio.wait_for(self._ct, timeout=2)
            except Exception: pass
        self._save_daily_state()
        logger.info("BTP stopped: trades=%d pnl=%.2f", self._trade_count, self._daily_pnl)

    async def on_tick(self, tick):
        if not self._running: return
        self._last_tick = tick
        self._agg.add_tick(tick)

        # 持仓中: tick级智能出场 (价格停滞检测)
        if self._pos_ticket:
            await self._check_pos_tick(tick)

        # 定期刷新M5/M15
        now = time.time()
        if now - self._last_m5_refresh > 300:
            await self._refresh_m5(); self._last_m5_refresh = now
        if now - self._last_m15_refresh > 900:
            await self._refresh_m15(); self._last_m15_refresh = now

    # ── 日状态持久化 ──
    def _save_daily_state(self):
        try:
            BTP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            DAILY_STATE_FILE.write_text(json.dumps({
                "day": self._last_day, "trade_count": self._trade_count,
                "daily_pnl": self._daily_pnl, "daily_loss": self._daily_loss,
                "consec_loss": self._consec_loss,
            }), encoding="utf-8")
        except Exception: pass

    def _load_daily_state(self):
        if not DAILY_STATE_FILE.exists(): return
        try:
            d = json.loads(DAILY_STATE_FILE.read_text(encoding="utf-8"))
            today = int(datetime.now(timezone.utc).timestamp() / 86400)
            if d.get("day") == today:
                self._trade_count = d.get("trade_count", 0)
                self._daily_pnl = d.get("daily_pnl", 0.0)
                self._daily_loss = d.get("daily_loss", 0.0)
                self._consec_loss = d.get("consec_loss", 0)
                self._last_day = today
        except Exception: pass

    # ── 数据预加载 ──
    async def _preload(self):
        def f():
            r1 = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M1, 0, 500)
            if r1 is not None and len(r1) > 0:
                self._m1_buf = [{"t": datetime.fromtimestamp(row[0], tz=timezone.utc),
                                 "ts": float(row[0]),
                                 "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                                 "c": float(row[4]), "v": int(row[5])} for row in r1]
            r5 = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M5, 0, 200)
            if r5 is not None and len(r5) > 0:
                self._m5_buf = [{"t": datetime.fromtimestamp(row[0], tz=timezone.utc),
                                 "ts": float(row[0]),
                                 "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                                 "c": float(row[4]), "v": int(row[5])} for row in r5]
            r15 = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M15, 0, 200)
            if r15 is not None and len(r15) > 0:
                self._m15_buf = [{"t": datetime.fromtimestamp(row[0], tz=timezone.utc),
                                  "ts": float(row[0]),
                                  "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                                  "c": float(row[4]), "v": int(row[5])} for row in r15]
            self._update_m1_indicators()
        await MT5Executor._run_in_executor(f)
        self._last_m5_refresh = time.time()
        self._last_m15_refresh = time.time()
        logger.info("BTP preloaded M1=%d M5=%d M15=%d", len(self._m1_buf), len(self._m5_buf), len(self._m15_buf))

    async def _refresh_m5(self):
        def f():
            r = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M5, 0, 200)
            if r is not None and len(r) > 0:
                self._m5_buf = [{"t": datetime.fromtimestamp(row[0], tz=timezone.utc),
                                 "ts": float(row[0]),
                                 "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                                 "c": float(row[4]), "v": int(row[5])} for row in r]
        try: await MT5Executor._run_in_executor(f)
        except Exception: pass

    async def _refresh_m15(self):
        def f():
            r = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M15, 0, 200)
            if r is not None and len(r) > 0:
                self._m15_buf = [{"t": datetime.fromtimestamp(row[0], tz=timezone.utc),
                                  "ts": float(row[0]),
                                  "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                                  "c": float(row[4]), "v": int(row[5])} for row in r]
        try: await MT5Executor._run_in_executor(f)
        except Exception: pass

    def _update_m1_indicators(self):
        """更新M1的EMA20, ATR14, EMA穿越计数"""
        b = self._m1_buf
        if len(b) < 30: return
        closes = np.array([x["c"] for x in b])
        highs = np.array([x["h"] for x in b])
        lows = np.array([x["l"] for x in b])
        self._m1_e20 = EMA(closes, 20)
        self._m1_at = ATR(highs, lows, closes, 14)

        # EMA穿越计数 (最近5根)
        n = len(b)
        if n >= 6:
            crosses = 0
            for j in range(n - 5, n):
                if j > 0 and not np.isnan(self._m1_e20[j]) and not np.isnan(self._m1_e20[j-1]):
                    if (closes[j-1] - self._m1_e20[j-1]) * (closes[j] - self._m1_e20[j]) < 0:
                        crosses += 1
            self._ema_cross_count = crosses

    # ── M1 K线收盘处理 ──
    async def _on_m1(self, c):
        if not self._running: return
        c["ts"] = c["t"].timestamp()
        self._m1_buf.append(c)
        if len(self._m1_buf) > 500: self._m1_buf.pop(0)
        self._update_m1_indicators()

        if len(self._m1_buf) < 60: return

        # 日重置
        ts = c["ts"]
        dy = int(ts / 86400)
        if dy != self._last_day:
            self._trade_count = 0; self._daily_pnl = 0.0; self._daily_loss = 0.0
            self._consec_loss = 0; self._last_day = dy; self._save_daily_state()

        # 检查持仓状态 (broker可能已平仓)
        if self._pos_ticket:
            if await self._check_position_closed():
                return
            # M1收盘智能出场 (已禁用, 回测显示无智能更好)
            if SMART_EXIT_ENABLED:
                await self._check_smart_exit_m1(c)
            return

        # 无持仓 → 检查入场信号
        await self._check_entry()

    # ── 入场检测 ──
    async def _check_entry(self):
        b = self._m1_buf
        i = len(b) - 1
        if i < 60: return
        if len(self._m1_e20) == 0 or np.isnan(self._m1_e20[i]): return
        if np.isnan(self._m1_at[i]): return

        cl = b[i]["c"]; atr_val = float(self._m1_at[i])

        # 日风控
        if self._daily_pnl >= DAILY_TARGET: return
        if self._daily_loss >= MAX_DAILY_LOSS: return
        if self._trade_count >= MAX_DAILY_TRADES: return
        if self._consec_loss >= CONSEC_LOSS_LIMIT: return

        # M1动能枯竭检测
        signal = detect_exhaustion_strict(b, i, atr_val)
        if not signal: return

        # M5方向过滤 (仅逆势不做)
        m5_dir = "RANGING"
        if len(self._m5_buf) > 30:
            m5_closes = np.array([x["c"] for x in self._m5_buf])
            m5_e20 = EMA(m5_closes, 20)
            m5_idx = len(self._m5_buf) - 1
            if len(m5_e20) > m5_idx and not np.isnan(m5_e20[m5_idx]):
                m5_dir = judge_direction(self._m5_buf, m5_e20, m5_idx)

        if m5_dir == "UP" and signal == "sell": return
        if m5_dir == "DOWN" and signal == "buy": return

        # 评分 (仅记录, 不过滤)
        score = 0
        if m5_dir != "RANGING":
            if (m5_dir == "UP" and signal == "buy") or (m5_dir == "DOWN" and signal == "sell"):
                score += 2

        m5_fvgs = detect_fvg(self._m5_buf) if len(self._m5_buf) > 30 else []
        for fvg in m5_fvgs[-3:]:
            if signal == "buy" and fvg["type"] == "bull": score += 1; break
            if signal == "sell" and fvg["type"] == "bear": score += 1; break

        m1_fvgs = detect_fvg(b[:i+1])
        for fvg in m1_fvgs[-3:]:
            if signal == "buy" and fvg["type"] == "bull": score += 1; break
            if signal == "sell" and fvg["type"] == "bear": score += 1; break

        m15_levels = detect_key_levels(self._m15_buf) if len(self._m15_buf) > 10 else []
        for lv in m15_levels:
            if signal == "buy" and lv["type"] in ("support", "both") and abs(cl - lv["price"]) < atr_val * 0.5:
                score += 1; break
            if signal == "sell" and lv["type"] in ("resist", "both") and abs(cl - lv["price"]) < atr_val * 0.5:
                score += 1; break

        if i >= 1 and _is_engulfing(b[i-1], b[i]):
            if (signal == "buy" and _is_bull(b[i])) or (signal == "sell" and _is_bear(b[i])):
                score += 1

        # 评分过滤 (回测最优: 评分≥1)
        if score < MIN_SCORE: return

        # 开仓: TP/SL基于实际成交价 (BUY=ask, SELL=bid), 避免点差侵蚀
        tick = self._last_tick
        if tick is None:
            try:
                tick = await self._exe._run_in_executor(
                    lambda: mt5.symbol_info_tick(self._cfg.symbol))
            except Exception:
                return
        if tick is None: return

        if signal == "buy":
            fill_price = tick.ask  # BUY以ASK成交
            sl = round(fill_price - SL_DIST, 2)
            tp = round(fill_price + TP_DIST, 2)
        else:
            fill_price = tick.bid  # SELL以BID成交
            sl = round(fill_price + SL_DIST, 2)
            tp = round(fill_price - TP_DIST, 2)

        await self._open_order(signal, fill_price, sl, tp, score)

    # ── 开仓 ──
    async def _open_order(self, direction, price, sl, tp, score):
        vol = self._cfg.volume
        if direction == "buy":
            result = await self._exe.buy_market(self._cfg.symbol, vol, sl=sl, tp=tp)
        else:
            result = await self._exe.sell_market(self._cfg.symbol, vol, sl=sl, tp=tp)
        if not result.get("success"):
            logger.warning("BTP order failed: %s", result.get("error")); return

        ticket = result.get("order", 0); fill = result.get("price", price)
        self._pos_ticket = ticket
        self._pos_direction = direction
        self._pos_entry = fill
        self._pos_open_ts = time.time()
        self._pos_last_price = fill
        self._pos_last_move_ts = time.time()
        self._trade_count += 1

        did = self._rec.record_decision(direction=direction.upper(), strategy="BTP", price=price,
                                         volume=vol, order_id=ticket, reason="BTP_s%d" % score)
        self._active_trade_id = self._rec.record_trade(direction=direction.upper(), open_price=fill,
                                                         volume=vol, strategy="BTP", open_order_id=ticket,
                                                         decision_id=did, sl=sl, tp=tp)
        self._save_daily_state()
        logger.info("BTP %s @ %.2f sl=%.2f tp=%.2f s=%d #%d", direction.upper(), price, sl, tp, score, self._trade_count)

    # ── 持仓检查 ──
    async def _check_position_closed(self):
        """检查持仓是否已被broker平仓 (TP/SL触发)"""
        if not self._pos_ticket: return True
        try:
            pos = await self._exe._run_in_executor(lambda: mt5.positions_get(ticket=self._pos_ticket))
            if pos and len(pos) > 0: return False
            # 已平仓
            await self._handle_close()
            return True
        except Exception: return False

    # ── tick级智能出场: 价格停滞 ──
    async def _check_pos_tick(self, tick):
        """每个tick检查: 价格停滞 (3秒无动 + 有盈利 → 离场)"""
        if not self._pos_ticket: return

        # 先检查是否已被平仓
        try:
            pos = await self._exe._run_in_executor(lambda: mt5.positions_get(ticket=self._pos_ticket))
            if not pos or len(pos) == 0:
                await self._handle_close()
                return
            p = pos[0]; profit = p.profit
        except Exception: return

        price = tick.bid
        now = time.time()

        # 更新价格运动追踪
        if abs(price - self._pos_last_price) > STAGNATE_MOVE:
            self._pos_last_price = price
            self._pos_last_move_ts = now

        # 持仓超时 → 强平
        if now - self._pos_open_ts > MAX_HOLD_SEC:
            await self._exe.close_all_positions(self._cfg.symbol)
            logger.info("BTP exit: timeout %ds", int(now - self._pos_open_ts))
            return

        # 价格停滞 + 有盈利 → 离场 (已禁用, 回测显示无智能更好)
        if SMART_EXIT_ENABLED and now - self._pos_last_move_ts > STAGNATE_SEC and profit > 0:
            # 均线缠绕加强确认
            if self._ema_cross_count >= SMART_EXIT_CROSS_HI:
                await self._exe.close_all_positions(self._cfg.symbol)
                logger.info("BTP exit: stagnate+wrap profit=%.2f %ds", profit, int(now - self._pos_open_ts))
                return

        # 快速止盈: 如果盈利已超过TP的80%, 直接走
        if profit >= TP_DIST * 0.8:
            await self._exe.close_all_positions(self._cfg.symbol)
            logger.info("BTP exit: quick_tp profit=%.2f", profit)
            return

    # ── M1收盘智能出场: 反向K线 + 均线稳定 ──
    async def _check_smart_exit_m1(self, candle):
        """M1收盘时检查: 反向K线 + 均线稳定 → 离场"""
        if not self._pos_ticket: return
        b = self._m1_buf
        i = len(b) - 1
        if i < 1: return

        try:
            pos = await self._exe._run_in_executor(lambda: mt5.positions_get(ticket=self._pos_ticket))
            if not pos or len(pos) == 0: return
            p = pos[0]; profit = p.profit
        except Exception: return

        if profit <= 0: return  # 只在有盈利时考虑智能出场

        atr_val = float(self._m1_at[i]) if not np.isnan(self._m1_at[i]) else 0.3
        body = _body(candle)
        direction = self._pos_direction

        # 价格停滞 + 均线缠绕
        if body < atr_val * SMART_EXIT_BODY_RATIO and self._ema_cross_count >= SMART_EXIT_CROSS_HI:
            await self._exe.close_all_positions(self._cfg.symbol)
            logger.info("BTP exit: M1 stagnate+wrap body=%.3f cross=%d profit=%.2f", body, self._ema_cross_count, profit)
            return

        # 反向K线 + 均线稳定
        if direction == "buy" and _is_bear(candle) and body > 0.02 and self._ema_cross_count <= SMART_EXIT_CROSS_LO:
            await self._exe.close_all_positions(self._cfg.symbol)
            logger.info("BTP exit: M1 bearish+stable body=%.3f cross=%d profit=%.2f", body, self._ema_cross_count, profit)
            return
        if direction == "sell" and _is_bull(candle) and body > 0.02 and self._ema_cross_count <= SMART_EXIT_CROSS_LO:
            await self._exe.close_all_positions(self._cfg.symbol)
            logger.info("BTP exit: M1 bullish+stable body=%.3f cross=%d profit=%.2f", body, self._ema_cross_count, profit)
            return

    # ── 平仓处理 ──
    async def _handle_close(self):
        pnl = 0.0; close_price = 0.0; reason = "C"
        if self._pos_ticket:
            try:
                deals = await self._exe._run_in_executor(
                    lambda: mt5.history_deals_get(position=self._pos_ticket, from_position=0, to_position=0))
                if deals and len(deals) > 0:
                    ds = list(deals); pnl = round(sum(d.profit for d in ds), 2)
                    close_price = ds[-1].price if hasattr(ds[-1], "price") else 0.0
                    reason = "TP" if pnl > 0 else "SL"
            except Exception: pass
        self._daily_pnl += pnl
        if pnl < 0:
            self._daily_loss += abs(pnl); self._consec_loss += 1
        else:
            self._consec_loss = 0
        if self._active_trade_id and close_price:
            try: self._rec.close_trade(self._active_trade_id, close_price, pnl)
            except Exception: pass
        self._active_trade_id = None; self._save_daily_state()
        logger.info("BTP CLOSE pnl=%.2f daily=%.2f %s #%d", pnl, self._daily_pnl, reason, self._trade_count)
        self._pos_ticket = None; self._pos_direction = None

    # ── 清理循环 (日重置 + 超时平仓) ──
    async def _cleanup_loop(self):
        while self._running:
            try:
                await asyncio.sleep(10)
                if self._pos_ticket and self._pos_open_ts > 0:
                    if time.time() - self._pos_open_ts > MAX_HOLD_SEC:
                        await self._exe.close_all_positions(self._cfg.symbol)
                        logger.info("BTP cleanup: timeout close")
                now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
                if now_bj.hour == 23 and now_bj.minute >= 55:
                    if self._pos_ticket:
                        await self._exe.close_all_positions(self._cfg.symbol)
                        self._pos_ticket = None
                    await asyncio.sleep(70)
            except asyncio.CancelledError: break
            except Exception: pass

    def get_status(self):
        return {
            "state": "RUNNING" if self._running else "STOPPED",
            "running": self._running,
            "position": self._pos_ticket,
            "direction": self._pos_direction,
            "streak": "%dL" % self._consec_loss if self._consec_loss > 0 else "0L",
            "trade_count": self._trade_count,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_loss": round(self._daily_loss, 2),
            "tradeable": True,
            "strategy": "BTP",
        }
