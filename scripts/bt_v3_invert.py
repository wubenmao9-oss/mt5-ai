"""Inverted V2: Fade all V2 signals"""
import MetaTrader5 as mt5, numpy as np
mt5.initialize(); print("MT5: {}".format(mt5.version()))
P5,PH1=mt5.TIMEFRAME_M5,mt5.TIMEFRAME_H1

def calc_adx(h,l,c,p=14):
 n=len(c);a=np.full(n,np.nan);pd=np.full(n,np.nan);nd=np.full(n,np.nan)
 tr=np.zeros(n);pm=np.zeros(n);nm=np.zeros(n)
 for i in range(1,n):
  tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
  u=h[i]-h[i-1];d=l[i-1]-l[i]
  if u>d and u>0:pm[i]=u
  if d>u and d>0:nm[i]=d
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
 for i in range(p*2+1,n):a[i]=a[i-1]-a[i-1]/p+dx[i]
 return a,pd,nd

def calc_bb(c,p=20,s=2.0):
 n=len(c);m=np.full(n,np.nan);u=np.full(n,np.nan);l=np.full(n,np.nan)
 for i in range(p-1,n):
  w=c[i-p+1:i+1];a=np.mean(w);d=np.std(w,ddof=0);m[i]=a;u[i]=a+s*d;l[i]=a-s*d
 return m,u,l

def calc_atr(h,l,c,p=14):
 n=len(c);tr=np.zeros(n);r=np.zeros(n)
 for i in range(1,n):tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
 r[p]=np.mean(tr[1:p+1])
 for i in range(p+1,n):r[i]=(r[i-1]*(p-1)+tr[i])/p;return r

def sma(a,p):
 r=np.full_like(a,np.nan)
 for i in range(p-1,len(a)):r[i]=np.mean(a[i-p+1:i+1]);return r

