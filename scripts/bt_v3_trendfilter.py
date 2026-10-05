"""V3: V2 signals + daily trend filter (H4 SMA20)"""
import MetaTrader5 as mt5, numpy as np
mt5.initialize(); print("MT5: {}".format(mt5.version()))
P5,PH1,PH4=mt5.TIMEFRAME_M5,mt5.TIMEFRAME_H1,mt5.TIMEFRAME_H4

def calc_adx(h,l,c,p=14):
 n=len(c);a=np.full(n,np.nan,dtype=np.float64);pd=np.full(n,np.nan,dtype=np.float64);nd=np.full(n,np.nan,dtype=np.float64)
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
 a[p*2]=np.nanmean(dx[p:p*2+1])
 for i in range(p*2+1,n):a[i]=a[i-1]-a[i-1]/p+dx[i]
 return a,pd,nd

def calc_bb(c,p=20,s=2.0):
 n=len(c);m=np.full(n,np.nan,dtype=np.float64);u=np.full(n,np.nan,dtype=np.float64);l=np.full(n,np.nan,dtype=np.float64)
 for i in range(p-1,n):
  w=c[i-p+1:i+1];av=np.nanmean(w);sd=np.nanstd(w,ddof=0);m[i]=av;u[i]=av+s*sd;l[i]=av-s*sd
 return m,u,l

def calc_atr(h,l,c,p=14):
 n=len(c);tr=np.zeros(n);r=np.zeros(n)
 for i in range(1,n):tr[i]=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
 r[p]=np.nanmean(tr[1:p+1])
 for i in range(p+1,n):r[i]=(r[i-1]*(p-1)+tr[i])/p;return r

def sma(a,p):
 r=np.full(len(a),np.nan,dtype=np.float64)
 for i in range(p-1,len(a)):r[i]=np.nanmean(a[i-p+1:i+1]);return r

