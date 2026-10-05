"""Combined V2 vs V3 vs V4 backtest — ALL with $10 (1000pts) profit target"""
import MetaTrader5 as mt5
import numpy as np

mt5.initialize()
SYMBOL="XAUUSD"; PT=10.0  # $10 profit = 1000pts = 10.0 price

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
 for i in range(p*2+1,n):a[i]=a[i-1]-a[i-1]/p+dx[i]
 return a,pd,nd

def calc_bb(c,p=20,s=2.0):
 n=len(c);m=np.full(n,np.nan);u=np.full(n,np.nan);l=np.full(n,np.nan)
 for i in range(p-1,n):
  w=c[i-p+1:i+1];av=np.mean(w);sd=np.std(w,ddof=0);m[i]=av;u[i]=av+s*sd;l[i]=av-s*sd
 return m,u,l

def calc_atr(h,l,c,p=14):
 n=len(c);tr=np.zeros(n);r=np.zeros(n)
 for i in range(1,n):tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
 r[p]=np.mean(tr[1:p+1])
 for i in range(p+1,n):r[i]=(r[i-1]*(p-1)+tr[i])/p
 return r

def calc_rsi(c,p=7):
 n=len(c);r=np.full(n,np.nan);g=np.zeros(n);l=np.zeros(n)
 for i in range(1,n):
  d=c[i]-c[i-1]
  if d>0:g[i]=d
  else:l[i]=-d
 ag=np.zeros(n);al=np.zeros(n)
 ag[p]=np.mean(g[1:p+1]);al[p]=np.mean(l[1:p+1])
 for i in range(p+1,n):
  ag[i]=(ag[i-1]*(p-1)+g[i])/p;al[i]=(al[i-1]*(p-1)+l[i])/p
 for i in range(p,n):
  if al[i]>0:r[i]=100-100/(1+ag[i]/al[i])
  elif ag[i]>0:r[i]=100
  else:r[i]=50
 return r

def sma(a,p):
 n=len(a);r=np.full(n,np.nan)
 for i in range(p-1,n):
  s=0.0;cnt=0
  for j in range(i-p+1,i+1):
   if not np.isnan(a[j]):s+=a[j];cnt+=1
  if cnt>0:r[i]=s/cnt
 return r

def ema(c,p):
 n=len(c);r=np.full(n,np.nan)
 if n<p: return r
 m=2.0/(p+1);r[p-1]=np.mean(c[:p])
 for i in range(p,n): r[i]=(c[i]-r[i-1])*m+r[i-1]
 return r

def fetch_data(days):
 cnt_m5=int(days*86400/300)+500;cnt_h1=int(days*86400/3600)+200
 r5=mt5.copy_rates_from_pos(SYMBOL,mt5.TIMEFRAME_M5,0,cnt_m5)
 rh1=mt5.copy_rates_from_pos(SYMBOL,mt5.TIMEFRAME_H1,0,cnt_h1)
 if r5 is None or rh1 is None: return None
 h=np.array([r[2] for r in r5],dtype=float);l=np.array([r[3] for r in r5],dtype=float)
 c=np.array([r[4] for r in r5],dtype=float);o=np.array([r[1] for r in r5],dtype=float)
 hc=np.array([r[4] for r in rh1],dtype=float);ho=np.array([r[1] for r in rh1],dtype=float)
 hh=np.array([r[2] for r in rh1],dtype=float);hl=np.array([r[3] for r in rh1],dtype=float)
 ht=np.array([r[0] for r in rh1],dtype=float);t5=np.array([r[0] for r in r5],dtype=float)
 for a in [c,hc,ho,hh,hl,o]:
  for j in range(1,len(a)):
   if np.isnan(a[j]):a[j]=a[j-1]
 return h,l,c,o,hc,ho,hh,hl,ht,t5

