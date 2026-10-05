"""B1: BionicBrain V5 - EMA Trend + RSI + 4-Stage Ratchet"""
import asyncio, logging
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import numpy as np
from ..market_data.models import Tick
from ..trading.mt5_executor import MT5Executor
from .config import StrategyConfig
from .recorder import Recorder
from ..notifier.email_alerter import EmailAlerter
from .base import BaseStrategy
logger = logging.getLogger(__name__)
SPREAD=0.34;SLP=0.0065;TPP=0.0115;MAXD=3;MAXL=30.0;SESS_S=7;SESS_E=20
"""B1: BionicBrain V5 - EMA Trend + RSI + 4-Stage Ratchet"""
import asyncio, logging
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import numpy as np
from ..market_data.models import Tick
from ..trading.mt5_executor import MT5Executor
from .config import StrategyConfig
from .recorder import Recorder
from ..notifier.email_alerter import EmailAlerter
from .base import BaseStrategy
logger = logging.getLogger(__name__)
SP=0.34;SLP=0.0065;TPP=0.0115;MX=3;ML=30;SH=7;EH=20

def ema(a):
 n=len(a);r=np.full(n,np.nan);m=2/9;r[7]=np.mean(a[:8])
 for i in range(8,n):r[i]=(a[i]-r[i-1])*m+r[i-1]
 return r

def e21(a):
 n=len(a);r=np.full(n,np.nan);m=2/22;r[20]=np.mean(a[:21])
 for i in range(21,n):r[i]=(a[i]-r[i-1])*m+r[i-1]
 return r

def e50(a):
 n=len(a);r=np.full(n,np.nan);m=2/51;r[49]=np.mean(a[:50])
 for i in range(50,n):r[i]=(a[i]-r[i-1])*m+r[i-1]
 return r

def rsi(a,p=14):
 n=len(a);r=np.full(n,np.nan)
 for i in range(1,n):
  u=sum(a[j+1]-a[j]for j in range(max(0,i-p),i)if a[j+1]>a[j])
  d=sum(a[j]-a[j+1]for j in range(max(0,i-p),i)if a[j+1]<=a[j])
  r[i]=50 if u+d==0 else 100-100/(1+u/(d+1e-10))
 return r

def atr(h,l,c,p=14):
 n=len(c);tr=np.zeros(n);ra=np.zeros(n)
 for i in range(1,n):tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
 ra[p]=np.mean(tr[1:p+1])
 for i in range(p+1,n):ra[i]=(ra[i-1]*(p-1)+tr[i])/p
 return ra

def bb(c):
 n=len(c);m=np.full(n,np.nan);u=np.full(n,np.nan);l=np.full(n,np.nan)
 for i in range(19,n):w=c[i-19:i+1];av=np.mean(w);sd=np.std(w,ddof=0);m[i]=av;u[i]=av+2*sd;l[i]=av-2*sd
 return m,u,l

class TickAggregator:
 def __init__(self,s,cb):
  self._cb=cb;self._c=None
 def add_tick(self,t):
  ts=t.timestamp or datetime.now(timezone.utc);m5=self._r5(ts)
  if self._c is None or m5>self._c["t"]:
   o=self._c
   self._c={"t":m5,"o":t.bid,"h":t.bid,"l":t.bid,"c":t.bid,"v":1}
   if o and o["v"]>0:asyncio.create_task(self._cb(o))
  else:
   c=self._c;c["h"]=max(c["h"],t.bid);c["l"]=min(c["l"],t.bid);c["c"]=t.bid;c["v"]+=1
 def flush(self):
  if self._c and self._c["v"]>0:asyncio.create_task(self._cb(self._c))
  self._c=None
 @staticmethod
 def _r5(d):
  ts=int(d.timestamp());ts=ts-(ts%900)
  return datetime.fromtimestamp(ts,tz=timezone.utc)