for days in [30,60,90,180]:
 cnt5=int(days*86400/300)+500;cnt1=int(days*86400/3600)+200;cnt4=int(days*86400/14400)+100
 r5=mt5.copy_rates_from_pos("XAUUSD",P5,0,cnt5)
 r1=mt5.copy_rates_from_pos("XAUUSD",PH1,0,cnt1)
 if r5 is None or r1 is None:continue
 t=np.array([r[0] for r in r5]);h=np.array([r[2] for r in r5],dtype=float)
 l=np.array([r[3] for r in r5],dtype=float);c=np.array([r[4] for r in r5],dtype=float)
 ht=np.array([r[0] for r in r1]);hc=np.array([r[4] for r in r1],dtype=float)
 
 h1s20=sma(hc,20);h1s50=sma(hc,50)
 
 adx,pdi,ndi=calc_adx(h,l,c,14)
 bm,bu,bl=calc_bb(c,20,2.0);atr=calc_atr(h,l,c,14)
 vol=0.01;bal=500;pos=None;trds=[];tt=0;tb=0;prev_d=-1;day_dir="none"
 for i in range(100,len(c)):
  hi=h[i];lo=l[i];cl=c[i];op=c[i]
  ca=adx[i];cp=pdi[i];cn=ndi[i];cbu=bu[i];cbl=bl[i];cbm=bm[i];cat=atr[i]
  if np.isnan(ca)or np.isnan(cbu):continue
  # H1 SMA for V2 entry
  h1i=None
  for j in range(len(ht)-1,-1,-1):
   if ht[j]<=t[i]:h1i=j;break
  if h1i is None or h1i<50 or np.isnan(h1s20[h1i]):continue
  h1b=h1s20[h1i]>h1s50[h1i];h1s=h1s20[h1i]<h1s50[h1i]
  # Daily trend filter (H1 SMA20/50 - fixed once per day)
  cd=int(t[i]/86400)
  if cd!=prev_d:
   prev_d=cd;tt=0;tb=0
   if h1i is not None and h1i>=50 and not np.isnan(h1s20[h1i]):
    day_dir="long" if h1s20[h1i]>h1s50[h1i] else "short"
   else:day_dir="none"
  bw=cbu-cbl;tol=bw*0.02;adxr=ca>adx[i-1] if i>0 and not np.isnan(adx[i-1]) else False
  # Position management
  if pos:
   en=pos["en"];sl=pos["sl"];tp=pos["tp"];eb=pos["eb"]
   if pos["d"]=="l":
    if lo<=sl:pnl=(sl-en)*vol*10;trds.append({"r":"SL","pnl":pnl,"h":i-eb});bal+=pnl;pos=None
    elif hi>=tp:pnl=(tp-en)*vol*10;trds.append({"r":"TP","pnl":pnl,"h":i-eb});bal+=pnl;pos=None
   else:
    if hi>=sl:pnl=(en-sl)*vol*10;trds.append({"r":"SL","pnl":pnl,"h":i-eb});bal+=pnl;pos=None
    elif lo<=tp:pnl=(en-tp)*vol*10;trds.append({"r":"TP","pnl":pnl,"h":i-eb});bal+=pnl;pos=None
   if pos:continue
  if day_dir=="none":continue
  # TRENDING
  if ca>=20 and adxr:
   body=abs(cl-op);bbu=cl>cbu;bbd=cl<cbl
   if bbu and cp>cn and body>cat*0.3 and day_dir=="long":
    sl=round(lo-cat*0.2,2);tp=round(cl+2.5*(cl-lo+cat*0.2),2)
    pos={"d":"l","en":cl,"sl":sl,"tp":tp,"eb":i}
   elif bbd and cn>cp and body>cat*0.3 and day_dir=="short":
    sl=round(hi+cat*0.2,2);tp=round(cl-2.5*(hi-cl+cat*0.2),2)
    pos={"d":"s","en":cl,"sl":sl,"tp":tp,"eb":i}
  # RANGING
  elif ca<23:
   tt=tt+1 if hi>=cbu-tol else max(0,tt-1) if hi<cbu and lo>cbl else tt
   tb=tb+1 if lo<=cbl+tol else max(0,tb-1) if hi<cbu and lo>cbl else tb
   if lo<=cbl+tol and tb<3 and cl<cbm and h1b and day_dir=="long":
    sl=cl-bw*0.5;tp=cbm
    pos={"d":"l","en":cl,"sl":round(sl,2),"tp":round(tp,2),"eb":i}
   elif hi>=cbu-tol and tt<3 and cl>cbm and h1s and day_dir=="short":
    sl=cl+bw*0.5;tp=cbm
    pos={"d":"s","en":cl,"sl":round(sl,2),"tp":round(tp,2),"eb":i}
 ct=[t for t in trds if t["r"]in("TP","SL")];tl=len(ct)
 if tl<3:print("{}d: {} trds".format(days,tl));continue
 w=sum(1 for t in ct if t["pnl"]>0)
 tpnl=sum(t["pnl"] for t in ct);wr=w/tl*100
 aw=sum(t["pnl"] for t in ct if t["pnl"]>0)/w if w else 0
 al=sum(abs(t["pnl"]) for t in ct if t["pnl"]<=0)/(tl-w) if tl-w>0 else 0
 pf=(sum(t["pnl"] for t in ct if t["pnl"]>0)/sum(abs(t["pnl"]) for t in ct if t["pnl"]<=0)) if tl-w>0 else 999
 ah=sum(t["h"] for t in ct)/tl*5
 # Count long vs short days
 ld=sum(1 for t in trds if t["r"]in("TP","SL") and "d" in t and t.get("d","")=="l")
 sd=sum(1 for t in trds if t["r"]in("TP","SL") and "d" in t and t.get("d","")=="s")
 print("{}d: {} trds WR:{:.0f}% PnL:{:+.0f} PF:{:.1f} AW:{:+.0f} AL:{:.0f} AvgH:{:.0f}m".format(days,tl,wr,tpnl,pf,aw,al,ah))
 print("   Long:{}/{} Short:{}/{}".format(
   sum(1 for t in ct if t.get("d","")=="l" and t["pnl"]>0),sum(1 for t in ct if t.get("d","")=="l"),
   sum(1 for t in ct if t.get("d","")=="s" and t["pnl"]>0),sum(1 for t in ct if t.get("d","")=="s")))
mt5.shutdown()
