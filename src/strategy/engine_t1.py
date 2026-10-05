"""T1: 高频刮头皮测试策略 (验证下单/平仓流程, 非盈利导向)

目的: 测试策略执行链路 (下单→持仓→平仓→记录) 是否通畅。
  - 用 1 分钟(M1) K 线 EMA5/EMA10 快速判断方向
  - 无持仓时每个 tick 立即下单 (最小间隔 1 秒)
  - 盈亏驱动平仓:
      盈利 ≥ 1U → 立即平仓
      亏损 ≥ 2U → 立即平仓
      持仓满整数分钟: 盈利→平仓, 亏损→再等一分钟
  - 平仓后继续下一轮, 形成高频循环

注意: 此为测试策略, 不追求盈利。0.01 手小资金。
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5
import numpy as np

# 双模式导入: 内置相对导入 / 云端绝对导入 fallback
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

HOLD_SECONDS = 15          # 持仓时间, 到期强平 (越小越接近"每秒下单")
MIN_ORDER_INTERVAL = 1.0   # 最小下单间隔(秒), 避免同 tick 重复下单
MAX_TEST_TRADES = 1000     # 最大测试交易数, 达到后停止开新单 (防止无限循环撑爆记录)

# 盈亏驱动平仓规则 (用户要求)
TP_USD = 1.0               # 盈利达 +1U → 立即平仓
SL_USD = -2.0              # 亏损达 -2U → 立即平仓
MINUTE_CHECK_INTERVAL = 60 # 每满 60 秒检查一次盈亏状态


def _ema(a, p=9):
    """EMA 指标 (自包含, 不依赖其他策略模块)。"""
    n = len(a)
    r = np.full(n, np.nan)
    m = 2 / (p + 1)
    r[p - 1] = np.mean(a[:p])
    for i in range(p, n):
        r[i] = (a[i] - r[i - 1]) * m + r[i - 1]
    return r


class StrategyEngineT1(BaseStrategy):
    """T1 - 高频刮头皮测试策略。

    用 1 分钟 K 线 EMA5/EMA10 判断方向, 无持仓即下单,
    盈亏驱动平仓 (盈利1U/亏损2U/分钟检查), 平仓后立即开下一单。仅用于流程测试。
    """

    def __init__(self, executor, config=None, recorder=None, emailer=None):
        self._exe = executor
        self._cfg = config or StrategyConfig()
        # 测试策略强制 0.01 手, 不受 .env 配置影响
        self._cfg.volume = 0.01
        self._rec = recorder or Recorder()
        self._emailer = emailer  # 测试策略不发邮件 (避免刷屏)
        self._running = False

        self._buf = []           # 已收盘的 1 分钟 K 线
        self._cur_bar = None     # 当前未收盘的 1 分钟 K 线

        # 持仓跟踪
        self._pos_ticket = None
        self._pos_direction = None
        self._pos_open_time = 0.0
        self._pos_entry = 0.0
        self._last_minute_check = 0  # 上次分钟检查的分钟数 (用于"再等一分钟")

        self._last_order_ts = 0.0
        self._trade_count = 0
        self._active_trade_id = None
        self._active_decision_id = None
        self._close_task = None  # 后台平仓检查循环 (不依赖 tick, 防止断流导致不平仓)

    # ------------------------------------------------------------------
    async def start(self):
        self._running = True
        await self._preload()
        # 启动后台平仓检查循环 (独立于 tick, 防止 tick 断流导致持仓不被检查)
        self._close_task = asyncio.create_task(self._close_loop())
        logger.info(
            "T1 started: %s vol=%.2f 盈亏平仓(TP=%+dU/SL=%+dU/分钟检查) max=%d",
            self._cfg.symbol, self._cfg.volume, int(TP_USD), int(SL_USD), MAX_TEST_TRADES,
        )

    async def stop(self):
        self._running = False
        if self._close_task:
            self._close_task.cancel()
            try:
                await asyncio.wait_for(self._close_task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("T1 stopped, 共完成 %d 笔测试交易", self._trade_count)

    async def on_tick(self, tick: Tick):
        if not self._running:
            return
        self._update_bar(tick)

        # 持仓中不开新单 (平仓由后台 _close_loop 负责, 不依赖 tick)
        if self._pos_ticket:
            return

        # 达到最大测试数 → 停止开新单
        if self._trade_count >= MAX_TEST_TRADES:
            return

        # 无持仓 → 快速判断方向并下单 (受最小间隔约束)
        now = time.time()
        if now - self._last_order_ts < MIN_ORDER_INTERVAL:
            return
        direction = self._calc_direction()
        if direction:
            await self._open_order(direction, tick.bid)

    async def _close_loop(self):
        """后台平仓检查循环, 每 2 秒检查持仓盈亏并执行平仓规则。
        独立于 tick 流, 防止 tick 断流时持仓不被检查 (根因: 旧版只在 on_tick 里检查)。
        """
        while self._running:
            try:
                if self._pos_ticket:
                    await self._check_and_close()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("T1 close loop error: %s", e)
            await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # 1 分钟 K 线维护 (用 tick 实时聚合)
    # ------------------------------------------------------------------
    def _update_bar(self, tick):
        ts = tick.timestamp or datetime.now(timezone.utc)
        minute_ts = int(ts.timestamp()) // 60 * 60
        if self._cur_bar is None or self._cur_bar["t"] != minute_ts:
            if self._cur_bar:
                self._buf.append(self._cur_bar)
                if len(self._buf) > 50:
                    self._buf.pop(0)
            self._cur_bar = {
                "t": minute_ts, "o": tick.bid, "h": tick.bid,
                "l": tick.bid, "c": tick.bid,
            }
        else:
            b = self._cur_bar
            b["h"] = max(b["h"], tick.bid)
            b["l"] = min(b["l"], tick.bid)
            b["c"] = tick.bid

    # ------------------------------------------------------------------
    # 方向计算: 1 分钟 K 线 EMA5 vs EMA10
    # ------------------------------------------------------------------
    def _calc_direction(self):
        bars = self._buf + ([self._cur_bar] if self._cur_bar else [])
        if len(bars) < 12:
            return None
        closes = np.array([b["c"] for b in bars])
        e5 = _ema(closes, 5)
        e10 = _ema(closes, 10)
        i = len(closes) - 1
        if np.isnan(e5[i]) or np.isnan(e10[i]):
            return None
        # EMA5 > EMA10 → 短期向上 → buy; 否则 sell
        return "buy" if e5[i] > e10[i] else "sell"

    # ------------------------------------------------------------------
    # 开仓 (市价, 不带 SL/TP, 靠时间平仓)
    # ------------------------------------------------------------------
    async def _open_order(self, direction, price):
        vol = self._cfg.volume
        if direction == "buy":
            result = await self._exe.buy_market(self._cfg.symbol, vol, sl=0, tp=0)
        else:
            result = await self._exe.sell_market(self._cfg.symbol, vol, sl=0, tp=0)

        if not result.get("success"):
            logger.warning("T1 order failed: %s", result.get("error", "unknown"))
            return

        ticket = result.get("order", 0)
        fill = result.get("price", price)
        self._pos_ticket = ticket
        self._pos_direction = direction
        self._pos_entry = fill
        self._pos_open_time = time.time()
        self._last_order_ts = self._pos_open_time
        self._last_minute_check = 0  # 重置分钟检查计数
        self._trade_count += 1

        did = self._rec.record_decision(
            direction=direction.upper(), strategy="T1", price=price,
            volume=vol, order_id=ticket, reason="高频测试 EMA5/10",
        )
        self._active_trade_id = self._rec.record_trade(
            direction=direction.upper(), open_price=fill, volume=vol,
            strategy="T1", open_order_id=ticket, decision_id=did, sl=0, tp=0,
        )
        self._active_decision_id = did

        # 每 10 笔打印一次汇总, 避免日志爆炸
        if self._trade_count % 10 == 0:
            logger.info("T1 已完成 %d 笔测试交易", self._trade_count)
        else:
            logger.debug("T1 OPEN #%d %s @ %.2f ticket=%d",
                         self._trade_count, direction.upper(), fill, ticket)

    # ------------------------------------------------------------------
    # 盈亏驱动平仓 (用户要求):
    #   盈利 ≥ 1U → 立即平仓
    #   亏损 ≥ 2U → 立即平仓
    #   持仓满整数分钟: 盈利→平仓, 亏损→再等一分钟
    # ------------------------------------------------------------------
    async def _check_and_close(self):
        if not self._pos_ticket:
            return
        try:
            pos = await self._exe._run_in_executor(
                lambda: mt5.positions_get(ticket=self._pos_ticket)
            )
            if not pos or len(pos) == 0:
                # 持仓已不存在, 清理状态
                await self._handle_close(0.0, 0.0, "GONE")
                return
            p = pos[0]
            pnl = float(p.profit)
            close_price = float(p.price_current)
            elapsed = time.time() - self._pos_open_time

            # 规则1: 盈利 ≥ 1U → 立即平仓
            if pnl >= TP_USD:
                logger.info("T1 止盈平仓: pnl=+%.2f elapsed=%.0fs", pnl, elapsed)
                await self._exe.close_all_positions(self._cfg.symbol)
                await self._handle_close(pnl, close_price, "TP1U")
                return
            # 规则2: 亏损 ≥ 2U → 立即平仓
            if pnl <= SL_USD:
                logger.info("T1 止损平仓: pnl=%.2f elapsed=%.0fs", pnl, elapsed)
                await self._exe.close_all_positions(self._cfg.symbol)
                await self._handle_close(pnl, close_price, "SL2U")
                return
            # 规则3: 持仓满整数分钟时检查
            minutes_elapsed = int(elapsed // MINUTE_CHECK_INTERVAL)
            if minutes_elapsed > self._last_minute_check and minutes_elapsed >= 1:
                self._last_minute_check = minutes_elapsed
                if pnl > 0:
                    # 盈利 → 平仓
                    logger.info("T1 分钟盈利平仓: pnl=+%.2f elapsed=%.0fs", pnl, elapsed)
                    await self._exe.close_all_positions(self._cfg.symbol)
                    await self._handle_close(pnl, close_price, f"MIN+{pnl:.2f}")
                    return
                else:
                    # 亏损 → 再等一分钟
                    logger.info(
                        "T1 持仓亏损再等: pnl=%.2f elapsed=%.0fs (等到下一分钟)",
                        pnl, elapsed,
                    )
        except Exception as e:
            logger.warning("T1 close check error: %s", e)

    async def _handle_close(self, pnl, close_price, reason):
        """记录平仓, 用 position 的 profit (不依赖可能延迟的 history_deals_get)。"""
        if self._active_trade_id and close_price:
            try:
                self._rec.close_trade(self._active_trade_id, close_price, pnl)
            except Exception:
                pass
        self._active_trade_id = None
        hold = time.time() - self._pos_open_time
        logger.info(
            "T1 CLOSE #%d ticket=%d pnl=%+.2f hold=%.0fs reason=%s",
            self._trade_count, self._pos_ticket, pnl, hold, reason,
        )
        self._pos_ticket = None
        self._pos_direction = None

    # ------------------------------------------------------------------
    # 预加载历史 1 分钟 K 线
    # ------------------------------------------------------------------
    async def _preload(self):
        def f():
            r = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M1, 0, 30)
            if r is None:
                return
            for row in r:
                self._buf.append({
                    "t": int(row[0]),
                    "o": float(row[1]), "h": float(row[2]),
                    "l": float(row[3]), "c": float(row[4]),
                })
        await MT5Executor._run_in_executor(f)
        logger.info("T1 preloaded %d M1 bars", len(self._buf))

    # ------------------------------------------------------------------
    def get_status(self):
        hold = round(time.time() - self._pos_open_time, 1) if self._pos_ticket else 0
        return {
            "state": "RUNNING" if self._running else "STOPPED",
            "running": self._running,
            "position": self._pos_ticket,
            "direction": self._pos_direction,
            "hold_sec": hold,
            "trade_count": self._trade_count,
            "streak": "0W/0L",  # T1 是测试策略, 不追踪连胜/连败
            "strategy": "T1",
        }
