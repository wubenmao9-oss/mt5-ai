"""V2 vs V3(原版) vs V3(动态偏向) 回测对比"""
import MetaTrader5 as mt5
import numpy as np
mt5.initialize()
SYMBOL="XAUUSD"; PT=10.0

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
    for i in range(p+1,n):
        r[i]=(r[i-1]*(p-1)+tr[i])/p
    return r

def sma(a,p):
 n=len(a);r=np.full(n,np.nan)
 for i in range(p-1,n):
  s=0.0;cnt=0
  for j in range(i-p+1,i+1):
   if not np.isnan(a[j]):s+=a[j];cnt+=1
  if cnt>0:r[i]=s/cnt;return r

def fetch(days):
 cm5=int(days*86400/300)+500;ch1=int(days*86400/3600)+200;cd1=days+20
 r5=mt5.copy_rates_from_pos(SYMBOL,mt5.TIMEFRAME_M5,0,cm5)
 rh1=mt5.copy_rates_from_pos(SYMBOL,mt5.TIMEFRAME_H1,0,ch1)
 rd1=mt5.copy_rates_from_pos(SYMBOL,mt5.TIMEFRAME_D1,0,cd1)
 if r5 is None or rh1 is None or rd1 is None:return None
 h=np.array([r[2] for r in r5],dtype=float);l=np.array([r[3] for r in r5],dtype=float)
 c=np.array([r[4] for r in r5],dtype=float);o=np.array([r[1] for r in r5],dtype=float)
 hc=np.array([r[4] for r in rh1],dtype=float);ho=np.array([r[1] for r in rh1],dtype=float)
 hh=np.array([r[2] for r in rh1],dtype=float);hl=np.array([r[3] for r in rh1],dtype=float)
 ht=np.array([r[0] for r in rh1],dtype=float);t5=np.array([r[0] for r in r5],dtype=float)
 d1h=np.array([r[2] for r in rd1],dtype=float);d1l=np.array([r[3] for r in rd1],dtype=float)
 d1o=np.array([r[1] for r in rd1],dtype=float);d1t=np.array([r[0] for r in rd1],dtype=float)
 for a in [c,hc,ho,hh,hl,o]:
  for j in range(1,len(a)):
   if np.isnan(a[j]):a[j]=a[j-1]
 return h,l,c,o,hc,ho,hh,hl,ht,t5,d1h,d1l,d1o,d1t

def get_bias_static(d1o,d1h,d1l,d1t,t5):
 """V3 原版: 全天固定偏向"""
 bias=np.full(len(t5),None,dtype=object)
 for i in range(len(t5)):
  idx=np.searchsorted(d1t,t5[i],side='right')-1
  if idx>=1 and idx<len(d1t):
   yh=d1h[idx-1];yl=d1l[idx-1];to=d1o[idx]
   rng=yh-yl
   if rng>0:
    pos=(to-yl)/rng
    if pos<0.5:bias[i]='long'
    elif pos>0.5:bias[i]='short'
 return bias

def get_bias_dynamic(d1h,d1l,d1t,t5,c):
 """V3 动态版: 根据价格在昨日K线内的位置实时改变偏向"""
 bias=np.full(len(t5),None,dtype=object)
 for i in range(len(t5)):
  idx=np.searchsorted(d1t,t5[i],side='right')-1
  if idx>=1 and idx<len(d1t):
   yh=d1h[idx-1];yl=d1l[idx-1];rng=yh-yl
   if rng>0:
    pos=(c[i]-yl)/rng
    if pos<0.33:bias[i]='long'
    elif pos>0.67:bias[i]='short'
    else:bias[i]=None
  else:bias[i]=None
 return bias