# ===== V2: ADX+BB direct entry =====
def backtest_v2(h,l,c,o,hc,ho,hh,hl,ht,t5):
 adx,pdi,ndi=calc_adx(h,l,c,14);bb_m,bb_u,bb_l=calc_bb(c,20,2.0)
 atr_m5=calc_atr(h,l,c,14);h1_sma20=sma(hc,20);h1_sma50=sma(hc,50)
 pos=None;trades=[];tt=0;tb=0
 for i in range(100,len(c)):
  if np.isnan(adx[i]) or np.isnan(bb_u[i]): continue
  h1i=np.searchsorted(ht,t5[i],side='right')-1
  if h1i<50 or np.isnan(h1_sma20[h1i]): continue
  h1b=h1_sma20[h1i]>h1_sma50[h1i];h1s=h1_sma20[h1i]<h1_sma50[h1i]
  ca=adx[i];cp=pdi[i];cn=ndi[i]
  cbu=bb_u[i];cbl=bb_l[i];cbm=bb_m[i];cat=atr_m5[i]
  bw=cbu-cbl;tol=bw*0.02;body=abs(c[i]-o[i])
  if pos:
   en=pos["en"];sl=pos["sl"];tp=pos["tp"];d2=pos["d"];eb=pos["eb"]
   if d2=="l":
    if l[i]<=sl: pnl=(sl-en)*1.0;trades.append({"r":"SL","pnl":pnl,"h":i-eb});pos=None
    elif h[i]>=tp: pnl=(tp-en)*1.0;trades.append({"r":"TP","pnl":pnl,"h":i-eb});pos=None
   else:
    if h[i]>=sl: pnl=(en-sl)*1.0;trades.append({"r":"SL","pnl":pnl,"h":i-eb});pos=None
    elif l[i]<=tp: pnl=(en-tp)*1.0;trades.append({"r":"TP","pnl":pnl,"h":i-eb});pos=None
   if pos: continue
  adx_r=ca>adx[i-1] if i>0 and not np.isnan(adx[i-1]) else False
  tt=tt+1 if h[i]>=cbu-tol else max(0,tt-1) if h[i]<cbu and l[i]>cbl else tt
  tb=tb+1 if l[i]<=cbl+tol else max(0,tb-1) if h[i]<cbu and l[i]>cbl else tb
  if ca>=20 and adx_r:
   if c[i]>cbu and cp>cn and body>cat*0.3 and h1b:
    sl=round(cbl-bw*0.1,2);tp=round(c[i]+PT,2);pos={"d":"l","en":c[i],"sl":sl,"tp":tp,"eb":i}
   elif c[i]<cbl and cn>cp and body>cat*0.3 and h1s:
    sl=round(cbu+bw*0.1,2);tp=round(c[i]-PT,2);pos={"d":"s","en":c[i],"sl":sl,"tp":tp,"eb":i}
  elif ca<23:
   if l[i]<=cbl+tol and tb<3 and c[i]<cbm and h1b:
    sl=round(c[i]-bw*0.5,2);tp=round(c[i]+PT,2);pos={"d":"l","en":c[i],"sl":sl,"tp":tp,"eb":i}
   elif h[i]>=cbu-tol and tt<3 and c[i]>cbm and h1s:
    sl=round(c[i]+bw*0.5,2);tp=round(c[i]-PT,2);pos={"d":"s","en":c[i],"sl":sl,"tp":tp,"eb":i}
 return trades

