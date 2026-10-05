"""B2: Real MT5 Trading Engine - EMA Trend + RSI + 4-Stage Ratchet"""
import asyncio, logging, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import MetaTrader5 as mt5
import numpy as np
from ..market_data.models import Tick
from ..trading.mt5_executor import MT5Executor
from .config import StrategyConfig
from .recorder import Recorder
from ..notifier.email_alerter import EmailAlerter
from .base import BaseStrategy

logger = logging.getLogger(__name__)

# Constants
SPREAD = 0.34         # Spread cost in USD

DAILY_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "b2_daily_state.json"

def ema(a, p=9):
    n = len(a); r = np.full(n, np.nan); m = 2 / (p + 1)
    r[p-1] = np.mean(a[:p])
    for i in range(p, n):
        r[i] = (a[i] - r[i-1]) * m + r[i-1]
    return r

def e21(a): return ema(a, 21)
def e50(a): return ema(a, 50)

def rsi(a, p=14):
    n = len(a); r = np.full(n, np.nan)
    for i in range(1, n):
        u = sum(a[j+1] - a[j] for j in range(max(0, i-p), i) if a[j+1] > a[j])
        d = sum(a[j] - a[j+1] for j in range(max(0, i-p), i) if a[j+1] <= a[j])
        r[i] = 50 if u + d == 0 else 100 - 100 / (1 + u / (d + 1e-10))
    return r

def atr(h, l, c, p=14):
    n = len(c); tr = np.zeros(n); ra = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    ra[p] = np.mean(tr[1:p+1])
    for i in range(p+1, n):
        ra[i] = (ra[i-1] * (p - 1) + tr[i]) / p
    return ra

def bb(c):
    n = len(c); m = np.full(n, np.nan); u = np.full(n, np.nan); l = np.full(n, np.nan)
    for i in range(19, n):
        w = c[i-19:i+1]; av = np.mean(w); sd = np.std(w, ddof=0)
        m[i] = av; u[i] = av + 2 * sd; l[i] = av - 2 * sd
    return m, u, l


class TickAggregator:
    """Aggregate ticks to M15 candles."""
    def __init__(self, cb):
        self._cb = cb
        self._c = None

    def add_tick(self, t):
        ts = t.timestamp or datetime.now(timezone.utc)
        m5 = self._round_m15(ts)
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
    def _round_m15(d):
        ts = int(d.timestamp())
        ts = ts - (ts % 900)
        return datetime.fromtimestamp(ts, tz=timezone.utc)