def run(h,l,c,o,ht,hc,ho,hh,hl,t5,db,dual_mode=False):
 """通用回测引擎: if dual_mode then use V2 mode (no bias)"""
 adx,pdi,ndi=calc_adx(h,l,c,14);bm,bu,bl=calc_bb(c,20,2.0)
 atr=calc_atr(h,l,c,14);h20=sma(hc,20);h50=sma(hc,50)
 p=None;tr=[];tt=0;tb=0
 for i in range(100,len(c)):
  if np.isnan(adx[i]) or np.isnan(bu[i]):continue
  h1i=np.searchsorted(ht,t5[i],side='right')-1
  if h1i<50 or np.isnan(h20[h1i]):continue
  h1b=h20[h1i]>h50[h1i];h1s=h20[h1i]<h50[h1i]
  ca=adx[i];cp=pdi[i];cn=ndi[i];cbu=bu[i];cbl=bl[i];cbm=bm[i];cat=atr[i];bw=cbu-cbl;tol=bw*0.02;body=abs(c[i]-o[i])
  if p:
   en=p['en'];sl=p['sl'];tp=p['tp'];d2=p['d'];eb=p['eb']
   if d2=='l':
    if l[i]<=sl:pnl=(sl-en)*1.0;tr.append({'r':'SL','pnl':pnl,'h':i-eb});p=None
    elif h[i]>=tp:pnl=(tp-en)*1.0;tr.append({'r':'TP','pnl':pnl,'h':i-eb});p=None
   else:
    if h[i]>=sl:pnl=(en-sl)*1.0;tr.append({'r':'SL','pnl':pnl,'h':i-eb});p=None
    elif l[i]<=tp:pnl=(en-tp)*1.0;tr.append({'r':'TP','pnl':pnl,'h':i-eb});p=None
   if p:continue
  adx_r=ca>adx[i-1] if i>0 and not np.isnan(adx[i-1]) else False
  tt=tt+1 if h[i]>=cbu-tol else max(0,tt-1) if h[i]<cbu and l[i]>cbl else tt
  tb=tb+1 if l[i]<=cbl+tol else max(0,tb-1) if h[i]<cbu and l[i]>cbl else tb
  b=db[i] if not dual_mode else None
  if ca>=20 and adx_r:
   if c[i]>cbu and cp>cn and body>cat*0.3 and h1b and(b!='short'if b else 1):
    sl=round(cbl-bw*0.1,2);tp=round(c[i]+PT,2);p={'d':'l','en':c[i],'sl':sl,'tp':tp,'eb':i}
   elif c[i]<cbl and cn>cp and body>cat*0.3 and h1s and(b!='long'if b else 1):
    sl=round(cbu+bw*0.1,2);tp=round(c[i]-PT,2);p={'d':'s','en':c[i],'sl':sl,'tp':tp,'eb':i}
  elif ca<23:
   if l[i]<=cbl+tol and tb<3 and c[i]<cbm and h1b and(b!='short'if b else 1):
    sl=round(c[i]-bw*0.5,2);tp=round(c[i]+PT,2);p={'d':'l','en':c[i],'sl':sl,'tp':tp,'eb':i}
   elif h[i]>=cbu-tol and tt<3 and c[i]>cbm and h1s and(b!='long'if b else 1):
    sl=round(c[i]+bw*0.5,2);tp=round(c[i]-PT,2);p={'d':'s','en':c[i],'sl':sl,'tp':tp,'eb':i}
 return tr

def an(t,l):
 ct=[x for x in t if x['r']in('SL','TP')];tl=len(ct)
 if tl<3:return None
 w=sum(1 for x in ct if x['pnl']>0);l2=tl-w
 tpnl=sum(x['pnl'] for x in ct);wr=w/tl*100
 aw=sum(x['pnl'] for x in ct if x['pnl']>0)/w if w else 0
 al2=sum(abs(x['pnl']) for x in ct if x['pnl']<=0)/l2 if l2 else 0
 mw=max(x['pnl'] for x in ct if x['pnl']>0) if w else 0
 ml=max(abs(x['pnl']) for x in ct if x['pnl']<=0) if l2 else 0
 pf=(sum(x['pnl'] for x in ct if x['pnl']>0)/sum(abs(x['pnl']) for x in ct if x['pnl']<=0)) if l2>0 else 999
 ah=sum(x['h'] for x in ct)/tl*5
 return {'l':l,'tl':tl,'wr':wr,'pnl':tpnl,'pf':pf,'aw':aw,'al':al2,'mw':mw,'ml':ml,'ah':ah}

def fmt(r):
 if r is None:return '  (too few trades)'
 return f'  {r["l"]:6s}: {r["tl"]:3d}笔 | WR:{r["wr"]:3.0f}% | PnL:{r["pnl"]:+6.0f} | PF:{r["pf"]:.2f} | AW:{r["aw"]:+5.2f} | AL:{r["al"]:5.2f} | MaxW:{r["mw"]:6.2f} | MaxL:{r["ml"]:6.2f} | Avg:{r["ah"]:3.0f}m'

print('='*95)
print('     V2 vs V3(静态偏向) vs V3(动态偏向) 对比回测')
print('='*95)
print('     V2: 无偏向过滤（原始策略）')
print('     V3静态: 开盘在昨日K线下半区→全天只做多, 上半区→只做空')
print('     V3动态: 价格在昨日K线底33%→做多, 顶33%→做空, 中间→多空都可')
print('='*95)

for days in [30,60,90]:
 d=fetch(days)
 if d is None:print(f'{days}d: no data');continue
 h,l,c,o,hc,ho,hh,hl,ht,t5,d1h,d1l,d1o,d1t=d
 bs=get_bias_static(d1o,d1h,d1l,d1t,t5)
 bd=get_bias_dynamic(d1h,d1l,d1t,t5,c)
 print(f'\n  --- {days}天回测 ---')
 print(fmt(an(run(h,l,c,o,ht,hc,ho,hh,hl,t5,None,True),'V2')))
 print(fmt(an(run(h,l,c,o,ht,hc,ho,hh,hl,t5,bs,False),'V3静态')))
 print(fmt(an(run(h,l,c,o,ht,hc,ho,hh,hl,t5,bd,False),'V3动态')))
mt5.shutdown()
