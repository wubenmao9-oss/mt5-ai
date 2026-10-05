"""Dual-state strategy engine: ADX + Bollinger Bands with tick aggregation"""

import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5
import numpy as np

from ..market_data.models import Tick
from ..trading.mt5_executor import MT5Executor
from .config import StrategyConfig, MT5_TF_MAP
from .indicators import SMA, RSI, BollingerBands, ATR
from .recorder import Recorder
from ..notifier.email_alerter import EmailAlerter
from .base import BaseStrategy

logger = logging.getLogger(__name__)

TF_MT5 = {"M5": mt5.TIMEFRAME_M5, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}


def calc_adx(high, low, close, period=14):
    n = len(close)
    adx = np.full(n, np.nan); pdi = np.full(n, np.nan); ndi = np.full(n, np.nan)
    tr = np.zeros(n); pdm = np.zeros(n); ndm = np.zeros(n)
    pc = np.roll(close, 1); ph = np.roll(high, 1); pl = np.roll(low, 1)
    for i in range(1, n):
        tr[i] = max(high[i]-low[i], abs(high[i]-pc[i]), abs(low[i]-pc[i]))
        u = high[i]-ph[i]; d = pl[i]-low[i]
        if u > d and u > 0: pdm[i] = u
        if d > u and d > 0: ndm[i] = d
    tr_s = np.zeros(n); ps = np.zeros(n); ns = np.zeros(n)
    tr_s[period] = np.sum(tr[1:period+1]); ps[period]=np.sum(pdm[1:period+1]); ns[period]=np.sum(ndm[1:period+1])
    for i in range(period+1, n):
        tr_s[i]=tr_s[i-1]-tr_s[i-1]/period+tr[i]; ps[i]=ps[i-1]-ps[i-1]/period+pdm[i]; ns[i]=ns[i-1]-ns[i-1]/period+ndm[i]
    for i in range(period, n):
        if tr_s[i]: pdi[i]=100*ps[i]/tr_s[i]; ndi[i]=100*ns[i]/tr_s[i]
    dx = np.zeros(n)
    for i in range(period, n):
        s = pdi[i]+ndi[i]
        if s: dx[i]=100*abs(pdi[i]-ndi[i])/s
    adx[period*2] = np.mean(dx[period:period*2+1])
    for i in range(period*2+1, n): adx[i]=adx[i-1]-adx[i-1]/period+dx[i]
    return adx, pdi, ndi


def calc_bb(close, period=20, sd=2.0):
    n = len(close); m=np.full(n,np.nan); u=np.full(n,np.nan); l=np.full(n,np.nan)
    for i in range(period-1, n):
        w=close[i-period+1:i+1]; s=np.mean(w); d=np.std(w,ddof=0); m[i]=s; u[i]=s+sd*d; l[i]=s-sd*d
    return m, u, l


def calc_atr(high, low, close, p=14):
    n=len(close); tr=np.zeros(n); r=np.zeros(n)
    for i in range(1, n): tr[i]=max(high[i]-low[i],abs(high[i]-close[i-1]),abs(low[i]-close[i-1]))
    r[p]=np.mean(tr[1:p+1])
    for i in range(p+1,n): r[i]=(r[i-1]*(p-1)+tr[i])/p
    return r


# ======================================================================
# Tick ? M5 KInit?
# ======================================================================

class TickAggregator:
    """Init? TickInit? M5 KInitInit??"""

    def __init__(self, symbol: str, callback) -> None:
        self._symbol = symbol
        self._callback = callback  # async callable(candle_dict)
        self._candle: dict | None = None

    def add_tick(self, tick: Tick) -> None:
        ts = tick.timestamp or datetime.now(timezone.utc)
        m5 = self._round_m5(ts)

        if self._candle is None or m5 > self._candle["time"]:
            old = self._candle
            self._candle = {
                "time": m5, "open": tick.bid, "high": tick.bid,
                "low": tick.bid, "close": tick.bid, "volume": 1,
            }
            if old is not None and old["volume"] > 0:
                asyncio.create_task(self._callback(old))
        else:
            c = self._candle
            c["high"] = max(c["high"], tick.bid)
            c["low"] = min(c["low"], tick.bid)
            c["close"] = tick.bid
            c["volume"] += 1

    def flush(self) -> None:
        """InitInit K InitInitInit"""
        if self._candle and self._candle["volume"] > 0:
            asyncio.create_task(self._callback(self._candle))
        self._candle = None

    @staticmethod
    def _round_m5(dt: datetime) -> datetime:
        ts = int(dt.timestamp())
        ts = ts - (ts % 300)
        return datetime.fromtimestamp(ts, tz=timezone.utc)


