"""V5 High Conviction Strategy: RSI Divergence + H1 Trend + BB Touch"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import numpy as np

from ..market_data.models import Tick
from ..trading.mt5_executor import MT5Executor
from .config import StrategyConfig
from .indicators import SMA
from .recorder import Recorder
from ..notifier.email_alerter import EmailAlerter
from .base import BaseStrategy

logger = logging.getLogger(__name__)
SPREAD = 0.34


def calc_bb(c, p=20, s=2.0):
    n = len(c); m = np.full(n, np.nan); u = np.full(n, np.nan); l = np.full(n, np.nan)
    for i in range(p-1, n):
        w = c[i-p+1:i+1]; av = np.mean(w); sd = np.std(w, ddof=0)
        m[i] = av; u[i] = av + s * sd; l[i] = av - s * sd
    return m, u, l


def calc_rsi7(c):
    n = len(c); r = np.full(n, np.nan)
    for i in range(1, n):
        up = sum(c[j+1]-c[j] for j in range(max(0, i-7), i) if c[j+1] > c[j])
        dn = sum(c[j]-c[j+1] for j in range(max(0, i-7), i) if c[j+1] <= c[j])
        r[i] = 50 if up + dn == 0 else 100 - 100 / (1 + up / (dn + 1e-10))
    return r


def detect_divergence(price, rsi, idx, lookback=20):
    n = min(lookback, idx)
    if n < 10: return 0
    p_seg = price[idx-n:idx+1]; r_seg = rsi[idx-n:idx+1]
    p_lo = np.argmin(p_seg); p_hi = np.argmax(p_seg)
    r_lo = np.argmin(r_seg); r_hi = np.argmax(r_seg)
    if p_lo > r_lo and r_seg[r_lo] < r_seg[-1] * 1.02 and p_seg[-1] <= p_seg[p_lo]:
        return 1
    if p_hi < r_hi and r_seg[r_hi] > r_seg[-1] * 0.98 and p_seg[-1] >= p_seg[p_hi]:
        return -1
    return 0


class TickAggregator:
    def __init__(self, symbol, callback):
        self._symbol = symbol; self._callback = callback; self._candle = None

    def add_tick(self, tick):
        ts = tick.timestamp or datetime.now(timezone.utc)
        m5 = self._round_m5(ts)
        if self._candle is None or m5 > self._candle["time"]:
            old = self._candle
            self._candle = {"time": m5, "open": tick.bid, "high": tick.bid,
                            "low": tick.bid, "close": tick.bid, "volume": 1}
            if old is not None and old["volume"] > 0:
                asyncio.create_task(self._callback(old))
        else:
            c = self._candle
            c["high"] = max(c["high"], tick.bid); c["low"] = min(c["low"], tick.bid)
            c["close"] = tick.bid; c["volume"] += 1

    def flush(self):
        if self._candle and self._candle["volume"] > 0:
            asyncio.create_task(self._callback(self._candle))
        self._candle = None

    @staticmethod
    def _round_m5(dt):
        ts = int(dt.timestamp()); ts = ts - (ts % 300)
        return datetime.fromtimestamp(ts, tz=timezone.utc)


class StrategyEngineV5(BaseStrategy):

    def __init__(self, executor: MT5Executor, config: StrategyConfig | None = None,
                 recorder: Recorder | None = None, emailer: EmailAlerter | None = None):
        self._exe = executor; self._cfg = config or StrategyConfig()
        self._rec = recorder or Recorder(); self._emailer = emailer
        self._running = False; self._agg = None
        self._m5_buf = []; self._cleanup_task = None
        self._trailing_task = None
        self._position = None  # single high-conviction position only
        self._last_order_time = None; self._last_warning_time = None
        self._silence_reason = "Idle"
        self._h1_sma20 = 0.0; self._h1_updated = 0.0

    async def start(self):
        self._running = True
        self._agg = TickAggregator(self._cfg.symbol, self._on_m5_candle)
        await self._prefetch_history()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._trailing_task = asyncio.create_task(self._position_monitor())
        logger.info("V5 started: %s vol=%.2f TP=80pt SL=10pt", self._cfg.symbol, self._cfg.volume)

    async def stop(self):
        self._running = False
        if self._agg: self._agg.flush()
        for task in [self._trailing_task, self._cleanup_task]:
            if task: task.cancel(); await asyncio.sleep(0)
        self._rec.close()
        logger.info("V5 stopped")

    async def on_tick(self, tick: Tick):
        if self._running and self._agg: self._agg.add_tick(tick)

    async def _on_m5_candle(self, candle):
        if not self._running: return
        self._m5_buf.append(candle)
        if len(self._m5_buf) > 200: self._m5_buf.pop(0)
        if len(self._m5_buf) < 50: return
        await self._process(candle)

    async def _process(self, candle):
        t = candle["time"].timestamp()
        close = candle["close"]; high = candle["high"]; low = candle["low"]

        # Refresh H1 macro
        if t - self._h1_updated > 60:
            await self._refresh_h1_macro(); self._h1_updated = t

        closes = np.array([c["close"] for c in self._m5_buf], dtype=float)
        highs = np.array([c["high"] for c in self._m5_buf], dtype=float)
        lows = np.array([c["low"] for c in self._m5_buf], dtype=float)

        bb_m, bb_u, bb_l = calc_bb(closes, 20, 2.0)
        rsi = calc_rsi7(closes)
        i = len(self._m5_buf) - 1

        if np.isnan(bb_u[i]) or np.isnan(rsi[i]): return

        # Check divergence
        dv = detect_divergence(closes, rsi, i)
        if dv == 0:
            self._silence_reason = "No divergence"
            return

        # Check H1 direction
        h1_bull = close > self._h1_sma20

        # Check BB touch
        touch_buf = (bb_u[i] - bb_l[i]) * 0.02

        if dv == 1 and h1_bull and low <= bb_l[i] + touch_buf:
            # Bullish divergence + uptrend + touch lower BB = LONG
            tp = round(close + 0.80 + SPREAD, 2)
            sl = round(close - 0.10, 2)
            r = await self._exe.buy_market(self._cfg.symbol, self._cfg.volume)
            if r.get("success"):
                self._position = {"dir": "long", "entry": r["price"], "tp": tp, "sl": sl, "ot": t, "ticket": r.get("order", 0)}
                self._rec.record_decision("v5_div_buy", self._cfg.strategy_name, price=r["price"])
                self._last_order_time = t
                logger.info("V5 LONG div=%.2f sl=%.2f tp=%.2f", r["price"], sl, tp)
                if self._emailer:
                    self._emailer.send_order(datetime.now().strftime("%H:%M"), "Buy", "V5-Div", self._cfg.volume, price=r["price"])

        elif dv == -1 and not h1_bull and high >= bb_u[i] - touch_buf:
            # Bearish divergence + downtrend + touch upper BB = SHORT
            tp = round(close - 0.80 - SPREAD, 2)
            sl = round(close + 0.10, 2)
            q = await self._exe.get_quote(self._cfg.symbol)
            if q:
                r = await self._exe.sell_market(self._cfg.symbol, self._cfg.volume)
                if r.get("success"):
                    self._position = {"dir": "short", "entry": r["price"], "tp": tp, "sl": sl, "ot": t, "ticket": r.get("order", 0)}
                    self._rec.record_decision("v5_div_sell", self._cfg.strategy_name, price=r["price"])
                    self._last_order_time = t
                    logger.info("V5 SHORT div=%.2f sl=%.2f tp=%.2f", r["price"], sl, tp)
                    if self._emailer:
                        self._emailer.send_order(datetime.now().strftime("%H:%M"), "Sell", "V5-Div", self._cfg.volume, price=r["price"])

    async def _position_monitor(self):
        while self._running:
            try:
                pos = self._position
                if pos is None: await asyncio.sleep(2); continue

                q = await self._exe.get_quote(self._cfg.symbol)
                if q is None: await asyncio.sleep(2); continue

                bid = q["bid"]; ask = q["ask"]
                now = datetime.now(timezone.utc).timestamp()
                hold_min = (now - pos["ot"]) / 60
                reason = None

                if pos["dir"] == "long":
                    if bid >= pos["tp"]: reason = "TP"
                    elif bid <= pos["sl"]: reason = "SL"
                else:
                    if ask <= pos["tp"]: reason = "TP"
                    elif ask >= pos["sl"]: reason = "SL"

                if hold_min > 240: reason = "TIMEOUT"

                if reason:
                    r = await self._exe.close_all_positions(self._cfg.symbol)
                    pnl = r[0].get("profit", 0) - SPREAD if r and len(r) > 0 else 0
                    logger.info("V5 CLOSE %s reason=%s pnl=%.2f hold=%.0fmin",
                                pos["dir"].upper(), reason, pnl, hold_min)
                    if self._emailer:
                        self._emailer.send_close(datetime.now().strftime("%H:%M"),
                                                  pos["dir"].capitalize(), reason, pnl, "")
                    self._position = None

                await asyncio.sleep(2)
            except asyncio.CancelledError: break
            except Exception as e: logger.warning("V5 monitor: %s", e); await asyncio.sleep(5)

    async def _refresh_h1_macro(self):
        def _fetch():
            rates = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_H1, 0, 30)
            if rates is None or len(rates) < 20: return
            hc = np.array([r[4] for r in rates], dtype=float)
            for j in range(1, len(hc)):
                if np.isnan(hc[j]): hc[j] = hc[j-1]
            self._h1_sma20 = float(np.mean(hc[-20:]))
        await MT5Executor._run_in_executor(_fetch)

    async def _prefetch_history(self):
        def _fetch():
            rates = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M5, 0, 200)
            if rates is None: return
            for r in rates:
                dt = datetime.fromtimestamp(r[0], tz=timezone.utc)
                self._m5_buf.append({"time": dt, "open": float(r[1]), "high": float(r[2]),
                                     "low": float(r[3]), "close": float(r[4])})
        await MT5Executor._run_in_executor(_fetch)
        logger.info("V5 preloaded %d M5 bars", len(self._m5_buf))

    async def _cleanup_loop(self):
        while self._running:
            try:
                now = datetime.now(timezone.utc) + timedelta(hours=8)
                if now.hour == 23 and now.minute >= 55:
                    if self._position:
                        await self._exe.close_all_positions(self._cfg.symbol)
                        self._position = None
                    await asyncio.sleep(61)
                if self._emailer and self._last_order_time:
                    now_ts = datetime.now(timezone.utc).timestamp()
                    if now_ts - self._last_order_time > 3600 and \
                       (self._last_warning_time is None or now_ts - self._last_warning_time > 3600):
                        self._last_warning_time = now_ts
                        lt = datetime.fromtimestamp(self._last_order_time, tz=timezone.utc)
                        self._emailer.send_silent_warning(self._silence_reason,
                                                           lt.strftime("%Y-%m-%d %H:%M"), self.get_status())
                await asyncio.sleep(30)
            except asyncio.CancelledError: break
            except Exception as e: logger.warning("V5 cleanup: %s", e); await asyncio.sleep(30)

    def get_status(self) -> dict:
        return {
            "state": "DIVERGENCE",
            "running": self._running,
            "position": "OPEN" if self._position else "NONE",
            "silence_reason": self._silence_reason,
            "last_order": self._last_order_time,
            "tp_pts": 80, "sl_pts": 10, "spread": int(SPREAD * 100),
        }