# ===== V3: ADX deadband + pullback entry =====
def backtest_v3(h,l,c,o,hc,ho,hh,hl,ht,t5):
 adx,pdi,ndi=calc_adx(h,l,c,14);bb_m,bb_u,bb_l=calc_bb(c,20,2.0)
 atr_m5=calc_atr(h,l,c,14);ema10=ema(c,10)
 h1_sma20=sma(hc,20);h1_sma50=sma(hc,50)
 pos=None;trades=[];tt=0;tb=0
 trend_bias=None;pullback_waiting=False;breakout_price=0.0
 for i in range(100,len(c)):
  if np.isnan(adx[i]) or np.isnan(bb_u[i]): continue
  h1i=np.searchsorted(ht,t5[i],side='right')-1
  if h1i<50 or np.isnan(h1_sma20[h1i]): continue
  h1b=h1_sma20[h1i]>h1_sma50[h1i];h1s=h1_sma20[h1i]<h1_sma50[h1i]
  ca=adx[i];cp=pdi[i];cn=ndi[i]
  cbu=bb_u[i];cbl=bb_l[i];cbm=bb_m[i];cat=atr_m5[i]
  bw=cbu-cbl;tol=bw*0.02;body=abs(c[i]-o[i])

  if pos:
   en=pos["en"];sl=pos["sl"];d2=pos["d"];eb=pos["eb"]
   if d2=="l":
    if l[i]<=sl:
     pnl=(sl-en)*1.0;trades.append({"r":"SL","pnl":pnl,"h":i-eb});pos=None
     break
    elif h[i]>=cbu:  # Opposite BB exit
     pnl=(c[i]-en)*1.0;trades.append({"r":"OPP_BB","pnl":pnl,"h":i-eb});pos=None
     if pos: continue
   else:
    if h[i]>=sl:
     pnl=(en-sl)*1.0;trades.append({"r":"SL","pnl":pnl,"h":i-eb});pos=None
     break
    elif l[i]<=cbl:
     pnl=(en-c[i])*1.0;trades.append({"r":"OPP_BB","pnl":pnl,"h":i-eb});pos=None
     if pos: continue
  if pos: continue

  adx_r=ca>adx[i-1] if i>0 and not np.isnan(adx[i-1]) else False
  tt=tt+1 if h[i]>=cbu-tol else max(0,tt-1) if h[i]<cbu and l[i]>cbl else tt
  tb=tb+1 if l[i]<=cbl+tol else max(0,tb-1) if h[i]<cbu and l[i]>cbl else tb

  if ca>25 and adx_r: state="TRENDING"
  elif ca<20: state="RANGING"
  else: state="NO_TRADE"; trend_bias=None; pullback_waiting=False; continue

  if state=="TRENDING":
   bb_bu=c[i]>cbu;bb_bd=c[i]<cbl
   if pullback_waiting and trend_bias:
    if trend_bias=="long" and c[i]<=cbm*1.002 and c[i]>=cbl and h1b:
     sl=round(c[i]-cat*1.5,2);pos={"d":"l","en":c[i],"sl":sl,"eb":i};trend_bias=None;pullback_waiting=False
    elif trend_bias=="short" and c[i]>=cbm*0.998 and c[i]<=cbu and h1s:
     sl=round(c[i]+cat*1.5,2);pos={"d":"s","en":c[i],"sl":sl,"eb":i};trend_bias=None;pullback_waiting=False
    elif trend_bias=="long" and c[i]<breakout_price*0.99: trend_bias=None;pullback_waiting=False
    elif trend_bias=="short" and c[i]>breakout_price*1.01: trend_bias=None;pullback_waiting=False
   else:
    if bb_bu and cp>cn and body>cat*0.3 and h1b: trend_bias="long";pullback_waiting=True;breakout_price=c[i]
    elif bb_bd and cn>cp and body>cat*0.3 and h1s: trend_bias="short";pullback_waiting=True;breakout_price=c[i]
  elif state=="RANGING":
   trend_bias=None;pullback_waiting=False
   if l[i]<=cbl+tol and tb<3 and c[i]<cbm and h1b:
    sl=round(c[i]-bw*0.5,2);pos={"d":"l","en":c[i],"sl":sl,"eb":i}
   elif h[i]>=cbu-tol and tt<3 and c[i]>cbm and h1s:
    sl=round(c[i]+bw*0.5,2);pos={"d":"s","en":c[i],"sl":sl,"eb":i}
 return trades