class StrategyEngineB1(BaseStrategy):
 def __init__(self,executor,config=None,recorder=None,emailer=None):
  self._exe=executor;self._cfg=config or StrategyConfig()
  self._rec=recorder or Recorder();self._emailer=emailer
  self._r=False;self._agg=None;self._buf=[];self._ct=None;self._tt=None
  self._po=None;self._dn=0;self._dl=0;self._dp=0;self._cl=0;self._cb=0;self._ld=-1
  self._lo=None;self._lw=None;self._sr="Init"
 async def start(self):
  self._r=True;self._agg=TickAggregator(self._cfg.symbol,self._on_c)
  await self._pf();self._ct=asyncio.create_task(self._cleanup_routine())
  self._tt=asyncio.create_task(self._pl())
  logger.info("B1 started: %s vol=%.2f",self._cfg.symbol,self._cfg.volume)
 async def stop(self):
  self._r=False
  if self._agg:self._agg.flush()
  for t in[self._tt,self._ct]:
   if t:t.cancel();await asyncio.sleep(0)
  self._rec.close()
 async def on_tick(self,tick):
  if self._r and self._agg:self._agg.add_tick(tick)
 async def _on_c(self,c):
  if not self._r:return
  self._buf.append(c)
  if len(self._buf)>200:self._buf.pop(0)
  if len(self._buf)<60:return
  await self._pc(c)
 async def _pc(self,c):
  t=c["t"].timestamp();cl=c["c"];hi=c["h"];lo=c["l"]
  cls=np.array([x["c"]for x in self._buf]);his=np.array([x["h"]for x in self._buf]);los=np.array([x["l"]for x in self._buf])
  e8=ema(cls);e21v=e21(cls);e50v=e50(cls);rs=rsi(cls);at=atr(his,los,cls);bm,bu,bl=bb(cls)
  i=len(self._buf)-1
  if np.isnan(e21v[i])or np.isnan(rs[i]):return
  hh=(datetime.fromtimestamp(t).hour+8)%24;dy=int(t/86400)
  if dy!=self._ld:self._dn=0;self._dl=0;self._dp=0;self._cl=0;self._ld=dy
  if self._dp>=10:return
  if self._po:
   en=self._po["e"];sl=self._po["s"];tp=self._po["t"];dr=self._po["d"];rb=self._po.get("rb",{});cf=False
   if dr=="l":
    if lo<=sl:vn=sl-en-SP;self._dp+=vn;self._dl+=abs(vn);self._cl+=1;self._cb=i;cf=True
    elif hi>=tp:vn=tp-en-SP;self._dp+=vn;self._cl=0;cf=True
    elif self._po:
     nr=rb.get("n",0);ms=sl
     if cl-en>=en*0.02 and nr<1:ms=en*1.005;rb["n"]=1
     if cl-en>=en*0.05 and nr<2:ms=en*1.03;rb["n"]=2
     if cl-en>=en*0.08 and nr<3:ms=en*1.06;rb["n"]=3
     if cl-en>en*0.08:ms=max(ms,e8[i]-at[i]*0.3)
     if ms>sl:sl=round(ms,2);self._po["s"]=sl
   else:
    if hi>=sl:vn=en-sl-SP;self._dp+=vn;self._dl+=abs(vn);self._cl+=1;self._cb=i;cf=True
    elif lo<=tp:vn=en-tp-SP;self._dp+=vn;self._cl=0;cf=True
    elif self._po:
     nr=rb.get("n",0);ms=sl
     if en-cl>=en*0.02 and nr<1:ms=en*0.995;rb["n"]=1
     if en-cl>=en*0.05 and nr<2:ms=en*0.97;rb["n"]=2
     if en-cl>=en*0.08 and nr<3:ms=en*0.94;rb["n"]=3
     if en-cl>en*0.08:ms=min(ms,e8[i]+at[i]*0.3)
     if ms<sl:sl=round(ms,2);self._po["s"]=sl
   if cf:self._po=None;self._lo=t
  else:
   if self._po:return
   if self._dn>=MX or self._dl>=ML:return
   if self._cl>=2 and i-self._cb<2:return
   mu=cl>cls[i-1]and cls[i-1]>cls[i-2];md=cl<cls[i-1]and cls[i-1]<cls[i-2]
   eb=e8[i]>e21v[i]and e21v[i]>e50v[i];er=e8[i]<e21v[i]and e21v[i]<e50v[i]
   nr2=abs(cl-e21v[i])<at[i]*0.8;hk=hh>=SH and hh<EH;as_=hh>=0 and hh<6
   h96=np.max(his[max(0,i-95):i+1])if i>=96 else 1e9
   l96=np.min(los[max(0,i-95):i+1])if i>=96 else 0
   la=mu and eb and 25<rs[i]<80 and nr2 and hk
   lb=as_ and cl-SP/2<bl[i]and rs[i]<30
   lc=i>=96 and cls[i-1]<l96 and cl>l96
   sa=md and er and 20<rs[i]<75 and nr2 and hk
   sb=as_ and cl+SP/2>bu[i]and rs[i]>70
   sc=i>=96 and cls[i-1]>h96 and cl<h96
   if la or lb or lc:
    sl=round(cl*(1-SLP),2);tp=round(cl*(1+TPP),2)
    self._po={"d":"l","e":cl,"s":sl,"t":tp,"rb":{"n":0}};self._dn+=1
   elif sa or sb or sc:
    sl=round(cl*(1+SLP),2);tp=round(cl*(1-TPP),2)
    self._po={"d":"s","e":cl,"s":sl,"t":tp,"rb":{"n":0}};self._dn+=1
 async def _pl(self):
  while self._r:
   try:await asyncio.sleep(2)
   except asyncio.CancelledError:break
   except Exception as e:logger.warning("B1:%s",e)
 async def _pf(self):
  def f():
   r15=mt5.copy_rates_from_pos(self._cfg.symbol,mt5.TIMEFRAME_M15,0,200)
   if r15 is None:return
   for r in r15:
    dt=datetime.fromtimestamp(r[0],tz=timezone.utc)
    self._buf.append({"t":dt,"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4])})
  await MT5Executor._run_in_executor(f)
  logger.info("B1 preloaded %d M15 bars",len(self._buf))
 async def _cleanup_routine(self):
  while self._r:
   try:
    now=datetime.now(timezone.utc)+timedelta(hours=8)
    if now.hour==23 and now.minute>=55:
     if self._po:self._po=None
     await asyncio.sleep(61)
    if self._emailer and self._lo:
     nts=datetime.now(timezone.utc).timestamp()
     if nts-self._lo>3600 and(not self._lw or nts-self._lw>3600):
      self._lw=nts;lt=datetime.fromtimestamp(self._lo,tz=timezone.utc)
      self._emailer.send_silent_warning(self._sr,lt.strftime("%H:%M"),self.get_status())
    await asyncio.sleep(30)
   except asyncio.CancelledError:break
   except Exception as e:logger.warning("B1 clear:%s",e)
 def get_status(self):
  return{"state":"B1","running":self._r,"position":self._po,"streak":"0W/0L","silence_reason":self._sr,"dn":self._dn,"dp":round(self._dp,2),"dl":round(self._dl,2)}
