"""G2: XAUUSD 三线顺微利策略 (每天稳定交易, 稳字优先)

核心设计:
  1. M5 K 线, EMA10/20/30 三线排列判断趋势
  2. 三线多头排列 → 做多, 三线空头排列 → 做空
  3. 入场: 价格回踩 EMA20 + K 线确认
  4. 全天候交易 — 不挑时段
  5. 固定止盈 +2.0U, 固定止损 -1.0U (含点差后: 盈+1.66U 亏-1.34U)
  6. 日目标 +8U 即停, 日亏 -4U 即停
  7. 追求 >50% 胜率, 靠盈亏比累积盈利

数学优势: 需要胜率 > 45% 即盈利
  盈 = 2.0 - 0.34 = 1.66U
  亏 = 1.0 + 0.34 = 1.34U
  盈亏比 = 1.66 / 1.34 ≈ 1.24
  盈亏平衡点 = 1.34 / (1.34 + 1.66) ≈ 44.7%
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

# ── 账户风控 (150U) ──────────────────────────────────────────────
ACCOUNT_SIZE = 150.0
MAX_VOLUME = 0.01
MAX_DAILY_LOSS = 4.0
DAILY_TARGET = 8.0
MAX_DAILY_TRADES = 15
MIN_DAILY_TRADES = 2
CONSEC_LOSS_LIMIT = 3
TP_FIXED = 2.0
SL_FIXED = 1.0

# ── 数据文件 ──────────────────────────────────────────────────────
def _g2_data_dir():
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

G2_DATA_DIR = _g2_data_dir()
DAILY_STATE_FILE = G2_DATA_DIR / "g2_daily_state.json"


# ═══════════════════════════════════════════════════════════════════
# M5 K 线聚合器
# ═══════════════════════════════════════════════════════════════════
class M5Agg:
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
class StrategyEngineG2(BaseStrategy):
    """G2 — XAUUSD 三线顺微利策略。每天稳定交易，稳字优先。"""

    def __init__(self, executor, config=None, recorder=None, emailer=None):
        self._exe = executor
        self._cfg = config or StrategyConfig()
        self._cfg.volume = MAX_VOLUME
        self._rec = recorder or Recorder()
        self._emailer = emailer
        self._running = False
        self._agg = None
        self._buf = []  # M5 buf

        self._trade_count = 0
        self._daily_pnl = 0.0
        self._daily_loss = 0.0
        self._consec_loss = 0
        self._last_day = -1
        self._last_trade_ts = 0.0

        self._pos_ticket = None
        self._pos_direction = None
        self._pos_entry = 0.0
        self._pos_sl = 0.0
        self._pos_tp = 0.0
        self._pos_sl_stage = 0
        self._pos_open_ts = 0.0
        self._active_trade_id = None
        self._active_decision_id = None

        self._tt = None
        self._ct = None

    async def start(self):
        self._running = True
        self._agg = M5Agg(self._on_candle)
        self._load_daily_state()
        await self._preload()
        self._tt = asyncio.create_task(self._trailing_loop())
        self._ct = asyncio.create_task(self._cleanup_loop())
        logger.info("G2 started: %s TP=+%.1fU SL=-%.1fU", self._cfg.symbol, TP_FIXED, SL_FIXED)

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
        self._save_daily_state()
        logger.info("G2 stopped trades=%d daily_pnl=%.2f", self._trade_count, self._daily_pnl)

    async def on_tick(self, tick):
        if self._running and self._agg:
            self._agg.add_tick(tick)

    def _save_daily_state(self):
        try:
            G2_DATA_DIR.mkdir(parents=True, exist_ok=True)
            DAILY_STATE_FILE.write_text(json.dumps({
                "day": self._last_day, "trade_count": self._trade_count,
                "daily_pnl": self._daily_pnl, "daily_loss": self._daily_loss,
                "consec_loss": self._consec_loss,
            }), encoding="utf-8")
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
                self._last_day = today
        except Exception:
            pass

    async def _preload(self):
        def f():
            r = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M5, 0, 200)
            if r:
                for row in r:
                    dt = datetime.fromtimestamp(row[0], tz=timezone.utc)
                    self._buf.append({
                        "t": dt, "o": float(row[1]), "h": float(row[2]),
                        "l": float(row[3]), "c": float(row[4]), "v": int(row[5]),
                    })
        await MT5Executor._run_in_executor(f)
        logger.info("G2 preloaded %d M5 bars", len(self._buf))

    # ── 时段标注 (全时段可交易) ──
    @staticmethod
    def _session_label():
        now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
        h, m = now_bj.hour, now_bj.minute
        bj_ts = h * 60 + m
        if 8 * 60 <= bj_ts < 15 * 60 + 20:
            return "ASIAN"
        if 15 * 60 + 30 <= bj_ts < 16 * 60:
            return "BLOCK1"
        if 16 * 60 <= bj_ts < 20 * 60 + 20:
            return "EURO"
        if 20 * 60 + 30 <= bj_ts < 22 * 60:
            return "BLOCK2"
        if 22 * 60 <= bj_ts < 23 * 60 + 30:
            return "USLATE"
        return "OFF"

    # ── M5 K 线处理 ──
    async def _on_candle(self, c):
        if not self._running:
            return
        self._buf.append(c)
        if len(self._buf) > 200:
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

        e10 = EMA(closes, 10)
        e20 = EMA(closes, 20)
        e30 = EMA(closes, 30)
        rs = RSI(closes, 14)
        at = ATR(highs, lows, closes, 14)

        if any(np.isnan(v) for v in [e10[i], e20[i], e30[i], rs[i], at[i]]):
            return

        cl = closes[i]; lo = lows[i]; hi = highs[i]

        # 日重置
        ts = c["t"].timestamp()
        dy = int(ts / 86400)
        if dy != self._last_day:
            self._trade_count = 0
            self._daily_pnl = 0.0
            self._daily_loss = 0.0
            self._consec_loss = 0
            self._last_day = dy
            self._save_daily_state()

        # 持仓检查
        if await self._check_position():
            return

        # 风控
        if self._daily_pnl >= DAILY_TARGET:
            return
        if self._daily_loss >= MAX_DAILY_LOSS:
            return
        if self._trade_count >= MAX_DAILY_TRADES:
            return
        if self._consec_loss >= CONSEC_LOSS_LIMIT:
            return

        # ── 三线排列判断 ──
        # 多头: e10 > e20 > e30
        bull = e10[i] > e20[i] > e30[i]
        # 空头: e10 < e20 < e30
        bear = e10[i] < e20[i] < e30[i]
        if not (bull or bear):
            return

        # ── 入场 ──
        signal = None
        atr_val = float(at[i])

        if bull:
            # 多头: 价格回踩 EMA20 附近 (±0.3 ATR)
            lower = e20[i] - atr_val * 0.3
            upper = e20[i] + atr_val * 0.1
            if lo <= e20[i] and cl >= lower:
                # K线确认: 下影线 + 收盘回到 EMA20 附近
                if cl > e20[i] or (hi - lo) > atr_val * 0.5:
                    if rs[i] < 75:
                        signal = "buy"
        elif bear:
            upper = e20[i] + atr_val * 0.3
            lower = e20[i] - atr_val * 0.1
            if hi >= e20[i] and cl <= upper:
                if cl < e20[i] or (hi - lo) > atr_val * 0.5:
                    if rs[i] > 25:
                        signal = "sell"

        if not signal:
            return

        await self._open_order(signal, cl)

    # ── 持仓/平仓/开仓 ──
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

    def _points_to_price(self, profit_usd):
        return max(profit_usd * 1.5, 0.1)

    async def _open_order(self, direction, price):
        vol = self._cfg.volume
        sl_dist = self._points_to_price(SL_FIXED)
        tp_dist = self._points_to_price(TP_FIXED)
        if sl_dist <= 0 or tp_dist <= 0:
            return

        if direction == "buy":
            sl = round(price - sl_dist, 2)
            tp = round(price + tp_dist, 2)
            result = await self._exe.buy_market(self._cfg.symbol, vol, sl=sl, tp=tp)
        else:
            sl = round(price + sl_dist, 2)
            tp = round(price - tp_dist, 2)
            result = await self._exe.sell_market(self._cfg.symbol, vol, sl=sl, tp=tp)

        if not result.get("success"):
            logger.warning("G2 order failed: %s", result.get("error"))
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
        sess = self._session_label()
        did = self._rec.record_decision(
            direction=direction.upper(), strategy="G2", price=price,
            volume=vol, order_id=ticket, reason=f"G2_{sess}",
        )
        self._active_trade_id = self._rec.record_trade(
            direction=direction.upper(), open_price=fill, volume=vol,
            strategy="G2", open_order_id=ticket, decision_id=did, sl=sl, tp=tp,
        )
        self._active_decision_id = did
        self._save_daily_state()
        logger.info("G2 %s @ %.2f sl=%.2f tp=%.2f #%d", direction.upper(), price, sl, tp, self._trade_count)

    async def _handle_close(self):
        pnl = 0.0; close_price = 0.0; reason = "C"
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
            self._daily_loss += abs(pnl); self._consec_loss += 1
        else:
            self._consec_loss = 0
        if self._active_trade_id and close_price:
            try:
                self._rec.close_trade(self._active_trade_id, close_price, pnl)
            except Exception:
                pass
        self._active_trade_id = None
        self._save_daily_state()
        if pnl:
            logger.info("G2 CLOSE pnl=%.2f daily=%.2f %s", pnl, self._daily_pnl, reason)
        self._pos_ticket = None; self._pos_direction = None; self._pos_sl_stage = 0

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
                if profit >= 3.0 and self._pos_sl_stage < 1:
                    offset = 0.3
                    new_sl = round(entry + offset, 2) if direction == "buy" else round(entry - offset, 2)
                    await self._exe.modify_sl(self._cfg.symbol, self._pos_ticket, new_sl)
                    self._pos_sl = new_sl; self._pos_sl_stage = 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("G2 trailing: %s", e)

    async def _cleanup_loop(self):
        while self._running:
            try:
                await asyncio.sleep(60)
                now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
                if now_bj.hour == 23 and now_bj.minute >= 55:
                    if self._pos_ticket:
                        await self._exe.close_all_positions(self._cfg.symbol)
                        self._pos_ticket = None
                    await asyncio.sleep(70)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("G2 cleanup: %s", e)

    def get_status(self):
        return {
            "state": "RUNNING" if self._running else "STOPPED",
            "running": self._running,
            "position": self._pos_ticket,
            "direction": self._pos_direction,
            "streak": f"{self._consec_loss}L" if self._consec_loss > 0 else "0L",
            "trade_count": self._trade_count,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_loss": round(self._daily_loss, 2),
            "session": self._session_label(),
            "tradeable": True,
            "strategy": "G2",
        }