for days in [30,60,90]:
 cnt=int(days*86400/300)+500
 r5=mt5.copy_rates_from_pos("XAUUSD",P5,0,cnt)
 rh1=mt5.copy_rates_from_pos("XAUUSD",PH1,0,int(days*86400/3600)+200)
 if r5 is None or rh1 is None:continue
 t=np.array([r[0] for r in r5]);h=np.array([r[2] for r in r5],dtype=float)
 l=np.array([r[3] for r in r5],dtype=float);c=np.array([r[4] for r in r5],dtype=float)
 ht=np.array([r[0] for r in rh1]);hc=np.array([r[4] for r in rh1],dtype=float)
 h1s20=sma(hc,20);h1s50=sma(hc,50)
 adx,pdi,ndi=calc_adx(h,l,c,14)
 bm,bu,bl=calc_bb(c,20,2.0);atr=calc_atr(h,l,c,14)
 warm=50;pos=None;trds=[];bal=500.0;eq=[bal];dd=0;peak=bal
 touch_t=0;touch_b=0
 for i in range(warm,len(c)):
  hi=h[i];lo=l[i];cl=c[i];opn=c[i]
  cu_a=adx[i];cu_p=pdi[i];cu_n=ndi[i];cu_bu=bu[i];cu_bl=bl[i];cu_bm=bm[i];cu_at=atr[i]
  if np.isnan(cu_a)or np.isnan(cu_bu):continue
  h1i=None
  for j in range(len(ht)-1,-1,-1):
   if ht[j]<=t[i]:h1i=j;break
  if h1i is None or h1i<50 or np.isnan(h1s20[h1i]):continue
  s20=h1s20[h1i];s50=h1s50[h1i]
  h1_bull=s20>s50;h1_bear=s20<s50
  adx_r=cu_a>adx[i-1] if i>0 and not np.isnan(adx[i-1]) else False
  bw=cu_bu-cu_bl;tol=bw*0.02
  # Spot mgmt
  if pos is not None:
   en=pos["en"];sl=pos["sl"];tp=pos["tp"];et=pos["et"];eb=pos["eb"];d2=pos["d"]
   cls=False
   if d2=="l":
    if lo<=sl:pnl=(sl-en)*0.01*10;trds.append({"r":"SL","pnl":pnl,"h":i-eb});bal+=pnl;pos=None;cls=True
    elif hi>=tp:pnl=(tp-en)*0.01*10;trds.append({"r":"TP","pnl":pnl,"h":i-eb});bal+=pnl;pos=None;cls=True
   else:
    if hi>=sl:pnl=(en-sl)*0.01*10;trds.append({"r":"SL","pnl":pnl,"h":i-eb});bal+=pnl;pos=None;cls=True
    elif lo<=tp:pnl=(en-tp)*0.01*10;trds.append({"r":"TP","pnl":pnl,"h":i-eb});bal+=pnl;pos=None;cls=True
   eq.append(bal);continue
  # INVERTED V2 signals
  if cu_a>=20 and adx_r:
   body=abs(cl-opn);bb_bu=cl>cu_bu;bb_bd=cl<cu_bl
   if bb_bu and cu_p>cu_n and body>cu_at*0.3:
    # V2 says BUY ? INVERTED: SELL
    sl=round(hi+cu_at*0.2,2);tp=round(cl-2.5*(hi-cl+cu_at*0.2),2)
    pos={"d":"s","en":cl,"sl":sl,"tp":tp,"et":t[i],"eb":i}
   elif bb_bd and cu_n>cu_p and body>cu_at*0.3:
    # V2 says SELL ? INVERTED: BUY
    sl=round(lo-cu_at*0.2,2);tp=round(cl+2.5*(cl-lo+cu_at*0.2),2)
    pos={"d":"l","en":cl,"sl":sl,"tp":tp,"et":t[i],"eb":i}
  elif cu_a<23:
   t_top=hi>=cu_bu-tol;t_bot=lo<=cu_bl+tol
   if t_top:touch_t+=1
   if t_bot:touch_b+=1
   if hi<cu_bu and lo>cu_bl:touch_t=max(0,touch_t-1);touch_b=max(0,touch_b-1)
   # INVERTED RANGING: fade V2 signals
   if t_bot and touch_b<3 and cl<cu_bm:
    if h1_bear:  # V2 wants BUY (bullish) ? INVERTED: SELL when bearish
     sl=cl+bw*0.5;tp=cu_bm
     pos={"d":"s","en":cl,"sl":round(sl,2),"tp":round(tp,2),"et":t[i],"eb":i}
   elif t_top and touch_t<3 and cl>cu_bm:
    if h1_bull:  # V2 wants SELL (bearish) ? INVERTED: BUY when bullish
     sl=cl-bw*0.5;tp=cu_bm
     pos={"d":"l","en":cl,"sl":round(sl,2),"tp":round(tp,2),"et":t[i],"eb":i}
  eq.append(bal)
 ct=[t for t in trds if t["r"]in("TP","SL")];tl=len(ct)
 if tl<5:print("{}d: {} trds - too few".format(days,tl));continue
 w=sum(1 for t in ct if t["pnl"]>0)
 tpnl=sum(t["pnl"] for t in ct);wr=w/tl*100
 aw=sum(t["pnl"] for t in ct if t["pnl"]>0)/w if w else 0
 al=sum(abs(t["pnl"]) for t in ct if t["pnl"]<=0)/(tl-w) if tl-w>0 else 0
 pf=(sum(t["pnl"] for t in ct if t["pnl"]>0)/sum(abs(t["pnl"]) for t in ct if t["pnl"]<=0)) if tl-w>0 else 999
 mw=max(t["pnl"] for t in ct);ml=min(t["pnl"] for t in ct);ah=sum(t["h"] for t in ct)/tl*5
 print("{}d: {} trds WR:{:.0f}% PnL:{:+.0f} PF:{:.1f}".format(days,tl,wr,tpnl,pf))
 print("   AW:{:+.0f} AL:{:.0f} MaxW:{:+.0f} MaxL:{:.0f} AvgH:{:.0f}m".format(aw,al,mw,ml,ah))
mt5.shutdown()
