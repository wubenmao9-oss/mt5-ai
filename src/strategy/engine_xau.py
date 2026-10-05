"""自适应双模 — XAUUSD 终极交易策略

核心理念: 黄金76%时间震荡, 24%时间趋势 → 均值回归为主, 趋势跟踪为辅
三层架构:
  H4: 市场状态 (TRENDING / RANGING / VOLATILE)
  H1: 市场结构 (关键位, Order Block, FVG, 流动性池)
  M5: 入场时机 (形态, 动能, RSI, 量确认)

双模交易:
  RANGE模式: 在关键位附近做均值回归, 逆势入场
  TREND模式: 在FVG/OB回踩做趋势延续, 顺趋势入场

风险: 100U本金, 0.01手, 1点=1U, 固定SL/TP (回测验证最优)

v1.0.0: ATR动态SL/TP (SL被截断导致亏损)
v1.1.0: 固定SL/TP — TP=1.5 SL=0.5 回测208天+980U WR55.5% PF3.7
v2.1.0: 修复下单bug — dict误用导致多笔无止损开仓, 直接下单传SL/TP取代事后修改
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import deque

import MetaTrader5 as mt5
import numpy as np

try:
    from ..market_data.models import Tick
    from ..trading.mt5_executor import MT5Executor
    from .config import StrategyConfig
    from .recorder import Recorder
    from ..notifier.email_alerter import EmailAlerter
    from .base import BaseStrategy
    from .indicators import EMA, ATR, RSI, SMA, MACD, BollingerBands, SuperTrend, QQE
except ImportError:
    from src.market_data.models import Tick
    from src.trading.mt5_executor import MT5Executor
    from src.strategy.config import StrategyConfig
    from src.strategy.recorder import Recorder
    from src.notifier.email_alerter import EmailAlerter
    from src.strategy.base import BaseStrategy
    from src.strategy.indicators import EMA, ATR, RSI, SMA, MACD, BollingerBands, SuperTrend, QQE

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# 风控参数 (100U / 0.01手 GOLD)
# ═══════════════════════════════════════════════════════════════════
ACCOUNT_SIZE = 100.0
MAX_VOLUME = 0.01
MAX_DAILY_LOSS = 6.0          # 日最大亏损 6U (6%)
DAILY_TARGET = 10.0           # 日目标 10U
MAX_DAILY_TRADES = 20         # 日最大交易数
CONSEC_LOSS_LIMIT = 4         # 连亏4笔暂停

# ═══════════════════════════════════════════════════════════════════
# 市场状态参数
# ═══════════════════════════════════════════════════════════════════
ADX_TREND_THRESHOLD = 25      # ADX > 25 → 趋势
ADX_RANGE_THRESHOLD = 20      # ADX < 20 → 震荡
ADX_LOOKBACK = 14             # ADX计算周期

# ═══════════════════════════════════════════════════════════════════
# 固定SL/TP参数 (回测v2验证: 208天 +980U WR55.5% PF3.7)
# ═══════════════════════════════════════════════════════════════════
TP_DIST = 1.5                 # 止盈距离 (点)
SL_DIST = 0.5                 # 止损距离 (点)  → R:R = 3:1
MIN_SCORE = 4                 # 最小入场评分

# ═══════════════════════════════════════════════════════════════════
# 均值回归参数 (RANGE模式)
# ═══════════════════════════════════════════════════════════════════
RANGE_RSI_OVERBOUGHT = 70     # RSI超买
RANGE_RSI_OVERSOLD = 30       # RSI超卖
RANGE_MIN_ATR = 12.0          # 回测OOS验证: ATR≥12时RANGE模式WR从52%→57%
RANGE_MAX_ATR = 30.0          # ATR太大不交易 (>30点波动太危险)
RANGE_KEY_LEVEL_DIST = 0.5    # 价格距关键位<0.5*ATR才算"接近"

# ═══════════════════════════════════════════════════════════════════
# 趋势延续参数 (TREND模式)
# ═══════════════════════════════════════════════════════════════════
TREND_EMA_FAST = 20           # 快EMA
TREND_EMA_SLOW = 50           # 慢EMA

# ═══════════════════════════════════════════════════════════════════
# M5 入场参数
# ═══════════════════════════════════════════════════════════════════
M5_RSI_PERIOD = 7             # M5 RSI周期
M5_RSI_EXTREME = 80           # M5 RSI极端值
M5_MOMENTUM_PERIOD = 5        # M5动量周期

# ═══════════════════════════════════════════════════════════════════
# 时段参数
# ═══════════════════════════════════════════════════════════════════
SESSION_MAIN_START = 13       # 主时段开始 UTC (伦纽重叠)
SESSION_MAIN_END = 17         # 主时段结束 UTC
SESSION_SEC_START = 8         # 辅时段开始 UTC (伦敦)
SESSION_SEC_END = 12          # 辅时段结束 UTC
SESSION_AVOID_START = 22      # 避免时段开始 UTC
SESSION_AVOID_END = 0         # 避免时段结束 UTC

# ═══════════════════════════════════════════════════════════════════
# 出场参数
# ═══════════════════════════════════════════════════════════════════
MAX_HOLD_SEC = 1800           # 最大持仓30分钟
TRAILING_START_PCT = 0.5      # 盈利达50%TP开始跟踪
TRAILING_STEP_PCT = 0.3       # 回撤30%TP平仓
PARTIAL_TP_PCT = 0.6          # 盈利达60%TP可部分平仓
PARTIAL_CLOSE_RATIO = 0.5     # 部分平仓比例 (暂不实现, 0.01手无法拆)

# ═══════════════════════════════════════════════════════════════════
# 数据目录
# ═══════════════════════════════════════════════════════════════════
def _xau_data_dir():
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "data"
    try:
        p = Path(__file__).resolve().parent.parent.parent / "data"
        if p.exists(): return p
    except Exception: pass
    return Path.cwd() / "data"

XAU_DATA_DIR = _xau_data_dir()
DAILY_STATE_FILE = XAU_DATA_DIR / "xau_daily_state.json"


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════
def _body(c): return abs(c["c"] - c["o"])
def _is_bull(c): return c["c"] >= c["o"]
def _is_bear(c): return c["c"] < c["o"]
def _wick_up(c): return c["h"] - max(c["o"], c["c"])
def _wick_dn(c): return min(c["o"], c["c"]) - c["l"]
def _range(c): return c["h"] - c["l"]
def _mid(c): return (c["h"] + c["l"]) / 2


def _is_engulfing(prev, curr):
    """吞没形态"""
    if _is_bull(curr) and _is_bear(prev):
        return curr["c"] > prev["o"] and curr["o"] < prev["c"]
    if _is_bear(curr) and _is_bull(prev):
        return curr["c"] < prev["o"] and curr["o"] > prev["c"]
    return False


def _is_pin_bar(c, ratio=0.6):
    """Pin Bar: 长影线占比>ratio"""
    r = _range(c)
    if r <= 0: return False
    return _wick_dn(c) / r > ratio or _wick_up(c) / r > ratio


def _is_inside_bar(prev, curr):
    """内包形态"""
    return curr["h"] <= prev["h"] and curr["l"] >= prev["l"]


def _is_doji(c, ratio=0.1):
    """Doji: 实体占比<ratio"""
    r = _range(c)
    if r <= 0: return False
    return _body(c) / r < ratio


# ═══════════════════════════════════════════════════════════════════
# ADX 计算 (Wilder平滑)
# ═══════════════════════════════════════════════════════════════════
def calc_adx(highs, lows, closes, period=14):
    """计算ADX, 返回 (adx, plus_di, minus_di) 数组"""
    n = len(closes)
    if n < period + 1:
        return np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)

    tr = np.full(n, np.nan)
    plus_dm = np.full(n, np.nan)
    minus_dm = np.full(n, np.nan)

    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        tr[i] = max(hl, hc, lc)

        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm[i] = up if up > dn and up > 0 else 0
        minus_dm[i] = dn if dn > up and dn > 0 else 0

    # Wilder平滑
    atr_s = np.full(n, np.nan)
    apdm = np.full(n, np.nan)
    amdm = np.full(n, np.nan)

    atr_s[period] = np.sum(tr[1:period+1])
    apdm[period] = np.sum(plus_dm[1:period+1])
    amdm[period] = np.sum(minus_dm[1:period+1])

    for i in range(period + 1, n):
        atr_s[i] = atr_s[i-1] - atr_s[i-1] / period + tr[i]
        apdm[i] = apdm[i-1] - apdm[i-1] / period + plus_dm[i]
        amdm[i] = amdm[i-1] - amdm[i-1] / period + minus_dm[i]

    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    dx = np.full(n, np.nan)

    for i in range(period, n):
        if atr_s[i] > 0:
            plus_di[i] = 100 * apdm[i] / atr_s[i]
            minus_di[i] = 100 * amdm[i] / atr_s[i]
        s = plus_di[i] + minus_di[i]
        if s > 0:
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / s

    adx = np.full(n, np.nan)
    adx[2*period-1] = np.mean(dx[period:2*period])
    for i in range(2*period, n):
        if not np.isnan(dx[i]):
            adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period

    return adx, plus_di, minus_di


# ═══════════════════════════════════════════════════════════════════
# 关键位检测 (Swing High/Low, 带强度评分)
# ═══════════════════════════════════════════════════════════════════
def detect_key_levels(bars, lookback=100, strength=3):
    """检测Swing High/Low关键位, 带触及次数评分"""
    levels = []
    n = len(bars)
    start = max(strength, n - lookback)
    end = n - strength

    for i in range(start, end):
        # Swing High
        is_hi = all(bars[i]["h"] >= bars[i-j]["h"] for j in range(1, strength+1)) and \
                all(bars[i]["h"] >= bars[i+j]["h"] for j in range(1, strength+1))
        if is_hi:
            # 计算强度: 周围多少根K线的最高价都没超过
            s = 0
            for j in range(max(0, i-strength*2), min(n, i+strength*2+1)):
                if j != i and bars[j]["h"] < bars[i]["h"]:
                    s += 1
            levels.append({"type": "resist", "price": bars[i]["h"], "strength": s})

        # Swing Low
        is_lo = all(bars[i]["l"] <= bars[i-j]["l"] for j in range(1, strength+1)) and \
                all(bars[i]["l"] <= bars[i+j]["l"] for j in range(1, strength+1))
        if is_lo:
            s = 0
            for j in range(max(0, i-strength*2), min(n, i+strength*2+1)):
                if j != i and bars[j]["l"] > bars[i]["l"]:
                    s += 1
            levels.append({"type": "support", "price": bars[i]["l"], "strength": s})

    if not levels: return levels

    # 合并相近的关键位 (< 1.5点)
    merged = [levels[0]]
    for lv in levels[1:]:
        if abs(lv["price"] - merged[-1]["price"]) < 1.5:
            # 保留更强的
            if lv["strength"] > merged[-1]["strength"]:
                merged[-1] = lv
            elif lv["type"] != merged[-1]["type"]:
                merged[-1]["type"] = "both"
        else:
            merged.append(lv)

    # 按强度排序, 保留最强的10个
    merged.sort(key=lambda x: x["strength"], reverse=True)
    return merged[:10]


# ═══════════════════════════════════════════════════════════════════
# Order Block 检测
# ═══════════════════════════════════════════════════════════════════
def detect_order_blocks(bars, lookback=50):
    """检测Order Block: 急促行情前的最后一根反向K线

    看涨OB: 大阳线前最后一根阴线 (支撑区)
    看跌OB: 大阴线前最后一根阳线 (阻力区)
    """
    obs = []
    n = len(bars)
    if n < 5: return obs

    start = max(2, n - lookback)
    for i in range(start, n):
        curr_body = _body(bars[i])
        prev_body = _body(bars[i-1])
        curr_range = _range(bars[i])

        # 大实体K线 (> ATR * 1.5 或 占range > 70%)
        if curr_range <= 0 or curr_body / curr_range < 0.7:
            continue

        # 看涨OB: 当前大阳线, 前一根阴线
        if _is_bull(bars[i]) and _is_bear(bars[i-1]) and prev_body > 0:
            obs.append({
                "type": "bull_ob",
                "high": bars[i-1]["h"],
                "low": bars[i-1]["l"],
                "strength": curr_body / prev_body if prev_body > 0 else 1,
                "idx": i
            })

        # 看跌OB: 当前大阴线, 前一根阳线
        elif _is_bear(bars[i]) and _is_bull(bars[i-1]) and prev_body > 0:
            obs.append({
                "type": "bear_ob",
                "high": bars[i-1]["h"],
                "low": bars[i-1]["l"],
                "strength": curr_body / prev_body if prev_body > 0 else 1,
                "idx": i
            })

    return obs[-10:]  # 保留最近10个


# ═══════════════════════════════════════════════════════════════════
# FVG 检测 (Fair Value Gap)
# ═══════════════════════════════════════════════════════════════════
def detect_fvg(bars, lookback=50):
    """FVG: 三根K线之间留下的缺口"""
    fvgs = []
    n = len(bars)
    start = max(2, n - lookback)
    for i in range(start, n):
        # 看涨FVG: bars[i-2].high < bars[i].low
        if bars[i-2]["h"] < bars[i]["l"]:
            fvgs.append({
                "type": "bull",
                "high": bars[i]["l"],
                "low": bars[i-2]["h"],
                "mid": (bars[i]["l"] + bars[i-2]["h"]) / 2,
                "idx": i
            })
        # 看跌FVG: bars[i-2].low > bars[i].high
        elif bars[i-2]["l"] > bars[i]["h"]:
            fvgs.append({
                "type": "bear",
                "high": bars[i-2]["l"],
                "low": bars[i]["h"],
                "mid": (bars[i-2]["l"] + bars[i]["h"]) / 2,
                "idx": i
            })
    return fvgs[-10:]


# ═══════════════════════════════════════════════════════════════════
# 市场状态检测
# ═══════════════════════════════════════════════════════════════════
def detect_regime(h4_bars, h1_bars):
    """检测市场状态: TRENDING / RANGING / VOLATILE

    基于:
      - H4 ADX (趋势强度)
      - H4 EMA20/50排列
      - H1 波动率 (ATR相对大小)
    """
    n4 = len(h4_bars)
    n1 = len(h1_bars)
    if n4 < 50 or n1 < 30:
        return "RANGING", 0.0  # 数据不足默认震荡

    # H4 ADX
    h4_h = np.array([b["h"] for b in h4_bars])
    h4_l = np.array([b["l"] for b in h4_bars])
    h4_c = np.array([b["c"] for b in h4_bars])
    adx_arr, pdi, mdi = calc_adx(h4_h, h4_l, h4_c, ADX_LOOKBACK)
    adx_val = float(adx_arr[-1]) if not np.isnan(adx_arr[-1]) else 15.0

    # H4 EMA排列
    h4_e20 = EMA(h4_c, TREND_EMA_FAST)
    h4_e50 = EMA(h4_c, TREND_EMA_SLOW)
    ema_aligned = False
    ema_dir = "neutral"
    if not np.isnan(h4_e20[-1]) and not np.isnan(h4_e50[-1]):
        if h4_e20[-1] > h4_e50[-1]:
            ema_dir = "up"
            if h4_e20[-2] > h4_e50[-2]:  # 持续排列
                ema_aligned = True
        elif h4_e20[-1] < h4_e50[-1]:
            ema_dir = "down"
            if h4_e20[-2] < h4_e50[-2]:
                ema_aligned = True

    # H1 ATR (波动率)
    h1_h = np.array([b["h"] for b in h1_bars])
    h1_l = np.array([b["l"] for b in h1_bars])
    h1_c = np.array([b["c"] for b in h1_bars])
    h1_at = ATR(h1_h, h1_l, h1_c, 14)
    atr_val = float(h1_at[-1]) if not np.isnan(h1_at[-1]) else 10.0

    # 状态判断
    confidence = 0.0

    if adx_val > ADX_TREND_THRESHOLD and ema_aligned:
        regime = "TRENDING"
        confidence = min(1.0, (adx_val - ADX_TREND_THRESHOLD) / 20.0)
    elif adx_val < ADX_RANGE_THRESHOLD:
        regime = "RANGING"
        confidence = min(1.0, (ADX_RANGE_THRESHOLD - adx_val) / 15.0)
    elif atr_val > 25:  # 高波动
        regime = "VOLATILE"
        confidence = min(1.0, (atr_val - 25) / 15.0)
    else:
        regime = "RANGING"  # 中间地带偏震荡
        confidence = 0.3

    return regime, confidence


# ═══════════════════════════════════════════════════════════════════
# M5 入场评分
# ═══════════════════════════════════════════════════════════════════
def score_entry_m5(m5_bars, idx, signal_dir, atr_val, regime, h1_levels, h1_fvgs, h1_obs):
    """M5入场信号评分 (0-10分)

    signal_dir: "buy" / "sell"
    返回: (score, details)
    """
    if idx < 5: return 0, "数据不足"

    score = 0
    details = []
    c = m5_bars[idx]
    prev = m5_bars[idx - 1]
    cl = c["c"]

    # ── 1. K线形态 (+2) ──
    if _is_engulfing(prev, c):
        if (signal_dir == "buy" and _is_bull(c)) or (signal_dir == "sell" and _is_bear(c)):
            score += 2
            details.append("engulfing")

    if _is_pin_bar(c):
        # Pin bar方向一致
        if signal_dir == "buy" and _wick_dn(c) > _wick_up(c):
            score += 1
            details.append("pin_bull")
        elif signal_dir == "sell" and _wick_up(c) > _wick_dn(c):
            score += 1
            details.append("pin_bear")

    # ── 2. RSI (+2) ──
    m5_closes = np.array([b["c"] for b in m5_bars[:idx+1]])
    if len(m5_closes) > M5_RSI_PERIOD:
        rsi_arr = RSI(m5_closes, M5_RSI_PERIOD)
        rsi_val = float(rsi_arr[-1]) if not np.isnan(rsi_arr[-1]) else 50.0

        if signal_dir == "buy" and rsi_val < RANGE_RSI_OVERSOLD:
            score += 2; details.append(f"rsi_oversold({rsi_val:.0f})")
        elif signal_dir == "sell" and rsi_val > RANGE_RSI_OVERBOUGHT:
            score += 2; details.append(f"rsi_overbought({rsi_val:.0f})")
        elif signal_dir == "buy" and rsi_val < 45:
            score += 1; details.append(f"rsi_low({rsi_val:.0f})")
        elif signal_dir == "sell" and rsi_val > 55:
            score += 1; details.append(f"rsi_high({rsi_val:.0f})")

    # ── 3. H1关键位接近 (+2) ──
    for lv in h1_levels:
        dist = abs(cl - lv["price"])
        if dist < atr_val * RANGE_KEY_LEVEL_DIST:
            if signal_dir == "buy" and lv["type"] in ("support", "both"):
                score += 2; details.append(f"key_support({lv['price']:.1f})"); break
            elif signal_dir == "sell" and lv["type"] in ("resist", "both"):
                score += 2; details.append(f"key_resist({lv['price']:.1f})"); break

    # ── 4. FVG/Order Block确认 (+2) ──
    # 买入: 价格回踩到看涨FVG/OB
    if signal_dir == "buy":
        for fvg in h1_fvgs:
            if fvg["type"] == "bull" and fvg["low"] <= cl <= fvg["high"]:
                score += 1; details.append("fvg_bull"); break
        for ob in h1_obs:
            if ob["type"] == "bull_ob" and ob["low"] <= cl <= ob["high"]:
                score += 1; details.append("ob_bull"); break
    elif signal_dir == "sell":
        for fvg in h1_fvgs:
            if fvg["type"] == "bear" and fvg["low"] <= cl <= fvg["high"]:
                score += 1; details.append("fvg_bear"); break
        for ob in h1_obs:
            if ob["type"] == "bear_ob" and ob["low"] <= cl <= ob["high"]:
                score += 1; details.append("ob_bear"); break

    # ── 5. 动量确认 (+1) ──
    if idx >= M5_MOMENTUM_PERIOD:
        momentum = cl - m5_bars[idx - M5_MOMENTUM_PERIOD]["c"]
        if signal_dir == "buy" and momentum > 0:
            score += 1; details.append("mom_up")
        elif signal_dir == "sell" and momentum < 0:
            score += 1; details.append("mom_dn")

    # ── 6. 量确认 (+1) ──
    avg_vol = np.mean([b["v"] for b in m5_bars[max(0, idx-20):idx+1]])
    if c["v"] > avg_vol * 1.2:
        score += 1; details.append("vol_high")

    return score, ",".join(details) if details else "weak"


# ═══════════════════════════════════════════════════════════════════
# 时段判断
# ═══════════════════════════════════════════════════════════════════
def get_session_quality(utc_hour):
    """时段质量评分 (0-1)

    伦纽重叠(13-17): 1.0
    伦敦(8-12): 0.7
    纽约午后(17-21): 0.5
    亚洲活跃(0-2): 0.3
    其他: 0.1
    """
    if 13 <= utc_hour < 17: return 1.0   # 伦纽重叠
    if 8 <= utc_hour < 12: return 0.7    # 伦敦
    if 17 <= utc_hour < 21: return 0.5   # 纽约午后
    if 0 <= utc_hour < 2: return 0.3     # 亚洲初段
    if 22 <= utc_hour: return 0.1        # 深夜
    return 0.2


# ═══════════════════════════════════════════════════════════════════
# M5 K线聚合器
# ═══════════════════════════════════════════════════════════════════
class M5Agg:
    def __init__(self, cb):
        self._cb = cb; self._c = None

    def add_tick(self, t):
        ts = t.timestamp or datetime.now(timezone.utc)
        m5 = self._round_m5(ts)
        if self._c is None or m5 > self._c["t"]:
            old = self._c
            self._c = {"t": m5, "o": t.bid, "h": t.bid, "l": t.bid, "c": t.bid, "v": 1}
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
    def _round_m5(d):
        ts = int(d.timestamp()); ts = ts - (ts % 300)
        return datetime.fromtimestamp(ts, tz=timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# 策略主体
# ═══════════════════════════════════════════════════════════════════
class StrategyEngineXAU(BaseStrategy):
    """自适应双模 — XAUUSD终极策略: 自适应双模 (均值回归+趋势延续)"""

    def __init__(self, executor, config=None, recorder=None, emailer=None):
        self._exe = executor
        self._cfg = config or StrategyConfig()
        self._cfg.volume = MAX_VOLUME
        self._rec = recorder or Recorder()
        self._emailer = emailer
        self._running = False
        self._agg = None

        # M5 buffer (从tick聚合)
        self._m5_buf = []
        self._m5_max_buf = 500  # 保留500根M5

        # H1/H4 buffer (从MT5预加载)
        self._h1_buf = []
        self._h4_buf = []

        # 市场状态缓存
        self._regime = "RANGING"
        self._regime_conf = 0.0
        self._h1_atr = 10.0
        self._h1_levels = []
        self._h1_fvgs = []
        self._h1_obs = []

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
        self._pos_sl = 0.0
        self._pos_tp = 0.0
        self._pos_open_ts = 0.0
        self._pos_last_price = 0.0
        self._pos_regime = None  # 开仓时的regime
        self._pos_score = 0
        self._active_trade_id = None
        self._last_tick = None

        # 刷新时间
        self._ct = None
        self._last_h1_refresh = 0.0
        self._last_h4_refresh = 0.0
        self._last_regime_check = 0.0

    # ── start/stop ──
    async def start(self):
        self._running = True
        self._agg = M5Agg(self._on_m5)
        self._load_daily_state()
        await self._preload()
        self._ct = asyncio.create_task(self._cleanup_loop())
        logger.info("XAU started: %s 双模策略 TP=%.1f SL=%.1f R:R=3:1 MIN_SCORE=%d HOLD=%ds",
                     self._cfg.symbol, TP_DIST, SL_DIST, MIN_SCORE, MAX_HOLD_SEC)

    async def stop(self):
        self._running = False
        if self._agg: self._agg.flush()
        if self._ct:
            self._ct.cancel()
            try: await asyncio.wait_for(self._ct, timeout=2)
            except Exception: pass
        self._save_daily_state()
        logger.info("XAU stopped: trades=%d pnl=%.2f regime=%s",
                     self._trade_count, self._daily_pnl, self._regime)

    async def on_tick(self, tick):
        if not self._running: return
        self._last_tick = tick
        self._agg.add_tick(tick)

        # 持仓中: tick级出场检查
        if self._pos_ticket:
            await self._check_pos_tick(tick)

        # 定期刷新H1/H4
        now = time.time()
        if now - self._last_h1_refresh > 300:   # 5分钟刷新H1
            await self._refresh_h1(); self._last_h1_refresh = now
        if now - self._last_h4_refresh > 900:   # 15分钟刷新H4
            await self._refresh_h4(); self._last_h4_refresh = now
        if now - self._last_regime_check > 300:  # 5分钟检测regime
            await self._check_regime(); self._last_regime_check = now

    # ── 日状态持久化 ──
    def _save_daily_state(self):
        try:
            XAU_DATA_DIR.mkdir(parents=True, exist_ok=True)
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

    # ── 预加载 ──
    async def _preload(self):
        await self._refresh_h1()
        await self._refresh_h4()
        await self._check_regime()
        self._last_h1_refresh = time.time()
        self._last_h4_refresh = time.time()
        self._last_regime_check = time.time()
        logger.info("XAU preload: H1=%d H4=%d regime=%s atr=%.2f",
                     len(self._h1_buf), len(self._h4_buf),
                     self._regime, self._h1_atr)

    # ── 刷新H1数据 ──
    async def _refresh_h1(self):
        try:
            r = await self._exe._run_in_executor(
                lambda: mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_H1, 0, 200))
            if r is not None and len(r) > 0:
                self._h1_buf = self._to_bars(r)
                # 更新结构
                self._h1_levels = detect_key_levels(self._h1_buf)
                self._h1_fvgs = detect_fvg(self._h1_buf)
                self._h1_obs = detect_order_blocks(self._h1_buf)
                # 更新ATR
                h1_h = np.array([b["h"] for b in self._h1_buf])
                h1_l = np.array([b["l"] for b in self._h1_buf])
                h1_c = np.array([b["c"] for b in self._h1_buf])
                at = ATR(h1_h, h1_l, h1_c, 14)
                if not np.isnan(at[-1]):
                    self._h1_atr = float(at[-1])
        except Exception as e:
            logger.warning("XAU refresh H1 fail: %s", e)

    # ── 刷新H4数据 ──
    async def _refresh_h4(self):
        try:
            r = await self._exe._run_in_executor(
                lambda: mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_H4, 0, 200))
            if r is not None and len(r) > 0:
                self._h4_buf = self._to_bars(r)
        except Exception as e:
            logger.warning("XAU refresh H4 fail: %s", e)

    # ── 检测市场状态 ──
    async def _check_regime(self):
        if len(self._h4_buf) < 50 or len(self._h1_buf) < 30:
            return
        regime, conf = detect_regime(self._h4_buf, self._h1_buf)
        old = self._regime
        self._regime = regime
        self._regime_conf = conf
        if regime != old:
            logger.info("XAU regime: %s → %s (conf=%.2f atr=%.2f)", old, regime, conf, self._h1_atr)

    # ── M5 K线回调 ──
    async def _on_m5(self, candle):
        if not self._running: return
        self._m5_buf.append(candle)
        if len(self._m5_buf) > self._m5_max_buf:
            self._m5_buf = self._m5_buf[-self._m5_max_buf:]

        await self._check_entry(candle)

    # ── 入场检查 ──
    async def _check_entry(self, candle):
        """入场核心逻辑"""
        # 已持仓不重复开
        if self._pos_ticket: return

        # 风控检查
        now = time.time()
        today = int(datetime.now(timezone.utc).timestamp() / 86400)
        if today != self._last_day:
            self._last_day = today
            self._trade_count = 0; self._daily_pnl = 0.0
            self._daily_loss = 0.0; self._consec_loss = 0

        if self._daily_loss >= MAX_DAILY_LOSS: return
        if self._daily_pnl >= DAILY_TARGET: return
        if self._trade_count >= MAX_DAILY_TRADES: return
        if self._consec_loss >= CONSEC_LOSS_LIMIT: return

        # ATR检查
        atr = self._h1_atr
        if atr < RANGE_MIN_ATR or atr > RANGE_MAX_ATR: return

        # 时段检查
        utc_hour = datetime.now(timezone.utc).hour
        session_q = get_session_quality(utc_hour)
        if session_q < 0.2: return  # 深夜不交易

        # 需要足够的M5数据
        if len(self._m5_buf) < 30: return

        idx = len(self._m5_buf) - 1

        # ── RANGE模式: 均值回归 ──
        if self._regime in ("RANGING", "VOLATILE"):
            await self._check_range_entry(idx, atr, session_q)

        # ── TREND模式: 趋势延续 ──
        if self._regime == "TRENDING" and self._pos_ticket is None:
            await self._check_trend_entry(idx, atr, session_q)

    async def _check_range_entry(self, idx, atr, session_q):
        """均值回归入场: 在关键位/极端RSI逆势入场"""
        cl = self._m5_buf[idx]["c"]

        # 检查是否在关键位附近
        near_support = False
        near_resist = False
        for lv in self._h1_levels:
            dist = abs(cl - lv["price"])
            if dist < atr * RANGE_KEY_LEVEL_DIST:
                if lv["type"] in ("support", "both") and cl < lv["price"]:
                    near_support = True
                elif lv["type"] in ("resist", "both") and cl > lv["price"]:
                    near_resist = True

        # 检查FVG/OB接近
        in_bull_fvg = any(f["type"] == "bull" and f["low"] <= cl <= f["high"] for f in self._h1_fvgs)
        in_bear_fvg = any(f["type"] == "bear" and f["low"] <= cl <= f["high"] for f in self._h1_fvgs)
        in_bull_ob = any(o["type"] == "bull_ob" and o["low"] <= cl <= o["high"] for o in self._h1_obs)
        in_bear_ob = any(o["type"] == "bear_ob" and o["low"] <= cl <= o["high"] for o in self._h1_obs)

        signal = None
        if near_support or in_bull_fvg or in_bull_ob:
            signal = "buy"
        elif near_resist or in_bear_fvg or in_bear_ob:
            signal = "sell"

        if not signal: return

        # M5评分
        score, details = score_entry_m5(
            self._m5_buf, idx, signal, atr, "RANGING",
            self._h1_levels, self._h1_fvgs, self._h1_obs
        )

        # 深夜需要更高评分
        min_score = MIN_SCORE + (1 if session_q < 0.5 else 0)
        if score < min_score: return

        # 固定SL/TP
        tick = self._last_tick
        if tick is None:
            try:
                tick = await self._exe._run_in_executor(
                    lambda: mt5.symbol_info_tick(self._cfg.symbol))
            except Exception: return
        if tick is None: return

        if signal == "buy":
            fill_price = tick.ask
            sl = round(fill_price - SL_DIST, 2)
            tp = round(fill_price + TP_DIST, 2)
        else:
            fill_price = tick.bid
            sl = round(fill_price + SL_DIST, 2)
            tp = round(fill_price - TP_DIST, 2)

        await self._open_order(signal, fill_price, sl, tp, score, details, "RANGE")

    async def _check_trend_entry(self, idx, atr, session_q):
        """趋势延续入场: 回踩FVG/OB顺势入场"""
        cl = self._m5_buf[idx]["c"]

        # 判断趋势方向 (H4 EMA)
        if len(self._h4_buf) < 50: return
        h4_c = np.array([b["c"] for b in self._h4_buf])
        h4_e20 = EMA(h4_c, TREND_EMA_FAST)
        h4_e50 = EMA(h4_c, TREND_EMA_SLOW)
        if np.isnan(h4_e20[-1]) or np.isnan(h4_e50[-1]): return

        if h4_e20[-1] > h4_e50[-1]:
            trend_dir = "up"
        elif h4_e20[-1] < h4_e50[-1]:
            trend_dir = "down"
        else:
            return  # 无趋势

        # 回踩检测: 价格回踩到看涨FVG/OB (上升趋势) 或看跌FVG/OB (下降趋势)
        pullback_ok = False
        signal = None

        if trend_dir == "up":
            # 价格回踩到看涨FVG或OB
            for fvg in self._h1_fvgs:
                if fvg["type"] == "bull" and fvg["low"] <= cl <= fvg["high"]:
                    pullback_ok = True; break
            if not pullback_ok:
                for ob in self._h1_obs:
                    if ob["type"] == "bull_ob" and ob["low"] <= cl <= ob["high"]:
                        pullback_ok = True; break
            if pullback_ok:
                signal = "buy"
        elif trend_dir == "down":
            for fvg in self._h1_fvgs:
                if fvg["type"] == "bear" and fvg["low"] <= cl <= fvg["high"]:
                    pullback_ok = True; break
            if not pullback_ok:
                for ob in self._h1_obs:
                    if ob["type"] == "bear_ob" and ob["low"] <= cl <= ob["high"]:
                        pullback_ok = True; break
            if pullback_ok:
                signal = "sell"

        if not signal: return

        # M5评分
        score, details = score_entry_m5(
            self._m5_buf, idx, signal, atr, "TRENDING",
            self._h1_levels, self._h1_fvgs, self._h1_obs
        )

        # 趋势模式评分要求
        min_score = MIN_SCORE + (1 if session_q < 0.5 else 0)
        if score < min_score: return

        # 固定SL/TP
        tick = self._last_tick
        if tick is None:
            try:
                tick = await self._exe._run_in_executor(
                    lambda: mt5.symbol_info_tick(self._cfg.symbol))
            except Exception: return
        if tick is None: return

        if signal == "buy":
            fill_price = tick.ask
            sl = round(fill_price - SL_DIST, 2)
            tp = round(fill_price + TP_DIST, 2)
        else:
            fill_price = tick.bid
            sl = round(fill_price + SL_DIST, 2)
            tp = round(fill_price - TP_DIST, 2)

        await self._open_order(signal, fill_price, sl, tp, score, details, "TREND")

    # ── 开仓 ──
    async def _open_order(self, direction, price, sl, tp, score, details, mode):
        try:
            if direction == "buy":
                res = await self._exe.buy_market(self._cfg.symbol, MAX_VOLUME, sl, tp)
            else:
                res = await self._exe.sell_market(self._cfg.symbol, MAX_VOLUME, sl, tp)

            if res and res.get("deal", 0) > 0:
                self._pos_ticket = res["deal"]
                self._pos_direction = direction
                self._pos_entry = price
                self._pos_sl = sl
                self._pos_tp = tp
                self._pos_open_ts = time.time()
                self._pos_last_price = price
                self._pos_regime = mode
                self._pos_score = score
                self._trade_count += 1

                logger.info("XAU OPEN %s @%.2f SL=%.2f TP=%.2f score=%d mode=%s [%s]",
                             direction, price, sl, tp, score, mode, details)

                # 邮件通知
                if self._emailer:
                    try:
                        await self._emailer.send_alert(
                            f"XAU {direction.upper()}",
                            f"{direction} {self._cfg.symbol} @ {price:.2f}\n"
                            f"SL={sl:.2f} TP={tp:.2f} Score={score} Mode={mode}\n"
                            f"Details: {details}"
                        )
                    except Exception: pass
        except Exception as e:
            logger.error("XAU open order fail: %s", e)

    # ── 持仓Tick检查 ──
    async def _check_pos_tick(self, tick):
        """tick级出场: 超时强平 + 跟踪止盈"""
        if not self._pos_ticket: return

        now = time.time()
        hold_sec = now - self._pos_open_ts
        bid = tick.bid; ask = tick.ask

        # 当前盈亏
        if self._pos_direction == "buy":
            profit = bid - self._pos_entry
        else:
            profit = self._pos_entry - ask

        # 超时强平
        if hold_sec > MAX_HOLD_SEC:
            await self._close_pos("TIMEOUT")
            return

        # 跟踪止盈
        tp_dist = abs(self._pos_tp - self._pos_entry)
        if tp_dist > 0 and profit > 0:
            profit_pct = profit / tp_dist
            if profit_pct >= TRAILING_START_PCT:
                # 更新跟踪止损
                if self._pos_direction == "buy":
                    trail_sl = self._pos_entry + tp_dist * (profit_pct - TRAILING_STEP_PCT)
                    if bid < trail_sl:
                        await self._close_pos("TRAIL_TP")
                        return
                else:
                    trail_sl = self._pos_entry - tp_dist * (profit_pct - TRAILING_STEP_PCT)
                    if ask > trail_sl:
                        await self._close_pos("TRAIL_TP")
                        return

    # ── 平仓 ──
    async def _close_pos(self, reason):
        if not self._pos_ticket: return
        try:
            positions = await self._exe._run_in_executor(
                lambda: mt5.positions_get(symbol=self._cfg.symbol))
            if positions is None or len(positions) == 0:
                self._reset_pos(reason, 0.0)
                return

            pos = positions[0]
            if self._pos_direction == "buy":
                res = await self._exe.sell_market(self._cfg.symbol, MAX_VOLUME)
            else:
                res = await self._exe.buy_market(self._cfg.symbol, MAX_VOLUME)

            pnl = pos.profit if pos.profit else 0.0
            self._reset_pos(reason, pnl)
        except Exception as e:
            logger.error("XAU close fail: %s", e)
            self._reset_pos(reason, 0.0)

    def _reset_pos(self, reason, pnl):
        if pnl != 0.0:
            self._daily_pnl += pnl
            if pnl < 0:
                self._daily_loss += abs(pnl)
                self._consec_loss += 1
            else:
                self._consec_loss = 0

        logger.info("XAU CLOSE %s reason=%s pnl=%.2f daily=%.2f/%.2f trades=%d",
                     self._pos_direction or "?", reason, pnl,
                     self._daily_pnl, self._daily_loss, self._trade_count)
        self._pos_ticket = None
        self._pos_direction = None
        self._pos_entry = 0.0
        self._pos_sl = 0.0
        self._pos_tp = 0.0
        self._pos_open_ts = 0.0
        self._pos_last_price = 0.0
        self._pos_regime = None
        self._pos_score = 0

    # ── 清理循环 ──
    async def _cleanup_loop(self):
        while self._running:
            await asyncio.sleep(60)

    # ── 工具 ──
    @staticmethod
    def _to_bars(r):
        return [{"t": datetime.fromtimestamp(row[0], tz=timezone.utc),
                 "ts": float(row[0]),
                 "o": float(row[1]), "h": float(row[2]),
                 "l": float(row[3]), "c": float(row[4]),
                 "v": int(row[5])} for row in r]

    def get_status(self) -> dict:
        return {
            "active": self._running,
            "strategy": "ADM",
            "version": "2.1.0",
            "tp_dist": TP_DIST,
            "sl_dist": SL_DIST,
            "min_score": MIN_SCORE,
            "regime": self._regime,
            "regime_conf": round(self._regime_conf, 2),
            "h1_atr": round(self._h1_atr, 2),
            "h1_levels": len(self._h1_levels),
            "h1_fvgs": len(self._h1_fvgs),
            "h1_obs": len(self._h1_obs),
            "m5_bars": len(self._m5_buf),
            "pos": self._pos_direction,
            "pos_entry": self._pos_entry,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_loss": round(self._daily_loss, 2),
            "trades": self._trade_count,
        }