class StrategyEngineB2(BaseStrategy):
    """B2: Real MT5 Trading - EMA8/21/50 + RSI + BB signal engine."""

    def __init__(self, executor, config=None, recorder=None, emailer=None):
        self._exe = executor
        self._cfg = config or StrategyConfig()
        self._rec = recorder or Recorder()
        self._emailer = emailer
        self._running = False
        self._agg = None
        self._buf = []         # M15 candle buffer
        self._tt = None        # trailing stop task
        self._ct = None        # cleanup routine task
        
        # State
        self._trade_count = 0  # today
        self._daily_loss = 0.0
        self._daily_pnl = 0.0
        self._consec_loss = 0
        self._consec_bar = 0
        self._last_day = -1
        
        # Position tracking
        self._pos_ticket = None  # real MT5 position ticket
        self._last_order_time = None  # timestamp of last order
        self._last_warning_time = None
        self._silence_reason = "Init"
        self._active_trade_id = None  # recorder trade id for open position
        self._active_decision_id = None
        
        # SL tracking
        self._pos_sl_stage = 0  # 0=initial, 1=break_even, 2=profit_lock

    async def start(self):
        self._running = True
        self._agg = TickAggregator(self._on_candle)
        self._load_daily_state()
        await self._preload()
        self._ct = asyncio.create_task(self._cleanup_routine())
        self._tt = asyncio.create_task(self._trailing_loop())
        logger.info("B2 started: %s vol=%.2f trades_today=%d", self._cfg.symbol, self._cfg.volume, self._trade_count)

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

    async def on_tick(self, tick):
        if self._running and self._agg:
            self._agg.add_tick(tick)

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
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = DAILY_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(DAILY_STATE_FILE)
        except Exception as e:
            logger.warning("B2 save daily state failed: %s", e)

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
                self._last_day = today
                logger.info("B2 restored daily state: trades=%d pnl=%.2f", self._trade_count, self._daily_pnl)
            else:
                logger.info("B2 daily state from different day, starting fresh")
        except Exception as e:
            logger.warning("B2 load daily state failed: %s", e)

    # ------------------------------------------------------------------
    # Preload historical M15 bars
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
        logger.info("B2 preloaded %d M15 bars", len(self._buf))

    # ------------------------------------------------------------------
    # M15 Candle handler — signal generation + real MT5 orders
    # ------------------------------------------------------------------
    async def _on_candle(self, c):
        if not self._running:
            return
        self._buf.append(c)
        if len(self._buf) > 200:
            self._buf.pop(0)
        if len(self._buf) < 60:
            return  # need enough data for indicators
        await self._process_candle(c)

    async def _process_candle(self, c):
        t = c["t"].timestamp()
        cl = c["c"]; hi = c["h"]; lo = c["l"]
        closes = np.array([x["c"] for x in self._buf])
        highs = np.array([x["h"] for x in self._buf])
        lows = np.array([x["l"] for x in self._buf])
        
        e8v = ema(closes)
        e21v = e21(closes)
        e50v = e50(closes)
        rs = rsi(closes)
        at = atr(highs, lows, closes)
        bm, bu, bl = bb(closes)
        
        i = len(self._buf) - 1
        if np.isnan(e21v[i]) or np.isnan(rs[i]):
            return

        # Daily reset
        dy = int(t / 86400)
        if dy != self._last_day:
            self._trade_count = 0
            self._daily_loss = 0.0
            self._daily_pnl = 0.0
            self._consec_loss = 0
            self._consec_bar = 0
            self._last_day = dy
            self._save_daily_state()

        # Daily profit target hit → skip
        if self._daily_pnl >= 10:
            self._silence_reason = "Target+10 reached"
            return

        hh = int((datetime.fromtimestamp(t) + timedelta(hours=8)).hour % 24)
        hk = self._cfg.session_start <= hh < self._cfg.session_end
        as_ = 0 <= hh < 6

        # Check if we already have a real MT5 position
        has_position = await self._check_position()

        if has_position:
            # Position management is done by trailing_loop + MT5 SL/TP
            pass
        else:
            self._pos_ticket = None
            
            # Risk limits
            if self._trade_count >= self._cfg.max_trades or self._daily_loss >= self._cfg.max_loss:
                self._silence_reason = f"Limit: {self._trade_count}T/{round(self._daily_loss,1)}L"
                return
            if self._consec_loss >= 2 and i - self._consec_bar < 2:
                return  # cool down after consecutive losses

            # --- Signal conditions ---
            mu = cl > closes[i-1] and closes[i-1] > closes[i-2]
            md = cl < closes[i-1] and closes[i-1] < closes[i-2]
            eb = e8v[i] > e21v[i] and e21v[i] > e50v[i]
            er = e8v[i] < e21v[i] and e21v[i] < e50v[i]
            nr2 = abs(cl - e21v[i]) < at[i] * 0.8
            
            h96 = np.max(highs[max(0, i-95):i+1]) if i >= 96 else 1e9
            l96 = np.min(lows[max(0, i-95):i+1]) if i >= 96 else 0

            la = mu and eb and 25 < rs[i] < 80 and nr2 and hk
            lb = as_ and cl - SPREAD / 2 < bl[i] and rs[i] < 30
            lc = i >= 96 and closes[i-1] < l96 and cl > l96
            sa = md and er and 20 < rs[i] < 75 and nr2 and hk
            sb = as_ and cl + SPREAD / 2 > bu[i] and rs[i] > 70
            sc = i >= 96 and closes[i-1] > h96 and cl < h96

            # --- Execute real MT5 order on signal ---
            if la or lb or lc:
                await self._open_order("buy", cl)
            elif sa or sb or sc:
                await self._open_order("sell", cl)

    async def _check_position(self):
        """Check if our tracked position is still open on MT5."""
        if not self._pos_ticket:
            return False
        try:
            pos = await self._exe._run_in_executor(
                lambda: mt5.positions_get(ticket=self._pos_ticket)
            )
            if pos and len(pos) > 0:
                return True
            else:
                # Position was closed — send close notification
                await self._handle_close()
                self._pos_ticket = None
                self._pos_sl_stage = 0
                return False
        except Exception:
            return False

    async def _open_order(self, direction, price):
        """Open real MT5 order and send email notification."""
        vol = self._cfg.volume
        if direction == "buy":
            sl = round(price * (1 - self._cfg.sl_pct), 2)
            tp = round(price * (1 + self._cfg.tp_pct), 2)
            result = await self._exe.buy_market(self._cfg.symbol, vol, sl=sl, tp=tp)
        else:
            sl = round(price * (1 + self._cfg.sl_pct), 2)
            tp = round(price * (1 - self._cfg.tp_pct), 2)
            result = await self._exe.sell_market(self._cfg.symbol, vol, sl=sl, tp=tp)

        if result.get("success"):
            ticket = result.get("order", 0)
            self._pos_ticket = ticket
            self._trade_count += 1
            self._pos_sl_stage = 0
            self._last_order_time = datetime.now(timezone.utc).timestamp()
            self._silence_reason = ""

            # Record decision
            decision_id = self._rec.record_decision(
                direction=direction.upper(),
                strategy="B2",
                price=price,
                volume=vol,
                order_id=ticket,
                reason=f"SL={sl} TP={tp}",
            )
            # Record trade
            self._active_trade_id = self._rec.record_trade(
                direction=direction.upper(),
                open_price=result.get("price", price),
                volume=vol,
                strategy="B2",
                open_order_id=ticket,
                decision_id=decision_id,
                sl=sl,
                tp=tp,
            )
            self._active_decision_id = decision_id
            self._save_daily_state()

            # Send order email
            if self._emailer:
                now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
                self._emailer.send_order(
                    time_str=now_str,
                    direction=direction.upper(),
                    strategy="B2",
                    volume=vol,
                    price=price,
                )
            logger.info("B2 ORDER %s @ %.2f SL=%.2f TP=%.2f ticket=%d",
                        direction.upper(), price, sl, tp, ticket)
        else:
            logger.warning("B2 order failed: %s", result.get("error", "unknown"))

    async def _handle_close(self):
        """Called when position is detected as closed. Record and notify."""
        if not self._pos_ticket:
            return

        pos = await self._exe._run_in_executor(
            lambda: mt5.history_deals_get(
                position=self._pos_ticket,
                from_position=0,
                to_position=0
            )
        )

        # Get PnL from the deal
        pnl = 0.0
        close_price = 0.0
        reason = "Manual"
        hold_min = 0
        is_manual = True
        if pos and len(pos) > 0:
            deals = list(pos)
            total_pnl = sum(d.profit for d in deals)
            pnl = round(total_pnl, 2)
            close_price = deals[-1].price if hasattr(deals[-1], 'price') else 0.0
            # Check deal reason: DEAL_REASON_TP=2, DEAL_REASON_SL=1, others=manual
            last_deal = deals[-1]
            if hasattr(last_deal, 'reason'):
                dr = last_deal.reason
                if dr == 2:
                    reason = "TP"; is_manual = False
                elif dr == 1:
                    reason = "SL"; is_manual = False
                elif dr == 6:  # DEAL_REASON_CLIENT
                    reason = "Manual"; is_manual = True
                else:
                    reason = "TP" if pnl > 0 else "SL"; is_manual = False
            else:
                reason = "TP" if pnl > 0 else "SL"; is_manual = False
            if len(deals) >= 1:
                ot = deals[0].time if hasattr(deals[0], 'time') else 0
                ct = deals[-1].time if hasattr(deals[-1], 'time') else 0
                hold_min = round((ct - ot) / 60, 1) if ct and ot else 0

        # Fallback: if history_deals_get failed, use current market price
        if close_price == 0.0:
            try:
                tick = await self._exe._run_in_executor(
                    lambda: mt5.symbol_info_tick(self._cfg.symbol)
                )
                if tick:
                    close_price = tick.bid if hasattr(self, '_last_dir') and self._last_dir == "buy" else tick.ask
                    close_price = round(close_price, 2)
            except Exception:
                pass

        self._daily_pnl += pnl
        if pnl < 0:
            self._daily_loss += abs(pnl)
            self._consec_loss += 1
            self._consec_bar = len(self._buf)
        else:
            self._consec_loss = 0

        # Close trade in recorder — always record even if close_price is approximate
        if self._active_trade_id and close_price:
            self._rec.close_trade(self._active_trade_id, close_price, pnl)

        self._active_trade_id = None
        self._active_decision_id = None
        self._save_daily_state()

        now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
        hold_str = f"{hold_min}m" if hold_min > 0 else ""

        if self._emailer:
            self._emailer.send_close(
                time_str=now_str,
                direction="LONG" if pnl >= 0 else "SHORT",
                reason=reason,
                pnl=pnl,
                hold=hold_str,
            )
        logger.info("B2 CLOSE ticket=%d pnl=%.2f reason=%s",
                    self._pos_ticket, pnl, reason)

    # ------------------------------------------------------------------
    # Trailing stop loop — monitor and adjust SL on real position
    # ------------------------------------------------------------------
    async def _trailing_loop(self):
        while self._running:
            try:
                await asyncio.sleep(2)
                if not self._pos_ticket:
                    continue
                
                # Get current position state
                pos = await self._exe._run_in_executor(
                    lambda: mt5.positions_get(ticket=self._pos_ticket)
                )
                if not pos or len(pos) == 0:
                    # Position closed externally
                    self._pos_ticket = None
                    self._pos_sl_stage = 0
                    continue
                
                p = pos[0]
                entry = p.price_open
                current = p.price_current
                profit = p.profit
                direction = "buy" if p.type == mt5.ORDER_TYPE_BUY else "sell"
                
                # Profit-based SL adjustment
                if profit >= 10 and self._pos_sl_stage < 1:
                    # Break-even: move SL to entry + small offset
                    offset = entry * 0.0005  # ~0.5 USD
                    new_sl = round(entry + offset, 2) if direction == "buy" else round(entry - offset, 2)
                    await self._exe.modify_sl(self._cfg.symbol, self._pos_ticket, new_sl)
                    self._pos_sl_stage = 1
                    logger.info("B2 BE triggered ticket=%d new_sl=%.2f", self._pos_ticket, new_sl)
                
                elif profit >= 12 and self._pos_sl_stage < 2:
                    # Profit lock: move SL to lock 10 USD profit
                    lock_pips = 1000  # 10 USD ≈ 100 points for 0.01 lot
                    if direction == "buy":
                        new_sl = round(entry + lock_pips * 0.01, 2)  # ~10 USD
                    else:
                        new_sl = round(entry - lock_pips * 0.01, 2)
                    await self._exe.modify_sl(self._cfg.symbol, self._pos_ticket, new_sl)
                    self._pos_sl_stage = 2
                    logger.info("B2 PL triggered ticket=%d new_sl=%.2f", self._pos_ticket, new_sl)
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("B2 trailing: %s", e)

    # ------------------------------------------------------------------
    # Cleanup routine — daily rollover + silence warning
    # ------------------------------------------------------------------
    async def _cleanup_routine(self):
        while self._running:
            try:
                await asyncio.sleep(30)
                
                # Daily cleanup at 23:55 Beijing time
                now = datetime.now(timezone.utc) + timedelta(hours=8)
                if now.hour == 23 and now.minute >= 55:
                    if self._pos_ticket:
                        await self._exe.close_all_positions(self._cfg.symbol)
                        self._pos_ticket = None
                    self._trade_count = 0
                    self._daily_pnl = 0
                    await asyncio.sleep(61)
                
                # Silence warning: 1 hour without any action
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
                logger.warning("B2 cleanup: %s", e)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def get_status(self):
        return {
            "state": "RUNNING",
            "running": self._running,
            "position": self._pos_ticket,
            "streak": "0W/0L",
            "silence_reason": self._silence_reason or "",
            "trade_count": self._trade_count,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_loss": round(self._daily_loss, 2),
        }