# ===== V4: V2 + filters =====
def backtest_v4(h,l,c,o,hc,ho,hh,hl,ht,t5):
 adx,pdi,ndi=calc_adx(h,l,c,14);bb_m,bb_u,bb_l=calc_bb(c,20,2.0)
 atr_m5=calc_atr(h,l,c,14);rsi7=calc_rsi(c,7)
 h1_sma20=sma(hc,20);h1_sma50=sma(hc,50);h1_rsi=calc_rsi(hc,14);h1_atr=calc_atr(hh,hl,hc,14)
 pos=None;trades=[];tt=0;tb=0
 for i in range(100,len(c)):
  if np.isnan(adx[i]) or np.isnan(bb_u[i]): continue
  h1i=np.searchsorted(ht,t5[i],side='right')-1
  if h1i<50 or np.isnan(h1_sma20[h1i]): continue
  h1b=h1_sma20[h1i]>h1_sma50[h1i] and h1_rsi[h1i]>50
  h1s=h1_sma20[h1i]<h1_sma50[h1i] and h1_rsi[h1i]<50
  ca=adx[i];cp=pdi[i];cn=ndi[i]
  cbu=bb_u[i];cbl=bb_l[i];cbm=bb_m[i];cat=atr_m5[i]
  bw=cbu-cbl;tol=bw*0.02;body=abs(c[i]-o[i])
  if pos:
   en=pos["en"];sl=pos["sl"];tp=pos["tp"];d2=pos["d"];eb=pos["eb"]
   if d2=="l":
    if l[i]<=sl: pnl=(sl-en)*1.0;trades.append({"r":"SL","pnl":pnl,"h":i-eb});pos=None
    elif h[i]>=tp: pnl=(tp-en)*1.0;trades.append({"r":"TP","pnl":pnl,"h":i-eb});pos=None
   else:
    if h[i]>=sl: pnl=(en-sl)*1.0;trades.append({"r":"SL","pnl":pnl,"h":i-eb});pos=None
    elif l[i]<=tp: pnl=(en-tp)*1.0;trades.append({"r":"TP","pnl":pnl,"h":i-eb});pos=None
   if pos: continue
  # Filters
  bb_w=bw/cbm if cbm>0 else 0
  if bb_w<0.0008: continue
  if i>=4 and abs(c[i]-c[i-4])<0.3: continue
  if not np.isnan(h1_atr[h1i]) and h1_atr[h1i]>0:
   h1bdy=abs(hc[h1i]-ho[h1i])
   if not np.isnan(h1bdy) and h1bdy>h1_atr[h1i]*1.2: continue
  rsi_v=rsi7[i];rsi_ok=30<rsi_v<70
  adx_r=ca>adx[i-1] if i>0 and not np.isnan(adx[i-1]) else False
  tt=tt+1 if h[i]>=cbu-tol else max(0,tt-1) if h[i]<cbu and l[i]>cbl else tt
  tb=tb+1 if l[i]<=cbl+tol else max(0,tb-1) if h[i]<cbu and l[i]>cbl else tb
  if ca>=20 and adx_r:
   if c[i]>cbu and cp>cn and body>cat*0.3 and h1b and rsi_ok:
    sl=round(cbl-bw*0.1,2);tp=round(c[i]+PT,2);pos={"d":"l","en":c[i],"sl":sl,"tp":tp,"eb":i}
   elif c[i]<cbl and cn>cp and body>cat*0.3 and h1s and rsi_ok:
    sl=round(cbu+bw*0.1,2);tp=round(c[i]-PT,2);pos={"d":"s","en":c[i],"sl":sl,"tp":tp,"eb":i}
  elif ca<23:
   if l[i]<=cbl+tol and tb<3 and c[i]<cbm and h1b and rsi_ok:
    sl=round(c[i]-bw*0.5,2);tp=round(c[i]+PT,2);pos={"d":"l","en":c[i],"sl":sl,"tp":tp,"eb":i}
   elif h[i]>=cbu-tol and tt<3 and c[i]>cbm and h1s and rsi_ok:
    sl=round(c[i]+bw*0.5,2);tp=round(c[i]-PT,2);pos={"d":"s","en":c[i],"sl":sl,"tp":tp,"eb":i}
 return trades

def analyze(trades,label):
 ct=[t for t in trades if t["r"]in("SL","TP","OPP_BB")];tl=len(ct)
 if tl<3: return f"  {label:6s}: {tl:2d}笔 (too few)"
 w=sum(1 for t in ct if t["pnl"]>0);l2=tl-w
 tpnl=sum(t["pnl"] for t in ct);wr=w/tl*100
 aw=sum(t["pnl"] for t in ct if t["pnl"]>0)/w if w else 0
 al2=sum(abs(t["pnl"]) for t in ct if t["pnl"]<=0)/l2 if l2 else 0
 mw=max(t["pnl"] for t in ct if t["pnl"]>0) if w else 0
 ml=max(abs(t["pnl"]) for t in ct if t["pnl"]<=0) if l2 else 0
 pf=(sum(t["pnl"] for t in ct if t["pnl"]>0)/sum(abs(t["pnl"]) for t in ct if t["pnl"]<=0)) if l2>0 else 999
 ah=sum(t["h"] for t in ct)/tl*5
 o=sum(1 for t in ct if t["r"]=="OPP_BB")
 return f"  {label:6s}: {tl:2d}笔 | WR:{wr:3.0f}% | PnL:{tpnl:+5.0f} | AW:{aw:+5.2f} | AL:{al2:5.2f} | PF:{pf:.2f} | MaxW:{mw:5.2f} | MaxL:{ml:5.2f} | AvgH:{ah:3.0f}m | OPP_BB:{o}"

print("V2 vs V3 vs V4 对比回测 (正确: $10=1000点止盈)")
print("="*75)
for days in [30,60,90]:
 d=fetch_data(days)
 if d is None: print(f"{days}d: no data"); continue
 h,l,c,o,hc,ho,hh,hl,ht,t5 = d
 t2=backtest_v2(h,l,c,o,hc,ho,hh,hl,ht,t5)
 t3=backtest_v3(h,l,c,o,hc,ho,hh,hl,ht,t5)
 t4=backtest_v4(h,l,c,o,hc,ho,hh,hl,ht,t5)
 print(f"\n--- {days}天 ---")
 print(analyze(t2,"V2"))
 print(analyze(t3,"V3"))
 print(analyze(t4,"V4"))
mt5.shutdown()