# ======================================================================
# InitInit??
# ======================================================================

class StrategyEngineV2(BaseStrategy):
    """ADX + BB InitInitInit/InitInitInit"""

    def __init__(
        self,
        executor: MT5Executor,
        config: StrategyConfig | None = None,
        recorder: Recorder | None = None,
        emailer: EmailAlerter | None = None,
    ) -> None:
        self._exe = executor
        self._cfg = config or StrategyConfig()
        self._rec = recorder or Recorder()
        self._agg: TickAggregator | None = None
        self._running = False
        self._cleanup_task: asyncio.Task | None = None
        self._m5_buf: list[dict] = []  # M5 candle buffer for indicators

        # ??
        self._state = "RANGING"
        self._touch_top = 0
        self._touch_bot = 0
        self._trend_entered = False
        self._position: dict | None = None
        self._cooldown_until: float = 0.0
        self._last_candle_time: float = 0.0
        self._streak_wins: int = 0
        self._streak_losses: int = 0
        self._emailer: EmailAlerter | None = emailer
        self._last_order_time: float | None = None
        self._last_warning_time: float | None = None
        self._silence_reason: str = "Idle"

    # ----------------------------------------------------------------
    # Init?
    # ----------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._agg = TickAggregator(self._cfg.symbol, self._on_m5_candle)
        await self._prefetch_history()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        stats = self._rec.get_stats()
        logger.info(
            "Engine V2: %s | Trades: %d WR: %.1f%%",
            self._cfg.symbol, stats["total_trades"], stats["win_rate"],
        )

    async def stop(self) -> None:
        self._running = False
        if self._agg:
            self._agg.flush()
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try: await self._cleanup_task
            except asyncio.CancelledError: pass
        self._rec.close()
        logger.info("??V2??")

    # ----------------------------------------------------------------
    # Tick ??
    # ----------------------------------------------------------------

    async def on_tick(self, tick: Tick) -> None:
        if self._running and self._agg:
            self._agg.add_tick(tick)

    # ----------------------------------------------------------------
    # M5 KInit
    # ----------------------------------------------------------------

    async def _on_m5_candle(self, candle: dict) -> None:
        if not self._running:
            return
        self._m5_buf.append(candle)
        if len(self._m5_buf) > 200:
            self._m5_buf.pop(0)

        if len(self._m5_buf) < 50:
            return  # ??

        await self._process(candle)

    async def _process(self, candle: dict) -> None:
        cfg = self._cfg
        t = candle["time"].timestamp()
        close = candle["close"]
        high = candle["high"]
        low = candle["low"]
        open_p = candle["open"]

        # Session filter
        if self._session_cooldown(candle["time"]):
            self._silence_reason = "Pre-market cooldown"; self._cooldown_until = t + 300
            return
        if t < self._cooldown_until:
            self._silence_reason = "Starting"; return

        # Position check
        if self._position:
            self._silence_reason = "Starting"; await self._check_position(candle)
            return

        self._silence_reason = "Waiting for signal"
        # Convert buffers to numpy
        closes = np.array([c["close"] for c in self._m5_buf], dtype=float)
        highs = np.array([c["high"] for c in self._m5_buf], dtype=float)
        lows = np.array([c["low"] for c in self._m5_buf], dtype=float)
        opens = np.array([c.get("open", c["close"]) for c in self._m5_buf], dtype=float)

        # Calculate indicators
        adx_arr, pdi, ndi = calc_adx(highs, lows, closes, 14)
        _, bb_u, bb_l = calc_bb(closes, 20, 2.0)
        atr_arr = calc_atr(highs, lows, closes, 14)

        i = len(self._m5_buf) - 1  # current index
        cur_adx = adx_arr[i]
        cur_bb_u = bb_u[i]
        cur_bb_l = bb_l[i]
        cur_pdi = pdi[i]
        cur_ndi = ndi[i]
        cur_atr = atr_arr[i] if not np.isnan(atr_arr[i]) else 5.0

        if np.isnan(cur_adx) or np.isnan(cur_bb_u):
            return

        bw = cur_bb_u - cur_bb_l
        mid = (cur_bb_u + cur_bb_l) / 2

        # Reset trend phase
        if cur_adx < 23:
            self._trend_entered = False

        bb_break_up = close > cur_bb_u and close > open_p
        bb_break_dn = close < cur_bb_l and close < open_p
        body = abs(close - open_p)

        # --- TRENDING ---
        if cur_adx >= 20 and not self._trend_entered:
            if bb_break_up and cur_pdi > cur_ndi and body > cur_atr * 0.3:
                sl = round(low - cur_atr * 0.2, 2)
                tp = round(close + 2.5 * (close - low + cur_atr * 0.2), 2)
                r = await self._exe.buy_market(cfg.symbol, cfg.volume)
                if r["success"]:
                    self._position = {"dir": "long", "entry": r["price"], "sl": sl, "tp": tp, "et": t}
                    self._trend_entered = True
                    self._touch_top = 0; self._touch_bot = 0
                    self._rec.record_decision("trend_buy", cfg.strategy_name, price=r["price"])
                    self._last_order_time = t; self._cooldown_until = t + 300
                    if self._emailer:
                        self._emailer.send_order(datetime.now().strftime("%H:%M:%S"), "Buy", "V2-Trend", cfg.volume, price=r["price"])
                    logger.info("Init? order=%d price=%.2f", r["order"], r["price"])
                return
            elif bb_break_dn and cur_ndi > cur_pdi and body > cur_atr * 0.3:
                sl = round(high + cur_atr * 0.2, 2)
                tp = round(close - 2.5 * (high - close + cur_atr * 0.2), 2)
                q = await self._exe.get_quote(cfg.symbol)
                if q:
                    r = await self._exe.sell_market(cfg.symbol, cfg.volume)
                    if r["success"]:
                        self._position = {"dir": "short", "entry": r["price"], "sl": sl, "tp": tp, "et": t}
                        self._trend_entered = True
                        self._touch_top = 0; self._touch_bot = 0
                        self._rec.record_decision("trend_sell", cfg.strategy_name, price=r["price"])
                        self._last_order_time = t; self._cooldown_until = t + 300
                        if self._emailer:
                            self._emailer.send_order(datetime.now().strftime("%H:%M:%S"), "Sell", "V2-Trend", cfg.volume, price=r["price"])
                        logger.info("Init? order=%d price=%.2f", r["order"], r["price"])
                return

        # --- RANGING ---
        tol = bw * 0.02
        touched_top = high >= cur_bb_u - tol
        touched_bot = low <= cur_bb_l + tol

        if touched_top:
            self._touch_top += 1
        if touched_bot:
            self._touch_bot += 1
        # Decay when inside
        if high < cur_bb_u and low > cur_bb_l:
            self._touch_top = max(0, self._touch_top - 1)
            self._touch_bot = max(0, self._touch_bot - 1)

        entry_price = None
        direction = None
        if touched_bot and self._touch_bot < 3 and close < mid:
            entry_price = cur_bb_l
            direction = "buy"
        elif touched_top and self._touch_top < 3 and close > mid:
            entry_price = cur_bb_u
            direction = "sell"

        if direction and entry_price:
            sl = entry_price - bw * 0.5 if direction == "buy" else entry_price + bw * 0.5
            tp = mid
            if direction == "buy":
                r = await self._exe.buy_market(cfg.symbol, cfg.volume)
                if r["success"]:
                    self._position = {"dir": "long", "entry": r["price"], "sl": round(sl, 2), "tp": round(tp, 2), "et": t}
                    self._rec.record_decision("range_buy", cfg.strategy_name, price=r["price"])
                    self._last_order_time = t; self._cooldown_until = t + 120
                    if self._emailer: self._emailer.send_order(datetime.now().strftime("%H:%M:%S"), "Buy", "V2-Range", cfg.volume, price=r["price"])
                    logger.info("Range BUY order=%d price=%.2f", r["order"], r["price"])
            else:
                r = await self._exe.sell_market(cfg.symbol, cfg.volume)
                if r["success"]:
                    self._position = {"dir": "short", "entry": r["price"], "sl": round(sl, 2), "tp": round(tp, 2), "et": t}
                    self._rec.record_decision("range_sell", cfg.strategy_name, price=r["price"])
                    self._last_order_time = t; self._cooldown_until = t + 120
                    if self._emailer: self._emailer.send_order(datetime.now().strftime("%H:%M:%S"), "Sell", "V2-Range", cfg.volume, price=r["price"])
                    logger.info("Range SELL order=%d price=%.2f", r["order"], r["price"])

    # ----------------------------------------------------------------
    # Init?
    # ----------------------------------------------------------------

    async def _check_position(self, candle: dict) -> None:
        cfg = self._cfg
        high = candle["high"]; low = candle["low"]
        was_win = False
        if self._position["dir"] == "long" and low <= self._position["sl"]:
            r = await self._exe.close_all_positions(cfg.symbol)
            pnl = sum(x.get("profit", 0) for x in r) if r else 0
            self._rec.record_decision("close_long_sl", cfg.strategy_name, price=self._position["sl"])
            if self._emailer:
                self._emailer.send_close(datetime.now().strftime("%H:%M:%S"), "Long", "Stop Loss", pnl, "")
            self._position = None; was_win = False
        elif self._position["dir"] == "long" and high >= self._position["tp"]:
            r = await self._exe.close_all_positions(cfg.symbol)
            pnl = sum(x.get("profit", 0) for x in r) if r else 0
            self._rec.record_decision("close_long_tp", cfg.strategy_name, price=self._position["tp"])
            if self._emailer:
                self._emailer.send_close(datetime.now().strftime("%H:%M:%S"), "Long", "Take Profit", pnl, "")
            self._position = None; was_win = True
        elif self._position["dir"] == "short" and high >= self._position["sl"]:
            r = await self._exe.close_all_positions(cfg.symbol)
            pnl = sum(x.get("profit", 0) for x in r) if r else 0
            self._rec.record_decision("close_short_sl", cfg.strategy_name, price=self._position["sl"])
            if self._emailer:
                self._emailer.send_close(datetime.now().strftime("%H:%M:%S"), "Short", "Stop Loss", pnl, "")
            self._position = None; was_win = False
        elif self._position["dir"] == "short" and low <= self._position["tp"]:
            r = await self._exe.close_all_positions(cfg.symbol)
            pnl = sum(x.get("profit", 0) for x in r) if r else 0
            self._rec.record_decision("close_short_tp", cfg.strategy_name, price=self._position["tp"])
            if self._emailer:
                self._emailer.send_close(datetime.now().strftime("%H:%M:%S"), "Short", "Take Profit", pnl, "")
            self._position = None; was_win = True
        
        if self._position is None:
            if was_win:
                self._streak_wins += 1; self._streak_losses = 0
            else:
                self._streak_losses += 1; self._streak_wins = 0
            if self._streak_wins >= 3 or self._streak_losses >= 3:
                tag = f"{self._streak_wins}??" if self._streak_wins >= 3 else f"{self._streak_losses}??"
                logger.info("InitInit: %s, pause 30min", tag)
                self._cooldown_until = candle["time"].timestamp() + 1800
                self._streak_wins = 0; self._streak_losses = 0

    # ----------------------------------------------------------------
    # Session filter
    # ----------------------------------------------------------------

    @staticmethod
    def _session_cooldown(dt: datetime) -> bool:
        bjt = (dt.hour + 8) % 24 * 60 + dt.minute
        return (450 <= bjt < 510) or (860 <= bjt < 900) or (1230 <= bjt < 1260)

    # ----------------------------------------------------------------
    # InitInit?
    # ----------------------------------------------------------------

    async def _prefetch_history(self) -> None:
        def _fetch():
            rates = mt5.copy_rates_from_pos(self._cfg.symbol, mt5.TIMEFRAME_M5, 0, 200)
            if rates is None:
                return
            for r in rates:
                dt = datetime.fromtimestamp(r[0], tz=timezone.utc)
                self._m5_buf.append({
                    "time": dt, "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]),
                })
        await MT5Executor._run_in_executor(_fetch)
        logger.info("Preloaded %d M5 bars", len(self._m5_buf))

    # ----------------------------------------------------------------
    # Init?
    # ----------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        while self._running:
            try:
                now_bjt = datetime.now(timezone.utc) + timedelta(hours=8)
                # Watchdog: 1 hour silent warning
                if self._emailer and self._last_order_time:
                    now_ts = datetime.now(timezone.utc).timestamp()
                    if now_ts - self._last_order_time > 3600:
                        if not self._last_warning_time or now_ts - self._last_warning_time > 3600:
                            self._last_warning_time = now_ts
                            lt = datetime.fromtimestamp(self._last_order_time, tz=timezone.utc)
                            self._emailer.send_silent_warning(self._silence_reason, lt.strftime("%Y-%m-%d %H:%M:%S"), self.get_status())
                now = now_bjt
                if now.hour == 23 and now.minute == 55:
                    logger.info("??: Init? + ??")
                    await self._exe.cancel_all_pending(self._cfg.symbol)
                    await self._exe.close_all_positions(self._cfg.symbol)
                    self._position = None
                    await asyncio.sleep(61)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Init?: %s", e)
            await asyncio.sleep(30)

    # ----------------------------------------------------------------
    # Init?
    # ----------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            "state": self._state,
            "running": self._running,
            "touch_top": self._touch_top,
            "touch_bot": self._touch_bot,
            "trend_entered": self._trend_entered,
            "position": self._position,
            "streak": f"{self._streak_wins}W/{self._streak_losses}L",
            "silence_reason": self._silence_reason,
            "last_order": self._last_order_time,
        }
