"""V3 Final: EMASlope TREND-only + H1 ADX + ATR-adaptive thresholds"""
import MetaTrader5 as mt5; import numpy as np
mt5.initialize(); print("MT5: {}".format(mt5.version()))
PH1=mt5.TIMEFRAME_H1

def ema(a,p):
 r=np.full(len(a),np.nan,dtype=np.float64);m=2.0/(p+1);r[p-1]=np.mean(a[:p])
 for i in range(p,len(a)):r[i]=(a[i]-r[i-1])*m+r[i-1];return r

def calc_adx(h,l,c,p=14):
 n=len(c);a=np.full(n,np.nan);pd=np.full(n,np.nan);nd=np.full(n,np.nan)
 tr=np.zeros(n);pm=np.zeros(n);nm=np.zeros(n)
 for i in range(1,n):
  tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
  u=h[i]-h[i-1];d2=l[i-1]-l[i]
  if u>d2 and u>0:pm[i]=u
  if d2>u and d2>0:nm[i]=d2
 ts=np.zeros(n);ps=np.zeros(n);ns=np.zeros(n)
 ts[p]=np.sum(tr[1:p+1]);ps[p]=np.sum(pm[1:p+1]);ns[p]=np.sum(nm[1:p+1])
 for i in range(p+1,n):
  ts[i]=ts[i-1]-ts[i-1]/p+tr[i];ps[i]=ps[i-1]-ps[i-1]/p+pm[i];ns[i]=ns[i-1]-ns[i-1]/p+nm[i]
 for i in range(p,n):
  if ts[i]:pd[i]=100*ps[i]/ts[i];nd[i]=100*ns[i]/ts[i]
 dx=np.zeros(n)
 for i in range(p,n):
  s=pd[i]+nd[i]
  if s:dx[i]=100*abs(pd[i]-nd[i])/s
 a[p*2]=np.mean(dx[p:p*2+1])
 for i in range(p*2+1,n):a[i]=(a[i-1]*(p-1)+dx[i])/p
 return a,pd,nd

def calc_atr(h,l,c,p=14):
 n=len(c);tr=np.zeros(n);r=np.zeros(n)
 for i in range(1,n):tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
 r[p]=np.mean(tr[1:p+1])
 for i in range(p+1,n):r[i]=(r[i-1]*(p-1)+tr[i])/p;return r

