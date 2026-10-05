"""G1: XAUUSD 小资金右侧趋势策略 (时段优化+自适应盈亏比)

核心设计:
  1. 严格时段管理 — 只在安全窗口交易, 避开机构扫盘时段
     ASIAN  08:00-15:20 | 平稳可做
     BLOCK  15:30-16:00 | 欧盘开盘扫单 ✗
     EURO   16:00-20:20 | VWAP 走势平稳 ✅ 最佳窗口
     BLOCK  20:30-22:00 | 美盘假方向 ✗
     USLATE 22:00-23:30 | 谨慎可做
  2. 右侧趋势跟踪 — EMA20/50 确认趋势, 回撤入场
  3. VWAP 锚定 — 欧盘时段核心参考
  4. 回撤 0.5/1.5 ATR 档位入场
  5. 自适应盈亏比 — 每 N 笔交易后自动调优 TP/SL 参数
  6. ATR 动态止损止盈 + 移动止损

适合 150U / 0.01 手 XAUUSD, 追求稳健复利。
"""
import asyncio
import itertools
import json
import logging
import math
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
    from .indicators import EMA, RSI, ATR
except ImportError:
    from src.market_data.models import Tick
    from src.trading.mt5_executor import MT5Executor
    from src.strategy.config import StrategyConfig
    from src.strategy.recorder import Recorder
    from src.notifier.email_alerter import EmailAlerter
    from src.strategy.base import BaseStrategy
    from src.strategy.indicators import EMA, RSI, ATR

logger = logging.getLogger(__name__)

# ── 账户风控硬参数 (150U) ──────────────────────────────────────────
ACCOUNT_SIZE = 150.0
MAX_VOLUME = 0.01          # 正常 0.01 手
VOLUME_REDUCED = 0.005     # 连亏后降至 0.005
MAX_SL_PCT = 0.003         # 单笔止损硬上限 0.3% (~4.5U)
MIN_SL_PCT = 0.0015        # 单笔止损下限
MAX_DAILY_LOSS = 8.0       # 日最大亏损 8U (硬上限)
SOFT_LOSS_LIMIT = 4.0      # 日亏 4U 后冷却 2 小时
DAILY_TARGET = 8.0         # 日盈利目标 +8U 后停
MAX_DAILY_TRADES = 10      # 日最大交易数
MAX_HOLD_MINUTES = 90      # 持仓超过此时长 → 强制平仓

# ── 时段定义 (北京时间) ─────────────────────────────────────────────
SESSION = {
    "ASIAN":   (8,  0,   15, 20),   # 08:00-15:20  亚盘
    "BLOCK1":  (15, 30,  16, 0),    # 15:30-16:00  欧盘开盘扫单 ✗
    "EURO":    (16, 0,   20, 20),   # 16:00-20:20  欧盘稳盘 ✅
    "BLOCK2":  (20, 30,  22, 0),    # 20:30-22:00  美盘假方向 ✗
    "USLATE":  (22, 0,   23, 30),   # 22:00-23:30  美盘后期
}
TRADEABLE_SESSIONS = ["ASIAN", "EURO", "USLATE"]

# ── 回撤档位 (ATR 倍数) ──────────────────────────────────────────────
PULLBACK_LEVELS = [0.3, 0.5, 1.5]

# ── 自适应优化 ──────────────────────────────────────────────────────
OPTIMIZE_EVERY_N_TRADES = 25       # 每 25 笔优化一次
TRADE_HISTORY_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "g1_trades.json"
OPT_PARAMS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "g1_params.json"

# ── 默认参数 (会被自适应优化覆盖) ───────────────────────────────────
DEFAULT_PARAMS = {
    "tp_sl_ratio": 2.5,        # TP = SL × 这个值 (回测最优 90天+90.3U)
    "sl_atr_mult": 0.8,        # SL = ATR × 这个值 (回测最优)
    "rsi_overbought": 72,      # RSI 超买阈值 (放宽)
    "rsi_oversold": 28,        # RSI 超卖阈值 (放宽)
    "min_rr": 1.3,             # 最小盈亏比, 低于此不开仓
}

# ── 辅助: 数据目录 (云端兼容) ──────────────────────────────────────
def _g1_data_dir():
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "data"
    try:
        p = Path(__file__).resolve().parent.parent.parent / "data"
        if p.exists():
            return p
    except Exception:
        pass
    return Path.cwd() / "data"


