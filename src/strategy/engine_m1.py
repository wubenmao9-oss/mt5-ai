"""M1: 亚欧盘小资金稳健策略 (布林带回归 + EMA50 趋势过滤)

面向 100U 本金、0.01 手的稳健日内策略:
  - 仅在北京时间 08:00-20:00 交易 (亚盘+欧盘, 避开美盘)
  - 布林带回归入场 + RSI 超买超卖 + EMA50 趋势过滤
  - ATR 动态止损 (1.2x), 止盈 (2.0x), 单笔最大亏损硬上限 0.25%
  - 日亏损上限 5U, 日盈利目标 +3U 后停止, 日最大 4 笔
  - 连续 2 亏后冷却 90 分钟, 盈利移动止损 (保本/锁利)
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np

# 双模式导入: 内置加载用相对导入, 云端加载用绝对导入 fallback
try:
    from ..market_data.models import Tick
    from ..trading.mt5_executor import MT5Executor
    from .config import StrategyConfig
    from .recorder import Recorder
    from ..notifier.email_alerter import EmailAlerter
    from .base import BaseStrategy
except ImportError:
    from src.market_data.models import Tick
    from src.trading.mt5_executor import MT5Executor
    from src.strategy.config import StrategyConfig
    from src.strategy.recorder import Recorder
    from src.notifier.email_alerter import EmailAlerter
    from src.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

SPREAD = 0.34  # USD, 价差成本估算


def _data_dir() -> Path:
    """定位 data 目录, 兼容内置 (__file__ 真实) 与云端 (__file__ 虚拟) 加载。"""
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


DAILY_STATE_FILE = _data_dir() / "m1_daily_state.json"

# 小资金硬性风控常量 (针对 100U / 0.01 手 XAUUSD)
DAILY_TARGET = 3.0          # 日盈利目标 +3U, 达成后停止进场
MAX_SL_PCT = 0.0025         # 单笔止损硬上限 0.25% (~6.6U @ 2650)
MIN_SL_PCT = 0.0015         # 单笔止损下限 0.15%
TP_SL_RATIO = 1.6           # 止盈/止损 盈亏比
BE_TRIGGER = 1.5            # 盈利 +1.5U 触发保本
PL_TRIGGER = 3.0            # 盈利 +3.0U 触发锁利 +1.5U
COOLDOWN_AFTER_LOSS_STREAK = 90 * 60  # 连续 2 亏后冷却 90 分钟


# ----------------------------------------------------------------------
# 指标
# ----------------------------------------------------------------------
def ema(a, p=9):
    n = len(a)
    r = np.full(n, np.nan)
    m = 2 / (p + 1)
    r[p - 1] = np.mean(a[:p])
    for i in range(p, n):
        r[i] = (a[i] - r[i - 1]) * m + r[i - 1]
    return r


def rsi(a, p=14):
    n = len(a)
    r = np.full(n, np.nan)
    for i in range(1, n):
        u = sum(a[j + 1] - a[j] for j in range(max(0, i - p), i) if a[j + 1] > a[j])
        d = sum(a[j] - a[j + 1] for j in range(max(0, i - p), i) if a[j + 1] <= a[j])
        r[i] = 50 if u + d == 0 else 100 - 100 / (1 + u / (d + 1e-10))
    return r


def atr(h, l, c, p=14):
    n = len(c)
    tr = np.zeros(n)
    ra = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    ra[p] = np.mean(tr[1:p + 1])
    for i in range(p + 1, n):
        ra[i] = (ra[i - 1] * (p - 1) + tr[i]) / p
    return ra


def bb(c, period=20, std_mult=2.0):
    n = len(c)
    mid = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period - 1, n):
        w = c[i - period + 1:i + 1]
        av = np.mean(w)
        sd = np.std(w, ddof=0)
        mid[i] = av
        upper[i] = av + std_mult * sd
        lower[i] = av - std_mult * sd
    return mid, upper, lower


# ----------------------------------------------------------------------
# Tick 聚合为 M15 K 线
# ----------------------------------------------------------------------
class TickAggregator:
    """聚合 Tick 为 M15 K 线, 完成一根后回调。"""

    def __init__(self, cb):
        self._cb = cb
        self._c = None

    def add_tick(self, t):
        ts = t.timestamp or datetime.now(timezone.utc)
        m15 = self._round_m15(ts)
        if self._c is None or m15 > self._c["t"]:
            old = self._c
            self._c = {"t": m15, "o": t.bid, "h": t.bid, "l": t.bid, "c": t.bid, "v": 1}
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
    def _round_m15(d):
        ts = int(d.timestamp())
        ts = ts - (ts % 900)
        return datetime.fromtimestamp(ts, tz=timezone.utc)


# ----------------------------------------------------------------------
# 策略主体
# ----------------------------------------------------------------------
class StrategyEngineM1(BaseStrategy):
    """M1 - 亚欧盘小资金稳健策略。

    适合 100U 本金 / 0.01 手。仅在亚盘+欧盘时段交易,
    布林带回归入场, EMA50 趋势过滤, 严格小资金风控。
    """

    def __init__(self, executor, config=None, recorder=None, emailer=None):
        self._exe = executor
        self._cfg = config or StrategyConfig()
        # 强制覆盖小资金专用风控参数 (无论 .env 怎么配, M1 始终用这套)
        # 亚欧盘时段 08:00-20:00 北京时间 (避开美盘), 日最大亏损 5U, 日最大 4 笔
        self._cfg.session_start = 8
        self._cfg.session_end = 20
        self._cfg.max_loss = 5.0
        self._cfg.max_trades = 4
        # 确保 0.01 手 (若 .env 误配更大手数, 取较小值保护小资金)
        if self._cfg.volume > 0.01:
            self._cfg.volume = 0.01
        self._rec = recorder or Recorder()
        self._emailer = emailer
        self._running = False
        self._agg = None
        self._buf = []           # M15 K 线缓冲
        self._tt = None          # 移动止损任务
        self._ct = None          # 日清理任务

        # 日内状态
        self._trade_count = 0
        self._daily_loss = 0.0
        self._daily_pnl = 0.0
        self._consec_loss = 0
        self._consec_bar = 0
        self._last_day = -1
        self._cooldown_until = 0.0  # UTC timestamp, 之前不进场

        # 持仓跟踪
        self._pos_ticket = None
        self._pos_direction = None
        self._pos_entry = 0.0
        self._pos_sl = 0.0
        self._pos_tp = 0.0
        self._pos_sl_stage = 0   # 0=初始, 1=保本, 2=锁利
        self._last_order_time = None
        self._last_warning_time = None
        self._silence_reason = "Init"
        self._active_trade_id = None
        self._active_decision_id = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self):
        self._running = True
        self._agg = TickAggregator(self._on_candle)
        self._load_daily_state()
        await self._preload()
        self._ct = asyncio.create_task(self._cleanup_routine())
        self._tt = asyncio.create_task(self._trailing_loop())
        logger.info(
            "M1 started: %s vol=%.2f session=%d-%d Beijing target=+%.1fU maxloss=%.1fU",
            self._cfg.symbol, self._cfg.volume,
            self._cfg.session_start, self._cfg.session_end,
            DAILY_TARGET, self._cfg.max_loss,
        )

    async def stop(self):
        self._running = False
        if self._agg:
            self._agg.flush()
        for t in [self._tt, self._ct]:
            if t:
                t.cancel()
                try:
                    await asyncio.wait_for(t, timeout=2)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        self._save_daily_state()
        logger.info("M1 stopped")

    async def on_tick(self, tick: Tick):
        if self._running and self._agg:
            self._agg.add_tick(tick)

    # ------------------------------------------------------------------
    # 日内状态持久化 (重启不丢失当日风控计数)
    # ------------------------------------------------------------------
    def _save_daily_state(self):
        try:
            DAILY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "day": self._last_day,
                "trade_count": self._trade_count,
                "daily_loss": self._daily_loss,
                "daily_pnl": self._daily_pnl,
                "consec_loss": self._consec_loss,
                "consec_bar": self._consec_bar,
                "cooldown_until": self._cooldown_until,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = DAILY_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(DAILY_STATE_FILE)
        except Exception as e:
            logger.warning("M1 save daily state failed: %s", e)

    def _load_daily_state(self):
        if not DAILY_STATE_FILE.exists():
            return
        try:
            data = json.loads(DAILY_STATE_FILE.read_text(encoding="utf-8"))
            today = int(datetime.now(timezone.utc).timestamp() / 86400)
            if data.get("day") == today:
                self._trade_count = data.get("trade_count", 0)
                self._daily_loss = data.get("daily_loss", 0.0)
                self._daily_pnl = data.get("daily_pnl", 0.0)
                self._consec_loss = data.get("consec_loss", 0)
                self._consec_bar = data.get("consec_bar", 0)
                self._cooldown_until = data.get("cooldown_until", 0.0)
                self._last_day = today
                logger.info(
                    "M1 restored: trades=%d pnl=%.2f loss=%.2f cooldown=%s",
                    self._trade_count, self._daily_pnl, self._daily_loss,
                    "yes" if self._cooldown_until else "no",
                )
            else:
                logger.info("M1 daily state from different day, starting fresh")
        except Exception as e:
            logger.warning("M1 load daily state failed: %s", e)

    # ------------------------------------------------------------------
    # 预加载历史 M15 K 线
    # ------------------------------------------------------------------
    async def _preload(self):
        def f():
            r15 = mt5.copy_rates_from_pos(
                self._cfg.symbol, mt5.TIMEFRAME_M15, 0, 200
            )
            if r15 is None:
                return
            for r in r15:
                dt = datetime.fromtimestamp(r[0], tz=timezone.utc)
                self._buf.append({
                    "t": dt, "o": float(r[1]), "h": float(r[2]),
                    "l": float(r[3]), "c": float(r[4]),
                })

        await MT5Executor._run_in_executor(f)
        logger.info("M1 preloaded %d M15 bars", len(self._buf))

    # ------------------------------------------------------------------
    # M15 K 线处理: 信号生成 + 下单
    # ------------------------------------------------------------------
    async def _on_candle(self, c):
        if not self._running:
            return
        self._buf.append(c)
        if len(self._buf) > 200:
            self._buf.pop(0)
        if len(self._buf) < 60:
            return  # 指标数据不足
        await self._process_candle(c)

    async def _process_candle(self, c):
        t = c["t"].timestamp()
        cl = c["c"]; hi = c["h"]; lo = c["l"]

        closes = np.array([x["c"] for x in self._buf])
        highs = np.array([x["h"] for x in self._buf])
        lows = np.array([x["l"] for x in self._buf])

        e9 = ema(closes, 9)
        e21 = ema(closes, 21)
        e50 = ema(closes, 50)
        rs = rsi(closes)
        at = atr(highs, lows, closes)
        bm, bu, bl = bb(closes)

        i = len(self._buf) - 1
        if np.isnan(e50[i]) or np.isnan(rs[i]) or np.isnan(at[i]) or np.isnan(bu[i]):
            return

        # 日重置
        dy = int(t / 86400)
        if dy != self._last_day:
            self._trade_count = 0
            self._daily_loss = 0.0
            self._daily_pnl = 0.0
            self._consec_loss = 0
            self._consec_bar = 0
            self._cooldown_until = 0.0
            self._last_day = dy
            self._save_daily_state()

        # 北京时间小时 (UTC+8)
        hh = int((datetime.fromtimestamp(t) + timedelta(hours=8)).hour % 24)
        in_session = self._cfg.session_start <= hh < self._cfg.session_end

        # 已有持仓 → 交给 trailing_loop + MT5 SL/TP
        has_position = await self._check_position()
        if has_position:
            return
        self._pos_ticket = None

        # ---- 风控闸门 ----
        if self._daily_pnl >= DAILY_TARGET:
            self._silence_reason = f"Daily target +{DAILY_TARGET}U reached"
            return
        if self._daily_loss >= self._cfg.max_loss:
            self._silence_reason = f"Max loss {self._cfg.max_loss}U reached"
            return
        if self._trade_count >= self._cfg.max_trades:
            self._silence_reason = f"Max trades {self._cfg.max_trades} reached"
            return
        if not in_session:
            self._silence_reason = f"Outside session ({hh} Beijing)"
            return
        now_ts = datetime.now(timezone.utc).timestamp()
        if now_ts < self._cooldown_until:
            remain = int((self._cooldown_until - now_ts) / 60)
            self._silence_reason = f"Cooldown {remain}m"
            return
        if self._consec_loss >= 2 and (i - self._consec_bar) < 6:
            # 连续亏损后, 至少等 6 根 M15 (90 分钟) 再考虑进场
            self._silence_reason = "Cooling after consecutive losses"
            return

        # ---- 趋势过滤 (EMA50 斜率) ----
        slope_up = e50[i] > e50[max(0, i - 3)]
        slope_dn = e50[i] < e50[max(0, i - 3)]
        # 价格在 EMA50 上方且斜率向上 = 多头; 反之空头
        trend_up = cl > e50[i] and slope_up
        trend_dn = cl < e50[i] and slope_dn
        # 价格贴近 EMA50 (震荡市, 适合回归)
        ranging = abs(cl - e50[i]) < at[i] * 0.5

        # ---- 入场信号: 布林带回归 ----
        # 做多: K 线低点触及下轨 + RSI 超卖 + 收盘回到下轨上方 + (顺势或震荡)
        touch_lower = lo <= bl[i]
        rsi_oversold = rs[i] < 35
        pullback_up = cl > bl[i]
        long_ok = (touch_lower and rsi_oversold and pullback_up
                   and (trend_up or ranging))

        # 做空: K 线高点触及上轨 + RSI 超买 + 收盘回到上轨下方 + (顺势或震荡)
        touch_upper = hi >= bu[i]
        rsi_overbought = rs[i] > 65
        pullback_dn = cl < bu[i]
        short_ok = (touch_upper and rsi_overbought and pullback_dn
                    and (trend_dn or ranging))

        # ---- 执行 ----
        if long_ok:
            await self._open_order("buy", cl, float(at[i]))
        elif short_ok:
            await self._open_order("sell", cl, float(at[i]))
        else:
            self._silence_reason = "No signal"

    # ------------------------------------------------------------------
    # 持仓检查
    # ------------------------------------------------------------------
    async def _check_position(self):
        """检查跟踪的 MT5 持仓是否仍存在, 已平仓则记录盈亏。"""
        if not self._pos_ticket:
            return False
        try:
            pos = await self._exe._run_in_executor(
                lambda: mt5.positions_get(ticket=self._pos_ticket)
            )
            if pos and len(pos) > 0:
                return True
            # 持仓已被 MT5 (SL/TP) 平掉
            await self._handle_close()
            self._pos_ticket = None
            self._pos_direction = None
            self._pos_sl_stage = 0
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 开仓 (真实 MT5 下单)
    # ------------------------------------------------------------------
    async def _open_order(self, direction, price, atr_val):
        vol = self._cfg.volume

        # ATR 动态止损止盈, 受小资金硬上限约束
        sl_dist = max(atr_val * 1.2, price * MIN_SL_PCT)
        max_sl = price * MAX_SL_PCT
        if sl_dist > max_sl:
            sl_dist = max_sl
        tp_dist = sl_dist * TP_SL_RATIO

        if direction == "buy":
            sl = round(price - sl_dist, 2)
            tp = round(price + tp_dist, 2)
            result = await self._exe.buy_market(self._cfg.symbol, vol, sl=sl, tp=tp)
        else:
            sl = round(price + sl_dist, 2)
            tp = round(price - tp_dist, 2)
            result = await self._exe.sell_market(self._cfg.symbol, vol, sl=sl, tp=tp)

        if not result.get("success"):
            logger.warning("M1 order failed: %s", result.get("error", "unknown"))
            self._silence_reason = f"Order failed: {result.get('error', '?')}"
            return

        ticket = result.get("order", 0)
        fill_price = result.get("price", price)
        self._pos_ticket = ticket
        self._pos_direction = direction
        self._pos_entry = fill_price
        self._pos_sl = sl
        self._pos_tp = tp
        self._pos_sl_stage = 0
        self._trade_count += 1
        self._last_order_time = datetime.now(timezone.utc).timestamp()
        self._silence_reason = ""

        # 记录决策与交易
        decision_id = self._rec.record_decision(
            direction=direction.upper(),
            strategy="M1",
            price=price,
            volume=vol,
            order_id=ticket,
            reason=f"BB+RSI+EMA50 SL={sl} TP={tp} ATR={atr_val:.2f}",
        )
        self._active_trade_id = self._rec.record_trade(
            direction=direction.upper(),
            open_price=fill_price,
            volume=vol,
            strategy="M1",
            open_order_id=ticket,
            decision_id=decision_id,
            sl=sl,
            tp=tp,
        )
        self._active_decision_id = decision_id
        self._save_daily_state()

        # 邮件通知
        if self._emailer:
            now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
            self._emailer.send_order(
                time_str=now_str,
                direction=direction.upper(),
                strategy="M1",
                volume=vol,
                price=price,
            )
        logger.info(
            "M1 ORDER %s @ %.2f SL=%.2f TP=%.2f ATR=%.2f ticket=%d #%d today",
            direction.upper(), price, sl, tp, atr_val, ticket, self._trade_count,
        )

    # ------------------------------------------------------------------
    # 平仓处理 (从 MT5 历史成交读取盈亏)
    # ------------------------------------------------------------------
    async def _handle_close(self):
        if not self._pos_ticket:
            return
        try:
            deals = await self._exe._run_in_executor(
                lambda: mt5.history_deals_get(
                    position=self._pos_ticket, from_position=0, to_position=0
                )
            )
        except Exception:
            deals = None

        pnl = 0.0
        close_price = 0.0
        reason = "Closed"
        hold_min = 0
        if deals and len(deals) > 0:
            ds = list(deals)
            total_pnl = sum(d.profit for d in ds)
            pnl = round(total_pnl, 2)
            close_price = ds[-1].price if hasattr(ds[-1], "price") else 0.0
            reason = "TP" if pnl > 0 else "SL"
            if len(ds) >= 1:
                ot = ds[0].time if hasattr(ds[0], "time") else 0
                ct = ds[-1].time if hasattr(ds[-1], "time") else 0
                hold_min = round((ct - ot) / 60, 1) if ct and ot else 0

        self._daily_pnl += pnl
        if pnl < 0:
            self._daily_loss += abs(pnl)
            self._consec_loss += 1
            self._consec_bar = len(self._buf)
            if self._consec_loss >= 2:
                self._cooldown_until = datetime.now(timezone.utc).timestamp() + COOLDOWN_AFTER_LOSS_STREAK
                logger.info("M1 cooldown %ds after %d consecutive losses",
                            COOLDOWN_AFTER_LOSS_STREAK, self._consec_loss)
        else:
            self._consec_loss = 0

        if self._active_trade_id and close_price:
            self._rec.close_trade(self._active_trade_id, close_price, pnl)
        self._active_trade_id = None
        self._active_decision_id = None
        self._save_daily_state()

        if self._emailer:
            now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
            hold_str = f"{hold_min}m" if hold_min > 0 else ""
            self._emailer.send_close(
                time_str=now_str,
                direction="LONG" if pnl >= 0 else "SHORT",
                reason=reason,
                pnl=pnl,
                hold=hold_str,
            )
        logger.info(
            "M1 CLOSE ticket=%d pnl=%.2f reason=%s daily_pnl=%.2f daily_loss=%.2f streak=%dL",
            self._pos_ticket, pnl, reason, self._daily_pnl, self._daily_loss, self._consec_loss,
        )

    # ------------------------------------------------------------------
    # 移动止损循环
    # ------------------------------------------------------------------
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

                # 阶段 1: 盈利 +1.5U → 保本 (锁 +0.3 价格点微小盈利)
                if profit >= BE_TRIGGER and self._pos_sl_stage < 1:
                    offset = 0.3  # 价格点 ≈ 0.3U for 0.01 lot
                    new_sl = round(entry + offset, 2) if direction == "buy" else round(entry - offset, 2)
                    await self._exe.modify_sl(self._cfg.symbol, self._pos_ticket, new_sl)
                    self._pos_sl = new_sl
                    self._pos_sl_stage = 1
                    logger.info("M1 BE: ticket=%d new_sl=%.2f profit=%.2f", self._pos_ticket, new_sl, profit)

                # 阶段 2: 盈利 +3.0U → 锁利 +1.5U
                elif profit >= PL_TRIGGER and self._pos_sl_stage < 2:
                    offset = 1.5  # 价格点 ≈ 1.5U for 0.01 lot
                    new_sl = round(entry + offset, 2) if direction == "buy" else round(entry - offset, 2)
                    await self._exe.modify_sl(self._cfg.symbol, self._pos_ticket, new_sl)
                    self._pos_sl = new_sl
                    self._pos_sl_stage = 2
                    logger.info("M1 PL: ticket=%d new_sl=%.2f profit=%.2f", self._pos_ticket, new_sl, profit)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("M1 trailing: %s", e)

    # ------------------------------------------------------------------
    # 日清理 + 静默告警
    # ------------------------------------------------------------------
    async def _cleanup_routine(self):
        while self._running:
            try:
                await asyncio.sleep(30)
                now_bj = datetime.now(timezone.utc) + timedelta(hours=8)

                # 北京时间 23:55 强制平仓 + 日重置
                if now_bj.hour == 23 and now_bj.minute >= 55:
                    if self._pos_ticket:
                        await self._exe.close_all_positions(self._cfg.symbol)
                        self._pos_ticket = None
                        self._pos_direction = None
                        self._pos_sl_stage = 0
                    self._trade_count = 0
                    self._daily_pnl = 0.0
                    self._daily_loss = 0.0
                    self._consec_loss = 0
                    self._consec_bar = 0
                    self._cooldown_until = 0.0
                    await asyncio.sleep(61)

                # 静默告警: 1 小时无任何动作
                if self._emailer:
                    now_ts = datetime.now(timezone.utc).timestamp()
                    last_action = self._last_order_time or now_ts
                    if now_ts - last_action > 3600:
                        if not self._last_warning_time or now_ts - self._last_warning_time > 3600:
                            self._last_warning_time = now_ts
                            last_time_str = "N/A"
                            if self._last_order_time:
                                last_time_str = datetime.fromtimestamp(
                                    self._last_order_time, tz=timezone.utc
                                ).strftime("%H:%M")
                            self._emailer.send_silent_warning(
                                reason=self._silence_reason or "No signal for >1h",
                                last_time=last_time_str,
                                status=self.get_status(),
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("M1 cleanup: %s", e)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def get_status(self):
        return {
            "state": "RUNNING" if self._running else "STOPPED",
            "running": self._running,
            "position": self._pos_ticket,
            "direction": self._pos_direction,
            "streak": f"{self._consec_loss}L",
            "silence_reason": self._silence_reason or "",
            "trade_count": self._trade_count,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_loss": round(self._daily_loss, 2),
            "strategy": "M1",
        }