def run(days=90):
 h1c=int(days*86400/3600+500)
 r1=mt5.copy_rates_from_pos("XAUUSD",PH1,0,h1c)
 if r1 is None:return None

 h=np.array([r[2] for r in r1],dtype=float)
 l=np.array([r[3] for r in r1],dtype=float)
 c=np.array([r[4] for r in r1],dtype=float)
 t=np.array([r[0] for r in r1],dtype=float)

 e85=ema(c,85);atr=calc_atr(h,l,c,14)
 adx,_,_=calc_adx(h,l,c,14)

 vol=0.01;bal=500.0;trds=[];peak=bal;max_dd=0;cc=0;c_pd=0;c_sl=0;c_ad=0;c_en=0

 for i in range(150,len(c)):
  cc+=1
  cl=c[i];hi=h[i];lo=l[i]
  if np.isnan(e85[i]) or np.isnan(atr[i]) or np.isnan(adx[i]):continue

  ema_v=e85[i];ema_p=e85[i-1];cu_atr=atr[i];cu_adx=adx[i]

  # Adaptive thresholds (based on ATR)
  pdist=abs(cl-ema_v)*100        # Price distance in points
  slope=(ema_v-ema_p)*100        # EMA slope in points
  dist_th=round(cu_atr*1,0)               # ~ATR*60 points = ATR*0.6 at current prices
  slope_th=0.3                    # Minimum EMA slope
  adx_th=15                       # H1 ADX threshold

  # Position management
  for p in list(trds):
   if p["s"]!="open":continue
   if p["d"]=="long":
    if lo<=p["sl"]:p["s"]="closed";p["x"]=p["sl"];p["pn"]=(p["sl"]-p["en"])*vol*10;p["r"]="SL"
    elif hi>=p["tp"]:p["s"]="closed";p["x"]=p["tp"];p["pn"]=(p["tp"]-p["en"])*vol*10;p["r"]="TP"
    # Trailing
    elif hi>p["en"]+p["init"]*0.5:
     ns=round(hi-p["init"]*0.2,2)
     if ns>p["sl"]:p["sl"]=ns
   else:
    if hi>=p["sl"]:p["s"]="closed";p["x"]=p["sl"];p["pn"]=(p["en"]-p["sl"])*vol*10;p["r"]="SL"
    elif lo<=p["tp"]:p["s"]="closed";p["x"]=p["tp"];p["pn"]=(p["en"]-p["tp"])*vol*10;p["r"]="TP"
    elif lo<p["en"]-p["init"]*0.5:
     ns=round(lo+p["init"]*0.2,2)
     if ns<p["sl"]:p["sl"]=ns

  open_p=[p for p in trds if p["s"]=="open"]
  if open_p:continue

  # TREND mode only: EMA85 slope + distance + ADX
  c_pd+=1 if pdist>dist_th else 0;c_sl+=1 if abs(slope)>slope_th else 0;c_ad+=1 if cu_adx>adx_th else 0
  if cu_adx>adx_th and pdist>dist_th and abs(slope)>slope_th:
   init_sl=cu_atr*1.8
   if slope>0: # EMA rising ? long
    sl=round(cl-init_sl,2);tp=round(cl+init_sl*2.5,2)
    trds.append({"d":"long","en":cl,"sl":sl,"tp":tp,"s":"open","init":init_sl,"et":t[i]})
   elif slope<0: # EMA falling ? short
    sl=round(cl+init_sl,2);tp=round(cl-init_sl*2.5,2)
    trds.append({"d":"short","en":cl,"sl":sl,"tp":tp,"s":"open","init":init_sl,"et":t[i]})

  # Track equity
  fp=sum((cl-p["en"])*vol*10 if p["d"]=="long" else (p["en"]-cl)*vol*10 for p in open_p)
  eq=bal+sum(x["pn"] for x in trds if x["s"]=="closed")+fp
  if eq>peak:peak=eq
  max_dd=max(max_dd,peak-eq)

 ct=[t for t in trds if t["s"]=="closed"];tl=len(ct)
 if tl<2:print("  Debug: cc={} pd={} sl={} adx={} en={}".format(cc,c_pd,c_sl,c_ad,c_en));return None
 w=[t for t in ct if t["pn"]>0];l2=[t for t in ct if t["pn"]<=0]
 tpnl=sum(t["pn"] for t in ct);wr=len(w)/tl*100
 aw=sum(t["pn"] for t in w)/len(w) if w else 0
 al=sum(t["pn"] for t in l2)/len(l2) if l2 else -0.01
 pf=(sum(t["pn"] for t in w)/sum(abs(t["pn"]) for t in l2)) if l2 and sum(abs(t["pn"]) for t in l2)>0 else 999
 mw=max(t["pn"] for t in ct);ml=min(t["pn"] for t in ct)
 avg_h=sum((t["et"]-t.get("st",t["et"])) for t in ct)/len(ct)/3600 if ct else 0

 print("  Trades:{} WR:{:.1f}% PnL:{:+.2f} PF:{:.2f}".format(tl,wr,tpnl,pf))
 print("  AvgW:{:+.2f} AvgL:{:+.2f} MaxDD:{:.2f}".format(aw,al,max_dd))
 print("  MaxWin:{:+.2f} MaxLoss:{:+.2f}".format(mw,ml))
 return True

for d in [30,60,90,180]:
 print("\n=== {} days ===".format(d))
 r=run(d)
 if r is None:print("  Too few trades")

mt5.shutdown();print("\nDone")