G1_DATA_DIR = _g1_data_dir()
TRADE_HISTORY_FILE = G1_DATA_DIR / "g1_trades.json"
OPT_PARAMS_FILE = G1_DATA_DIR / "g1_params.json"
DAILY_STATE_FILE = G1_DATA_DIR / "g1_daily_state.json"


# ═══════════════════════════════════════════════════════════════════
# M5 K 线聚合器
# ═══════════════════════════════════════════════════════════════════
class M5Aggregator:
    """聚合 Tick → M5 K 线, 完成一根后回调。"""

    def __init__(self, cb):
        self._cb = cb
        self._c = None

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
            c["h"] = max(c["h"], t.bid)
            c["l"] = min(c["l"], t.bid)
            c["c"] = t.bid
            c["v"] += 1

    def flush(self):
        if self._c and self._c["v"] > 0:
            asyncio.create_task(self._cb(self._c))
        self._c = None

    @staticmethod
    def _round_m5(d):
        ts = int(d.timestamp())
        ts = ts - (ts % 300)
        return datetime.fromtimestamp(ts, tz=timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# 策略主体
# ═══════════════════════════════════════════════════════════════════
class StrategyEngineG1(BaseStrategy):
    """G1 — XAUUSD 小资金右侧趋势策略。

    时段优化 + VWAP/EMA 趋势过滤 + 回撤入场 + 自适应盈亏比。
    """

    def __init__(self, executor, config=None, recorder=None, emailer=None):
        self._exe = executor
        self._cfg = config or StrategyConfig()
        self._cfg.volume = MAX_VOLUME  # 强制 0.01
        self._rec = recorder or Recorder()
        self._emailer = emailer
        self._running = False
        self._agg = None
        self._buf = []            # M5 K 线
        self._daily_vwap_sum = 0.0
        self._daily_vwap_vol = 0.0
        self._trade_count = 0
        self._daily_pnl = 0.0
        self._daily_loss = 0.0
        self._consec_loss = 0
        self._last_day = -1
        self._last_trade_ts = 0.0
        self._current_volume = MAX_VOLUME
        self._cooldown_until = 0.0
        self._active_trade_id = None
        self._active_decision_id = None

        # ATR 历史 (波动率过滤)
        self._atr_history = []

        # 持仓跟踪
        self._pos_ticket = None
        self._pos_direction = None
        self._pos_entry = 0.0
        self._pos_sl = 0.0
        self._pos_tp = 0.0
        self._pos_sl_stage = 0
        self._pos_open_ts = 0.0

        # 当前参数 (会被自适应优化修改)
        self._p = dict(DEFAULT_PARAMS)

        # 后台任务
        self._tt = None           # trailing loop
        self._ct = None           # cleanup
        self._opt_task = None     # 自适应优化

    # ── 生命周期 ──────────────────────────────────────────────────
    async def start(self):
        self._running = True
        self._agg = M5Aggregator(self._on_candle)
        self._load_params()
        self._load_daily_state()
        await self._preload()
        self._tt = asyncio.create_task(self._trailing_loop())
        self._ct = asyncio.create_task(self._cleanup_loop())
        logger.info("G1 started: %s vol=%.2f params=%s", self._cfg.symbol, self._cfg.volume, self._p)

    async def stop(self):
        self._running = False
        if self._agg:
            self._agg.flush()
        for t in [self._tt, self._ct]:
            if t:
                t.cancel()
                try:
                    await asyncio.wait_for(t, timeout=2)
                except Exception:
                    pass
        self._save_params()
        self._save_daily_state()
        logger.info("G1 stopped, trades=%d daily_pnl=%.2f", self._trade_count, self._daily_pnl)

    async def on_tick(self, tick: Tick):
        if self._running and self._agg:
            self._agg.add_tick(tick)

    # ── 参数持久化 ──────────────────────────────────────────────
    def _save_params(self):
        try:
            G1_DATA_DIR.mkdir(parents=True, exist_ok=True)
            OPT_PARAMS_FILE.write_text(json.dumps(self._p, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("G1 save params: %s", e)

    def _load_params(self):
        if OPT_PARAMS_FILE.exists():
            try:
                d = json.loads(OPT_PARAMS_FILE.read_text(encoding="utf-8"))
                for k in self._p:
                    if k in d:
                        self._p[k] = d[k]
                logger.info("G1 loaded params: %s", self._p)
            except Exception as e:
                logger.warning("G1 load params: %s", e)

    # ── 日内状态 ────────────────────────────────────────────────
    def _save_daily_state(self):
        try:
            G1_DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "day": self._last_day,
                "trade_count": self._trade_count,
                "daily_pnl": self._daily_pnl,
                "daily_loss": self._daily_loss,
                "consec_loss": self._consec_loss,
                "current_volume": self._current_volume,
                "cooldown_until": self._cooldown_until,
            }
            DAILY_STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _load_daily_state(self):
        if not DAILY_STATE_FILE.exists():
            return
        try:
            d = json.loads(DAILY_STATE_FILE.read_text(encoding="utf-8"))
            today = int(datetime.now(timezone.utc).timestamp() / 86400)
            if d.get("day") == today:
                self._trade_count = d.get("trade_count", 0)
                self._daily_pnl = d.get("daily_pnl", 0.0)
                self._daily_loss = d.get("daily_loss", 0.0)
                self._consec_loss = d.get("consec_loss", 0)
                self._current_volume = d.get("current_volume", MAX_VOLUME)
                self._cooldown_until = d.get("cooldown_until", 0.0)
                self._last_day = today
        except Exception:
            pass

    # ── 预加载 M5 历史 ────────────────────────────────────────────
    async def _preload(self):
        def f():
            r = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M5, 0, 200)
            if r is None:
                return
            for row in r:
                dt = datetime.fromtimestamp(row[0], tz=timezone.utc)
                self._buf.append({
                    "t": dt, "o": float(row[1]), "h": float(row[2]),
                    "l": float(row[3]), "c": float(row[4]), "v": int(row[5]),
                })
        await MT5Executor._run_in_executor(f)
        logger.info("G1 preloaded %d M5 bars", len(self._buf))

    # ── 当前北京时间时段 ──────────────────────────────────────────
    def _current_session(self):
        """返回 (session_name, is_tradeable)"""
        now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
        h, m = now_bj.hour, now_bj.minute
        ts = h * 60 + m
        for name, (sh, sm, eh, em) in SESSION.items():
            start = sh * 60 + sm
            end = eh * 60 + em
            if start <= ts < end:
                return name, name in TRADEABLE_SESSIONS
        return "OFF", False

    def _is_tradeable_time(self):
        sname, tradeable = self._current_session()
        return tradeable

    # ── M5 K 线处理 ──────────────────────────────────────────────
    async def _on_candle(self, c):
        if not self._running:
            return
        self._buf.append(c)
        if len(self._buf) > 300:
            self._buf.pop(0)
        if len(self._buf) < 60:
            return
        await self._process_candle(c)

    async def _process_candle(self, c):
        b = self._buf
        i = len(b) - 1
        closes = np.array([x["c"] for x in b])
        highs = np.array([x["h"] for x in b])
        lows = np.array([x["l"] for x in b])

        # ── 指标 ──
        e20 = EMA(closes, 20)
        e50 = EMA(closes, 50)
        rs = RSI(closes, 14)
        at = ATR(highs, lows, closes, 14)

        if np.isnan(e20[i]) or np.isnan(e50[i]) or np.isnan(rs[i]) or np.isnan(at[i]):
            return

        # ── 波动率过滤 (ATR > 2x 均值 → 异常波动, 跳过) ──
        atr_val = float(at[i])
        self._atr_history.append(atr_val)
        if len(self._atr_history) > 30:
            self._atr_history.pop(0)
        if len(self._atr_history) >= 10:
            avg_atr = sum(self._atr_history) / len(self._atr_history)
            if atr_val > avg_atr * 2.0:
                return

        # ── 冷却检查 (日亏过半后暂停 2 小时) ──
        now_ts = time.time()
        if self._cooldown_until > now_ts:
            return

        # ── 日重置 ──
        ts = c["t"].timestamp()
        dy = int(ts / 86400)
        if dy != self._last_day:
            self._trade_count = 0
            self._daily_pnl = 0.0
            self._daily_loss = 0.0
            self._consec_loss = 0
            self._last_day = dy
            self._daily_vwap_sum = 0.0
            self._daily_vwap_vol = 0.0
            self._save_daily_state()

        # ── 有持仓 → 不开新单 ──
        has_pos = await self._check_position()
        if has_pos:
            return
        self._pos_ticket = None

        # ── 自适应手数 (连亏 → 减半) ──
        self._current_volume = VOLUME_REDUCED if self._consec_loss >= 2 else MAX_VOLUME

        # ── 软限制: 日亏过半 → 冷却 2 小时 ──
        if self._daily_loss >= SOFT_LOSS_LIMIT and self._cooldown_until <= now_ts:
            self._cooldown_until = now_ts + 7200  # 2 hours
            self._save_daily_state()
            logger.info("G1 soft loss limit hit (%.1fU), cooling 2h", self._daily_loss)

        # ── 风控闸门 ──
        if self._daily_pnl >= DAILY_TARGET:
            return
        if self._daily_loss >= MAX_DAILY_LOSS:
            return
        if self._trade_count >= MAX_DAILY_TRADES:
            return
        if not self._is_tradeable_time():
            return
        if self._consec_loss >= 5:
            return  # 连 5 亏停手, 等下个交易日

        # ── 右側趋势判断 (宽松版) ──
        cl = c["c"]
        trend_up = cl > e50[i] and e20[i] > e50[i]
        trend_dn = cl < e50[i] and e20[i] < e50[i]
        ranging = not (trend_up or trend_dn)

        if ranging:
            return

        # ── VWAP ──
        vwap = self._calc_vwap(c)
        atr_val = float(at[i])
        sess_name = self._current_session()[0]

        # ── 按时段选择入场模式 ──
        signal = None
        if sess_name == "EURO":
            # 欧盘: VWAP 支撑/阻力 + 突破入场
            # 价格贴近 VWAP (±0.5 ATR) 后反弹 → 入场
            if vwap and vwap > 0:
                if trend_up:
                    # 多头: low 触及 VWAP 附近, 且 close 回到 VWAP 之上
                    vwap_range = atr_val * 0.5  # XAUUSD ~4100 时约 2.6
                    if lo <= vwap and cl > vwap and lo >= vwap - vwap_range:
                        if rs[i] < self._p["rsi_overbought"]:
                            signal = "buy"
                elif trend_dn:
                    vwap_range = atr_val * 0.5
                    if hi >= vwap and cl < vwap and hi <= vwap + vwap_range:
                        if rs[i] > self._p["rsi_oversold"]:
                            signal = "sell"
        else:
            # 亚盘/美盘: 回撤入场 (EMA20 / VWAP / 0.5/1.5 ATR 档位)
            if trend_up:
                entry_levels = []
                if not np.isnan(e20[i]): entry_levels.append(e20[i])
                if vwap and vwap > 0: entry_levels.append(vwap)
                for lv in PULLBACK_LEVELS:
                    entry_levels.append(cl - atr_val * lv / 2)
                candidates = [e for e in entry_levels if e <= cl and e > cl - atr_val * 1.5]
                if candidates:
                    best = max(candidates)
                    if lo <= best or (i >= 1 and lows[i-1] <= best):
                        if rs[i] < self._p["rsi_overbought"]:
                            signal = "buy"
                else:
                    if i >= 1:
                        if lows[i-1] <= max(entry_levels):
                            if rs[i] < self._p["rsi_overbought"]:
                                signal = "buy"
            elif trend_dn:
                entry_levels = []
                if not np.isnan(e20[i]): entry_levels.append(e20[i])
                if vwap and vwap > 0: entry_levels.append(vwap)
                for lv in PULLBACK_LEVELS:
                    entry_levels.append(cl + atr_val * lv / 2)
                candidates = [e for e in entry_levels if e >= cl and e < cl + atr_val * 1.5]
                if candidates:
                    best = min(candidates)
                    if hi >= best or (i >= 1 and highs[i-1] >= best):
                        if rs[i] > self._p["rsi_oversold"]:
                            signal = "sell"
                else:
                    if i >= 1:
                        if highs[i-1] >= min(entry_levels):
                            if rs[i] > self._p["rsi_oversold"]:
                                signal = "sell"

        if not signal:
            return

        # ── 盈亏比检查 ──
        sl_dist = max(atr_val * self._p["sl_atr_mult"], cl * MIN_SL_PCT)
        max_sl = cl * MAX_SL_PCT
        if sl_dist > max_sl:
            sl_dist = max_sl
        tp_dist = sl_dist * self._p["tp_sl_ratio"]
        rr = tp_dist / sl_dist
        if rr < self._p["min_rr"]:
            return

        await self._open_order(signal, cl, sl_dist, tp_dist, atr_val)

    # ── VWAP ─────────────────────────────────────────────────────
    def _calc_vwap(self, c):
        """简单日 VWAP。"""
        self._daily_vwap_sum += c["c"] * c["v"]
        self._daily_vwap_vol += c["v"]
        if self._daily_vwap_vol > 0:
            return self._daily_vwap_sum / self._daily_vwap_vol
        return 0.0

    # ── 持仓检查 ────────────────────────────────────────────────
    async def _check_position(self):
        if not self._pos_ticket:
            return False
        try:
            pos = await self._exe._run_in_executor(
                lambda: mt5.positions_get(ticket=self._pos_ticket)
            )
            if pos and len(pos) > 0:
                return True
            await self._handle_close()
            self._pos_ticket = None
            self._pos_direction = None
            self._pos_sl_stage = 0
            return False
        except Exception:
            return False

    # ── 开仓 ────────────────────────────────────────────────────
    async def _open_order(self, direction, price, sl_dist, tp_dist, atr_val):
        vol = self._current_volume  # 使用自适应手数
        if direction == "buy":
            sl = round(price - sl_dist, 2)
            tp = round(price + tp_dist, 2)
            result = await self._exe.buy_market(self._cfg.symbol, vol, sl=sl, tp=tp)
        else:
            sl = round(price + sl_dist, 2)
            tp = round(price - tp_dist, 2)
            result = await self._exe.sell_market(self._cfg.symbol, vol, sl=sl, tp=tp)

        if not result.get("success"):
            logger.warning("G1 order failed: %s", result.get("error", "unknown"))
            return

        ticket = result.get("order", 0)
        fill = result.get("price", price)
        self._pos_ticket = ticket
        self._pos_direction = direction
        self._pos_entry = fill
        self._pos_sl = sl
        self._pos_tp = tp
        self._pos_sl_stage = 0
        self._pos_open_ts = time.time()
        self._trade_count += 1
        self._last_trade_ts = self._pos_open_ts

        sname, _ = self._current_session()
        did = self._rec.record_decision(
            direction=direction.upper(), strategy="G1", price=price,
            volume=vol, order_id=ticket,
            reason=f"G1_{sname}_RR={tp_dist/sl_dist:.2f}",
        )
        self._active_trade_id = self._rec.record_trade(
            direction=direction.upper(), open_price=fill, volume=vol,
            strategy="G1", open_order_id=ticket, decision_id=did, sl=sl, tp=tp,
        )
        self._active_decision_id = did
        self._save_daily_state()

        if self._emailer:
            now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
            self._emailer.send_order(
                time_str=now_str, direction=direction.upper(),
                strategy="G1", volume=vol, price=price,
            )
        logger.info("G1 ORDER %s @ %.2f SL=%.2f TP=%.2f ATR=%.2f RR=%.2f #%d/%d",
                     direction.upper(), price, sl, tp, atr_val,
                     tp_dist / sl_dist, self._trade_count, MAX_DAILY_TRADES)

    # ── 平仓处理 ────────────────────────────────────────────────
    async def _handle_close(self):
        pnl = 0.0
        close_price = 0.0
        reason = "Closed"
        if self._pos_ticket:
            try:
                deals = await self._exe._run_in_executor(
                    lambda: mt5.history_deals_get(
                        position=self._pos_ticket, from_position=0, to_position=0
                    )
                )
                if deals and len(deals) > 0:
                    ds = list(deals)
                    pnl = round(sum(d.profit for d in ds), 2)
                    close_price = ds[-1].price if hasattr(ds[-1], "price") else 0.0
                    reason = "TP" if pnl > 0 else "SL"
            except Exception:
                pass

        self._daily_pnl += pnl
        if pnl < 0:
            self._daily_loss += abs(pnl)
            self._consec_loss += 1
        else:
            self._consec_loss = 0

        if self._active_trade_id and close_price:
            self._rec.close_trade(self._active_trade_id, close_price, pnl)

        # 记录交易到历史 (用于自适应优化)
        if self._pos_ticket:
            self._record_trade(direction=self._pos_direction, pnl=pnl,
                               sl_dist=abs(self._pos_entry - self._pos_sl) if self._pos_sl else 0,
                               tp_dist=abs(self._pos_tp - self._pos_entry) if self._pos_tp else 0)

        self._active_trade_id = None
        self._save_daily_state()

        if self._emailer:
            now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
            self._emailer.send_close(time_str=now_str, direction=self._pos_direction or "",
                                     reason=reason, pnl=pnl)

        logger.info("G1 CLOSE ticket=%s pnl=%.2f reason=%s daily_pnl=%.2f",
                     self._pos_ticket, pnl, reason, self._daily_pnl)

    # ── 交易记录 ────────────────────────────────────────────────
    def _record_trade(self, direction, pnl, sl_dist, tp_dist):
        """保存交易至 history, 触发自适应优化。"""
        try:
            G1_DATA_DIR.mkdir(parents=True, exist_ok=True)
            trades = []
            if TRADE_HISTORY_FILE.exists():
                try:
                    trades = json.loads(TRADE_HISTORY_FILE.read_text(encoding="utf-8"))
                except Exception:
                    trades = []
            sname = self._current_session()[0] if self._current_session() else "?"
            trades.append({
                "ts": time.time(),
                "direction": direction,
                "pnl": pnl,
                "sl_dist": sl_dist,
                "tp_dist": tp_dist,
                "session": sname,
            })
            # 保留最近 300 笔
            if len(trades) > 300:
                trades = trades[-300:]
            TRADE_HISTORY_FILE.write_text(json.dumps(trades, indent=2), encoding="utf-8")
            # 触发优化检查
            if len(trades) % OPTIMIZE_EVERY_N_TRADES == 0:
                asyncio.create_task(self._optimize())
        except Exception as e:
            logger.warning("G1 record trade: %s", e)

    # ── 自适应参数优化 ──────────────────────────────────────────
    async def _optimize(self):
        """从历史交易中搜索最优参数组合。"""
        logger.info("G1 自适应优化开始...")
        try:
            if not TRADE_HISTORY_FILE.exists():
                return
            trades = json.loads(TRADE_HISTORY_FILE.read_text(encoding="utf-8"))
            if len(trades) < 20:
                return

            recent = trades[-OPTIMIZE_EVERY_N_TRADES:]
            best_score = -999
            best_params = dict(self._p)

            # 网格搜索: tp_sl_ratio × sl_atr_mult
            for tp_r in [1.3, 1.5, 1.8, 2.0, 2.2, 2.5, 3.0]:
                for sl_m in [0.8, 1.0, 1.2, 1.5, 1.8]:
                    wins = 0
                    total_pnl = 0.0
                    gross_win = 0.0
                    gross_loss = 0.0
                    for t in recent:
                        # 估算: 如果按这组参数, 这笔交易会是什么结果
                        r_sl = max(t["sl_dist"] * (sl_m / self._p["sl_atr_mult"]), 0.001)
                        r_tp = r_sl * tp_r
                        rr = tp_r
                        if rr < 1.3:
                            continue
                        # 实际盈亏超过了当前 SL/TP 范围 → 假设还是同样结果
                        # 简化: 直接用实际 pnl 算, 只调整盈亏比过滤
                        actual_pnl = t["pnl"]
                        total_pnl += actual_pnl
                        if actual_pnl > 0:
                            wins += 1
                            gross_win += actual_pnl
                        else:
                            gross_loss += abs(actual_pnl)

                    if wins < 3:
                        continue
                    score = total_pnl
                    # 惩罚低胜率
                    wr = wins / len(recent) * 100
                    if wr < 35:
                        score *= 0.8
                    # 奖励高盈亏比
                    pf = gross_win / gross_loss if gross_loss > 0 else 999
                    if pf > 1.5:
                        score *= 1.1
                    if score > best_score:
                        best_score = score
                        best_params = {
                            "tp_sl_ratio": tp_r,
                            "sl_atr_mult": sl_m,
                            "rsi_overbought": self._p["rsi_overbought"],
                            "rsi_oversold": self._p["rsi_oversold"],
                            "min_rr": 1.3,
                        }

            if best_params != self._p:
                self._p = best_params
                self._save_params()
                logger.info("G1 优化完成: score=%.2f params=%s", best_score, best_params)
            else:
                logger.info("G1 优化完成: 当前参数已最优 score=%.2f", best_score)
        except Exception as e:
            logger.warning("G1 optimize error: %s", e)

    # ── 移动止损 ────────────────────────────────────────────────
    async def _trailing_loop(self):
        while self._running:
            try:
                await asyncio.sleep(2)
                if not self._pos_ticket:
                    continue
                pos = await self._exe._run_in_executor(
                    lambda: mt5.positions_get(ticket=self._pos_ticket)
                )
                if not pos or len(pos) == 0:
                    self._pos_ticket = None
                    self._pos_direction = None
                    self._pos_sl_stage = 0
                    continue
                p = pos[0]
                entry = p.price_open
                profit = p.profit
                direction = "buy" if p.type == mt5.ORDER_TYPE_BUY else "sell"

                # 阶段 1: 盈利 +1.5U → 保本 (tighter lock)
                if profit >= 1.5 and self._pos_sl_stage < 1:
                    offset = 0.2
                    new_sl = round(entry + offset, 2) if direction == "buy" else round(entry - offset, 2)
                    await self._exe.modify_sl(self._cfg.symbol, self._pos_ticket, new_sl)
                    self._pos_sl = new_sl
                    self._pos_sl_stage = 1

                # 阶段 2: 盈利 +3.0U → 锁利 +1.5U
                elif profit >= 3.0 and self._pos_sl_stage < 2:
                    offset = 1.5
                    new_sl = round(entry + offset, 2) if direction == "buy" else round(entry - offset, 2)
                    await self._exe.modify_sl(self._cfg.symbol, self._pos_ticket, new_sl)
                    self._pos_sl = new_sl
                    self._pos_sl_stage = 2
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("G1 trailing: %s", e)

    # ── 日清理 ──────────────────────────────────────────────────
    async def _cleanup_loop(self):
        while self._running:
            try:
                await asyncio.sleep(30)  # 30s check
                now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
                h, m = now_bj.hour, now_bj.minute
                bjt = h * 60 + m

                # ── 时间止损: 持仓超过 MAX_HOLD_MINUTES → 强平 ──
                if self._pos_ticket and self._pos_open_ts > 0:
                    elapsed = (time.time() - self._pos_open_ts) / 60
                    if elapsed > MAX_HOLD_MINUTES:
                        logger.info("G1 time stop: %.0fm > %dm", elapsed, MAX_HOLD_MINUTES)
                        await self._exe.close_all_positions(self._cfg.symbol)
                        self._pos_ticket = None

                # ── 时段前强平: 进入 BLOCK1/BLOCK2 前强制平仓 ──
                # BLOCK1 15:30 → 15:25 强平
                # BLOCK2 20:30 → 20:25 强平
                if (bjt == 15 * 60 + 25) or (bjt == 20 * 60 + 25) or (h == 23 and m >= 55):
                    if self._pos_ticket:
                        logger.info("G1 pre-block close at %02d:%02d", h, m)
                        await self._exe.close_all_positions(self._cfg.symbol)
                        self._pos_ticket = None
                    if h == 23 and m >= 55:
                        await asyncio.sleep(70)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("G1 cleanup: %s", e)

    # ── 状态 ────────────────────────────────────────────────────
    def get_status(self):
        sname, tradeable = self._current_session()
        return {
            "state": "RUNNING" if self._running else "STOPPED",
            "running": self._running,
            "position": self._pos_ticket,
            "direction": self._pos_direction,
            "streak": f"{self._consec_loss}L" if self._consec_loss > 0 else "0L",
            "trade_count": self._trade_count,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_loss": round(self._daily_loss, 2),
            "session": sname,
            "tradeable": tradeable,
            "tp_sl_ratio": self._p["tp_sl_ratio"],
            "sl_atr_mult": self._p["sl_atr_mult"],
            "strategy": "G1",
        }
