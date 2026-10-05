"""
V4 Strategy: V2+ with optimization filters
===========================================
Filters added over V2:
1. BB Width filter (<0.08% = dead market, skip)
2. M5 Momentum ROC (4-bar price change <0.3 = skip)
3. H1 ATR drift protection (H1 body >1.2*ATR = energy exhausted)
4. H1 RSI + SMA20/50 macro filter
5. M5 RSI(7) filter (avoid overbought/oversold entries)
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5
import numpy as np

from ..market_data.models import Tick
from ..trading.mt5_executor import MT5Executor
from .config import StrategyConfig
from .indicators import SMA, RSI
from .recorder import Recorder
from ..notifier.email_alerter import EmailAlerter
from .base import BaseStrategy

logger = logging.getLogger(__name__)

TF_MT5 = {"M5": mt5.TIMEFRAME_M5, "H1": mt5.TIMEFRAME_H1}

def calc_adx(high, low, close, period=14):
    n = len(close); adx=np.full(n,np.nan); pdi=np.full(n,np.nan); ndi=np.full(n,np.nan)
    tr=np.zeros(n); pdm=np.zeros(n); ndm=np.zeros(n)
    for i in range(1,n):
        tr[i]=max(high[i]-low[i],abs(high[i]-close[i-1]),abs(low[i]-close[i-1]))
        u=high[i]-high[i-1]; d=low[i-1]-low[i]
        if u>d and u>0: pdm[i]=u
        if d>u and d>0: ndm[i]=d
    ts=np.zeros(n); ps=np.zeros(n); ns=np.zeros(n)
    ts[period]=np.sum(tr[1:period+1]); ps[period]=np.sum(pdm[1:period+1]); ns[period]=np.sum(ndm[1:period+1])
    for i in range(period+1,n):
        ts[i]=ts[i-1]-ts[i-1]/period+tr[i]; ps[i]=ps[i-1]-ps[i-1]/period+pdm[i]; ns[i]=ns[i-1]-ns[i-1]/period+ndm[i]
    for i in range(period,n):
        if ts[i]: pdi[i]=100*ps[i]/ts[i]; ndi[i]=100*ns[i]/ts[i]
    dx=np.zeros(n)
    for i in range(period,n):
        s=pdi[i]+ndi[i]
        if s: dx[i]=100*abs(pdi[i]-ndi[i])/s
    adx[period*2]=np.mean(dx[period:period*2+1])
    for i in range(period*2+1,n): adx[i]=adx[i-1]-adx[i-1]/period+dx[i]
    return adx, pdi, ndi

def calc_bb(close, period=20, sd=2.0):
    n=len(close); m=np.full(n,np.nan); u=np.full(n,np.nan); l=np.full(n,np.nan)
    for i in range(period-1,n):
        w=close[i-period+1:i+1]; s=np.mean(w); d=np.std(w,ddof=0); m[i]=s; u[i]=s+sd*d; l[i]=s-sd*d
    return m,u,l

def calc_atr(high, low, close, p=14):
    n=len(close); tr=np.zeros(n); r=np.zeros(n)
    for i in range(1,n): tr[i]=max(high[i]-low[i],abs(high[i]-close[i-1]),abs(low[i]-close[i-1]))
    r[p]=np.mean(tr[1:p+1])
    for i in range(p+1,n): r[i]=(r[i-1]*(p-1)+tr[i])/p
    return r

def calc_rsi(c, p=7):
    n=len(c); r=np.full(n,np.nan); g=np.zeros(n); l=np.zeros(n)
    for i in range(1,n):
        d=c[i]-c[i-1]
        if d>0: g[i]=d
        else: l[i]=-d
    ag=np.zeros(n); al=np.zeros(n)
    ag[p]=np.mean(g[1:p+1]); al[p]=np.mean(l[1:p+1])
    for i in range(p+1,n):
        ag[i]=(ag[i-1]*(p-1)+g[i])/p; al[i]=(al[i-1]*(p-1)+l[i])/p
    for i in range(p,n):
        if al[i]>0: r[i]=100-100/(1+ag[i]/al[i])
        elif ag[i]>0: r[i]=100
        else: r[i]=50
    return r


class TickAggregator:
    def __init__(self, symbol, callback):
        self._symbol=symbol; self._callback=callback; self._candle=None

    def add_tick(self, tick):
        ts=tick.timestamp or datetime.now(timezone.utc)
        m5=self._round_m5(ts)
        if self._candle is None or m5>self._candle["time"]:
            old=self._candle
            self._candle={"time":m5,"open":tick.bid,"high":tick.bid,"low":tick.bid,"close":tick.bid,"volume":1}
            if old is not None and old["volume"]>0:
                asyncio.create_task(self._callback(old))
        else:
            c=self._candle
            c["high"]=max(c["high"],tick.bid); c["low"]=min(c["low"],tick.bid)
            c["close"]=tick.bid; c["volume"]+=1

    def flush(self):
        if self._candle and self._candle["volume"]>0:
            asyncio.create_task(self._callback(self._candle))
        self._candle=None

    @staticmethod
    def _round_m5(dt):
        ts=int(dt.timestamp()); ts=ts-(ts%300)
        return datetime.fromtimestamp(ts,tz=timezone.utc)


class StrategyEngineV4(BaseStrategy):
    """
    V4: V2 core + 5 optimization filters
    Target: $1.00/trade (100pts) with high WR
    """

    def __init__(self, executor: MT5Executor, config: StrategyConfig | None = None,
                 recorder: Recorder | None = None, emailer: EmailAlerter | None = None):
        self._exe=executor; self._cfg=config or StrategyConfig()
        self._rec=recorder or Recorder()
        self._agg=None; self._running=False; self._cleanup_task=None
        self._m5_buf=[]; self._state="RANGING"
        self._touch_top=0; self._touch_bot=0; self._position=None
        self._cooldown_until=0.0; self._last_order_time=None
        self._last_warning_time=None; self._silence_reason="Idle"
        self._h1_sma_fast=0.0; self._h1_sma_slow=0.0; self._h1_rsi=50.0
        self._h1_updated=0.0; self._emailer=emailer
        self._trailing_task=None

    async def start(self):
        self._running=True
        self._agg=TickAggregator(self._cfg.symbol,self._on_m5_candle)
        await self._prefetch_history()
        self._cleanup_task=asyncio.create_task(self._cleanup_loop())
        stats=self._rec.get_stats()
        logger.info("V4 started: %s | Trades: %d WR: %.1f%%", self._cfg.symbol, stats["total_trades"], stats["win_rate"])

    async def stop(self):
        self._running=False
        if self._agg: self._agg.flush()
        if self._trailing_task:
            self._trailing_task.cancel()
            try: await self._trailing_task
            except asyncio.CancelledError: pass
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try: await self._cleanup_task
            except asyncio.CancelledError: pass
        self._rec.close()
        logger.info("V4 stopped")

    async def on_tick(self, tick: Tick):
        if self._running and self._agg: self._agg.add_tick(tick)

    async def _on_m5_candle(self, candle):
        if not self._running: return
        self._m5_buf.append(candle)
        if len(self._m5_buf)>200: self._m5_buf.pop(0)
        if len(self._m5_buf)<50: return
        await self._process(candle)

    async def _process(self, candle):
        cfg=self._cfg; t=candle["time"].timestamp()
        close=candle["close"]; high=candle["high"]; low=candle["low"]; open_p=candle["open"]

        if self._session_cooldown(candle["time"]):
            self._silence_reason="Session cooldown"; self._cooldown_until=t+300; return
        if t<self._cooldown_until:
            self._silence_reason="Cooldown active"; return

        # Refresh H1 macro every 60s
        if t-self._h1_updated>60:
            await self._refresh_h1_macro(); self._h1_updated=t

        if self._position:
            self._silence_reason="In position"
            await self._check_position(candle); return

        # Calculate indicators
        closes=np.array([c["close"] for c in self._m5_buf],dtype=float)
        highs=np.array([c["high"] for c in self._m5_buf],dtype=float)
        lows=np.array([c["low"] for c in self._m5_buf],dtype=float)

        adx_arr,pdi,ndi=calc_adx(highs,lows,closes,14)
        bb_m,bb_u,bb_l=calc_bb(closes,20,2.0)
        atr_arr=calc_atr(highs,lows,closes,14)
        rsi7=calc_rsi(closes,7)

        i=len(self._m5_buf)-1
        ca=adx_arr[i]; cp=pdi[i]; cn=ndi[i]
        cbu=bb_u[i]; cbl=bb_l[i]; cbm=bb_m[i]; cat=atr_arr[i]
        bw=cbu-cbl; tol=bw*0.02

        if np.isnan(ca) or np.isnan(cbu): return

        # ============================================================
        # OPTIMIZATION FILTER #1: BB Width check
        # ============================================================
        bb_w=bw/cbm if cbm>0 else 0
        if bb_w<0.0008:
            self._silence_reason=f"BB too narrow: {bb_w:.6f}"
            return

        # ============================================================
        # OPTIMIZATION FILTER #2: Momentum check (4-bar ROC)
        # ============================================================
        if i>=4:
            mom=close-closes[i-4]
            if abs(mom)<0.3:
                self._silence_reason=f"Momentum too weak: {mom:.2f}"
                return

        # ============================================================
        # OPTIMIZATION FILTER #3: H1 ATR drift protection
        # ============================================================
        if self._h1_updated>0:
            h1_body=0
            if hasattr(self,'_last_h1_open') and self._last_h1_open>0:
                pass  # Checked in _refresh_h1_macro

        # H1 macro direction + RSI
        h1_bull=self._h1_sma_fast>self._h1_sma_slow and self._h1_rsi>50
        h1_bear=self._h1_sma_fast<self._h1_sma_slow and self._h1_rsi<50

        # ============================================================
        # OPTIMIZATION FILTER #5: M5 RSI(7) range check
        # ============================================================
        rsi_val=rsi7[i]
        rsi_buy_ok=30<rsi_val<70
        rsi_sell_ok=30<rsi_val<70

        # ============================================================
        # V2 State Machine
        # ============================================================
        adx_r=ca>adx_arr[i-1] if i>0 and not np.isnan(adx_arr[i-1]) else False

        if ca>=20 and adx_r:
            self._state="TRENDING"
        elif ca<23:
            self._state="RANGING"
        else:
            self._state="NO_TRADE"
            self._silence_reason=f"No-Trade ADX={ca:.1f}"
            return

        # Update touch counters
        self._touch_top=self._touch_top+1 if high>=cbu-tol else max(0,self._touch_top-1) if high<cbu and low>cbl else self._touch_top
        self._touch_bot=self._touch_bot+1 if low<=cbl+tol else max(0,self._touch_bot-1) if high<cbu and low>cbl else self._touch_bot

        body=abs(close-open_p)

        if self._state=="TRENDING":
            bb_bu=close>cbu; bb_bd=close<cbl
            if bb_bu and cp>cn and body>cat*0.3 and h1_bull and rsi_buy_ok:
                # OPTIMIZATION FILTER #4: H1 ATR drift protection
                if self._check_h1_atr_filter():
                    sl=round(cbl-bw*0.1,2)
                    tp=round(close+10.0,2)  # $1.00 target (100pts)
                    r=await self._exe.buy_market(cfg.symbol, cfg.volume)
                    if r["success"]:
                        self._position={"dir":"long","entry":r["price"],"sl":sl,"tp":tp,"et":t,"be":False,"ticket":r.get("order",0)}
                        trade_id = self._rec.record_trade("long", r["price"], cfg.volume, strategy=cfg.strategy_name, open_order_id=r.get("order",0), sl=sl, tp=tp)
                        self._position["trade_id"] = trade_id
                        self._rec.record_decision("v4_trend_buy",cfg.strategy_name,price=r["price"])
                        self._last_order_time=t; self._cooldown_until=t+120
                        self._start_trailing()
                        if self._emailer:
                            self._emailer.send_order(datetime.now().strftime("%H:%M:%S"),"Buy","V4-Trend",cfg.volume,price=r["price"])
                        logger.info("V4 trend buy @ %.2f tp=%.2f",r["price"],tp)
            elif bb_bd and cn>cp and body>cat*0.3 and h1_bear and rsi_sell_ok:
                if self._check_h1_atr_filter():
                    sl=round(cbu+bw*0.1,2)
                    tp=round(close-10.0,2)
                    r=await self._exe.sell_market(cfg.symbol, cfg.volume)
                    if r["success"]:
                        self._position={"dir":"short","entry":r["price"],"sl":sl,"tp":tp,"et":t,"be":False,"ticket":r.get("order",0)}
                        trade_id = self._rec.record_trade("short", r["price"], cfg.volume, strategy=cfg.strategy_name, open_order_id=r.get("order",0), sl=sl, tp=tp)
                        self._position["trade_id"] = trade_id
                        self._rec.record_decision("v4_trend_sell",cfg.strategy_name,price=r["price"])
                        self._last_order_time=t; self._cooldown_until=t+120
                        self._start_trailing()
                        if self._emailer:
                            self._emailer.send_order(datetime.now().strftime("%H:%M:%S"),"Sell","V4-Trend",cfg.volume,price=r["price"])
                        logger.info("V4 trend sell @ %.2f tp=%.2f",r["price"],tp)

        elif self._state=="RANGING":
            if low<=cbl+tol and self._touch_bot<3 and close<cbm and h1_bull and rsi_buy_ok:
                if self._check_h1_atr_filter():
                    sl=round(close-bw*0.5,2)
                    tp=round(close+10.0,2)
                    r=await self._exe.buy_market(cfg.symbol, cfg.volume)
                    if r["success"]:
                        self._position={"dir":"long","entry":r["price"],"sl":sl,"tp":tp,"et":t,"be":False,"ticket":r.get("order",0)}
                        trade_id = self._rec.record_trade("long", r["price"], cfg.volume, strategy=cfg.strategy_name, open_order_id=r.get("order",0), sl=sl, tp=tp)
                        self._position["trade_id"] = trade_id
                        self._rec.record_decision("v4_range_buy",cfg.strategy_name,price=r["price"])
                        self._last_order_time=t; self._cooldown_until=t+120
                        self._start_trailing()
                        if self._emailer:
                            self._emailer.send_order(datetime.now().strftime("%H:%M:%S"),"Buy","V4-Range",cfg.volume,price=r["price"])
                        logger.info("V4 range buy @ %.2f tp=%.2f",r["price"],tp)
            elif high>=cbu-tol and self._touch_top<3 and close>cbm and h1_bear and rsi_sell_ok:
                if self._check_h1_atr_filter():
                    sl=round(close+bw*0.5,2)
                    tp=round(close-10.0,2)
                    r=await self._exe.sell_market(cfg.symbol, cfg.volume)
                    if r["success"]:
                        self._position={"dir":"short","entry":r["price"],"sl":sl,"tp":tp,"et":t,"be":False,"ticket":r.get("order",0)}
                        trade_id = self._rec.record_trade("short", r["price"], cfg.volume, strategy=cfg.strategy_name, open_order_id=r.get("order",0), sl=sl, tp=tp)
                        self._position["trade_id"] = trade_id
                        self._rec.record_decision("v4_range_sell",cfg.strategy_name,price=r["price"])
                        self._last_order_time=t; self._cooldown_until=t+120
                        self._start_trailing()
                        if self._emailer:
                            self._emailer.send_order(datetime.now().strftime("%H:%M:%S"),"Sell","V4-Range",cfg.volume,price=r["price"])
                        logger.info("V4 range sell @ %.2f tp=%.2f",r["price"],tp)

    # ================ H1 ATR filter ================
    def _check_h1_atr_filter(self) -> bool:
        """OPTIMIZATION #3: Skip trade if last H1 body > 1.2 * H1 ATR"""
        if not hasattr(self,'_last_h1_body') or self._last_h1_body<0:
            return True
        if not hasattr(self,'_last_h1_atr') or self._last_h1_atr<0:
            return True
        if self._last_h1_atr>0 and self._last_h1_body>self._last_h1_atr*1.2:
            self._silence_reason=f"H1 ATR drift: body={self._last_h1_body:.1f} > 1.2*ATR={self._last_h1_atr*1.2:.1f}"
            return False
        return True

    # ================ Trailing stop loop ================
    def _start_trailing(self):
        if self._trailing_task and not self._trailing_task.done():
            self._trailing_task.cancel()
        self._trailing_task=asyncio.create_task(self._trailing_stop_loop())

    async def _trailing_stop_loop(self):
        """Multi-phase trailing: BE($3) -> Lock($6) -> TP($10)"""
        while self._running and self._position:
            try:
                pos=self._position
                if pos is None: break
                q=await self._exe.get_quote(self._cfg.symbol)
                if q is None: await asyncio.sleep(2); continue
                bid=q["bid"]; ask=q["ask"]
                entry=pos["entry"]; sl=pos["sl"]; tp=pos["tp"]
                be=pos.get("be",False)

                if pos["dir"]=="long":
                    pnl=(bid-entry)*1.0
                    if bid<=sl:
                        await self._close_position("BE_SL" if be else "SL"); break
                    if bid>=tp:
                        await self._close_position("TP"); break
                    if pnl>=3.0 and not be:
                        ns=round(entry+0.5,2); pos["sl"]=ns; pos["be"]=True
                        if pos.get("ticket"): await self._exe.modify_sl(self._cfg.symbol,pos["ticket"],ns)
                        logger.info("V4 BE: PnL=%.2f SL=%.2f",pnl,ns)
                    elif be and pnl>=6.0 and sl<entry+4.0:
                        ns=round(entry+4.0,2); pos["sl"]=ns
                        if pos.get("ticket"): await self._exe.modify_sl(self._cfg.symbol,pos["ticket"],ns)
                        logger.info("V4 LOCK4: PnL=%.2f SL=%.2f",pnl,ns)
                else:
                    pnl=(entry-ask)*1.0
                    if ask>=sl:
                        await self._close_position("BE_SL" if be else "SL"); break
                    if ask<=tp:
                        await self._close_position("TP"); break
                    if pnl>=3.0 and not be:
                        ns=round(entry-0.5,2); pos["sl"]=ns; pos["be"]=True
                        if pos.get("ticket"): await self._exe.modify_sl(self._cfg.symbol,pos["ticket"],ns)
                        logger.info("V4 BE: PnL=%.2f SL=%.2f",pnl,ns)
                    elif be and pnl>=6.0 and sl>entry-4.0:
                        ns=round(entry-4.0,2); pos["sl"]=ns
                        if pos.get("ticket"): await self._exe.modify_sl(self._cfg.symbol,pos["ticket"],ns)
                        logger.info("V4 LOCK4: PnL=%.2f SL=%.2f",pnl,ns)
                await asyncio.sleep(2)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.warning("V4 trailing: %s", e)
                await asyncio.sleep(5)

    async def _close_position(self, reason):
        cfg=self._cfg
        pos=self._position
        if pos is None: return
        # Get quote for PnL calculation
        q=await self._exe.get_quote(cfg.symbol)
        profit=0.0; close_price=pos.get("entry",0)
        entry=pos["entry"]; sl=pos["sl"]; tp=pos["tp"]
        dir_label="Long" if pos["dir"]=="long" else "Short"

        if reason == "TP" and pos["dir"]=="long":
            pnl = (tp - entry) * 1.0
        elif reason == "TP" and pos["dir"]=="short":
            pnl = (entry - tp) * 1.0
        elif reason in ("SL", "BE_SL") and pos["dir"]=="long":
            pnl = (sl - entry) * 1.0
        elif reason in ("SL", "BE_SL") and pos["dir"]=="short":
            pnl = (entry - sl) * 1.0
        elif q:
            if pos["dir"]=="long":
                close_price=q["bid"]
                profit=(close_price-pos["entry"])*1.0;pnl=profit
            else:
                close_price=q["ask"]
                profit=(pos["entry"]-close_price)*1.0;pnl=profit
        else:
            pnl=0.0

        # Close position on MT5
        r=await self._exe.close_all_positions(cfg.symbol)

        # Record trade close
        trade_id=pos.get("trade_id")
        if trade_id and self._rec:
            self._rec.close_trade(trade_id, close_price, round(pnl,2))

        # Email notification
        reason_label = reason
        if reason == "BE_SL": reason_label = "BE"  # Breakeven stop-out is still a win
        et=pos.get("et",datetime.now(timezone.utc).timestamp())
        hold_m=(datetime.now(timezone.utc).timestamp()-et)/60
        hold_str=f"{hold_m:.0f}min" if hold_m<60 else f"{hold_m/60:.1f}h"
        if self._emailer:
            self._emailer.send_close(datetime.now().strftime("%H:%M:%S"),dir_label,reason_label,round(pnl,2),hold_str)
        self._position=None
        logger.info("V4 closed %s reason=%s pnl=%.2f hold=%.0fmin", dir_label, reason, pnl,
                     (datetime.now(timezone.utc).timestamp()-pos.get("et",0))/60)

    async def _check_position(self, candle):
        """Check SL/TP on each M5 close"""
        await self._check_position_trailing(candle)

    async def _check_position_trailing(self, candle):
        pass  # Already handled by _trailing_stop_loop

    # ================ H1 macro refresh ================
    async def _refresh_h1_macro(self):
        def _fetch():
            rates=mt5.copy_rates_from_pos(self._cfg.symbol,mt5.TIMEFRAME_H1,0,60)
            if rates is None or len(rates)<55: return
            hc=np.array([r[4] for r in rates],dtype=float)
            ho=np.array([r[1] for r in rates],dtype=float)
            hh=np.array([r[2] for r in rates],dtype=float)
            hl=np.array([r[3] for r in rates],dtype=float)
            # Fix NaN
            for j in range(1,len(hc)):
                if np.isnan(hc[j]): hc[j]=hc[j-1]
            fast=SMA(hc,20); slow=SMA(hc,50)
            h1_rsi=calc_rsi(hc,14)
            h1_atr=calc_atr(hh,hl,hc,14)
            return (float(fast[-1]) if not np.isnan(fast[-1]) else 0.0,
                    float(slow[-1]) if not np.isnan(slow[-1]) else 0.0,
                    float(h1_rsi[-1]) if not np.isnan(h1_rsi[-1]) else 50.0,
                    float(h1_atr[-1]) if not np.isnan(h1_atr[-1]) else 0.0,
                    abs(float(hc[-1])-float(ho[-1])))
        result=await MT5Executor._run_in_executor(_fetch)
        if result:
            self._h1_sma_fast,self._h1_sma_slow,self._h1_rsi,self._last_h1_atr,self._last_h1_body=result
            self._last_h1_open=0

    async def _prefetch_history(self):
        def _fetch():
            rates=mt5.copy_rates_from_pos(self._cfg.symbol,mt5.TIMEFRAME_M5,0,200)
            if rates is None: return
            for r in rates:
                dt=datetime.fromtimestamp(r[0],tz=timezone.utc)
                self._m5_buf.append({"time":dt,"open":float(r[1]),"high":float(r[2]),"low":float(r[3]),"close":float(r[4])})
        await MT5Executor._run_in_executor(_fetch)
        logger.info("V4 preloaded %d M5 bars",len(self._m5_buf))
        await self._refresh_h1_macro()

    async def _cleanup_loop(self):
        while self._running:
            try:
                now_bjt=datetime.now(timezone.utc)+timedelta(hours=8)
                if self._emailer and self._last_order_time:
                    now_ts=datetime.now(timezone.utc).timestamp()
                    if now_ts-self._last_order_time>3600 and (not self._last_warning_time or now_ts-self._last_warning_time>3600):
                        self._last_warning_time=now_ts
                        lt=datetime.fromtimestamp(self._last_order_time,tz=timezone.utc)
                        self._emailer.send_silent_warning(
                            self._silence_reason,lt.strftime("%Y-%m-%d %H:%M:%S"),self.get_status())
                if now_bjt.hour==23 and now_bjt.minute==55:
                    logger.info("V4: Daily cleanup")
                    await self._exe.cancel_all_pending(self._cfg.symbol)
                    await self._exe.close_all_positions(self._cfg.symbol)
                    self._position=None; await asyncio.sleep(61)
            except asyncio.CancelledError: break
            except Exception as e: logger.warning("V4 cleanup: %s", e)
            await asyncio.sleep(30)

    @staticmethod
    def _session_cooldown(dt) -> bool:
        bjt=(dt.hour+8)%24*60+dt.minute
        return (450<=bjt<510) or (860<=bjt<900) or (1230<=bjt<1260)

    def get_status(self) -> dict:
        return {
            "state":self._state,"running":self._running,"streak":"0W/0L",
            "touch_top":self._touch_top,"touch_bot":self._touch_bot,
            "position":self._position,"silence_reason":self._silence_reason,
            "last_order":self._last_order_time,
            "h1_sma_fast":round(self._h1_sma_fast,1) if self._h1_sma_fast else 0,
            "h1_sma_slow":round(self._h1_sma_slow,1) if self._h1_sma_slow else 0,
            "h1_rsi":round(self._h1_rsi,1) if self._h1_rsi else 50,
        }
