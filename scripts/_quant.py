import numpy as np
from datetime import datetime, timezone
import MetaTrader5 as mt5

print("Loading 180 days of XAUUSD M5 data...")
mt5.initialize(login=0, password="你的密码", server="你的服务器")
rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, 52000)
if rates is None: print("No data"); exit()

t = np.array([r[0] for r in rates], dtype=float)
o = np.array([r[1] for r in rates], dtype=float)
h = np.array([r[2] for r in rates], dtype=float)
l = np.array([r[3] for r in rates], dtype=float)
c = np.array([r[4] for r in rates], dtype=float)
n = len(c)
print(f"Loaded {n} bars ({n*5/60/24:.1f} days)")

# === Forward analysis ===
# For each bar, look forward 12 bars and check:
# 1. What's the max HIGH (upside potential)?
# 2. What's the min LOW (downside risk)?
# 3. For various SL/TP combos, which hits first?

print("\n=== FORWARD 12-BAR ANALYSIS ===")
print(f"{'cond':>6} {'cnt':>6} {'up':>6} {'dn':>6} {'ratio':>7} {'W@8/24':>7} {'W@5/15':>7} {'W@10/30':>7}")
print("-"*60)

# Test ALL bullish bars (close > open) with various momentum/compression levels
lookback = 12
for mom_thresh in [0.3, 0.5, 0.7]:
    for rng_max in [15, 25, 35]:
        # Track forward outcomes
        total = 0; ups = 0; downs = 0
        win_8_24 = 0; win_5_15 = 0; win_10_30 = 0
        
        for i in range(lookback, n - lookback):
            # Bar conditions
            body = abs(c[i] - o[i])
            mom = body / (h[i] - l[i] + 0.01)
            hi = h[i-lookback:i+1].max()
            lo = l[i-lookback:i+1].min()
            rng = hi - lo
            bullish = c[i] > o[i]
            
            # Check if conditions met
            if not bullish: continue
            if mom < mom_thresh: continue
            if rng > rng_max: continue
            
            total += 1
            fwd_h = h[i+1:i+13].max()
            fwd_l = l[i+1:i+13].min()
            up_pot = fwd_h - c[i]
            dn_risk = c[i] - fwd_l
            ups += up_pot
            downs += dn_risk
            
            # Check various SL/TP
            # SL=8, TP=24
            sl_pt = 8; tp_pt = 24
            sl_hit = fwd_l <= c[i] - sl_pt
            tp_hit = fwd_h >= c[i] + tp_pt
            if sl_hit and tp_hit:
                # Both hit - check which first (approximate: use mid-bar check)
                mid = (h[i+1:i+13].max() + l[i+1:i+13].min()) / 2
                if mid > c[i]: win_8_24 += 1  # TP likely first
                else: pass  # SL likely first
            elif tp_hit: win_8_24 += 1
            
            # SL=5, TP=15
            sl_hit = fwd_l <= c[i] - 5
            tp_hit = fwd_h >= c[i] + 15
            if sl_hit and tp_hit:
                mid = (h[i+1:i+13].max() + l[i+1:i+13].min()) / 2
                if mid > c[i]: win_5_15 += 1
            elif tp_hit: win_5_15 += 1
            
            # SL=10, TP=30
            sl_hit = fwd_l <= c[i] - 10
            tp_hit = fwd_h >= c[i] + 30
            if sl_hit and tp_hit:
                mid = (h[i+1:i+13].max() + l[i+1:i+13].min()) / 2
                if mid > c[i]: win_10_30 += 1
            elif tp_hit: win_10_30 += 1
        
        if total > 50:
            up_avg = ups / total
            dn_avg = downs / total
            ratio = up_avg / max(dn_avg, 0.01)
            w1 = win_8_24 / max(total, 1) * 100
            w2 = win_5_15 / max(total, 1) * 100
            w3 = win_10_30 / max(total, 1) * 100
            label = f"mom>{mom_thresh:.1f}"
            print(f"BULL {label} r<{rng_max} {total:6d} {up_avg:6.1f} {dn_avg:6.1f} {ratio:7.2f} {w1:7.1f} {w2:7.1f} {w3:7.1f}")

# Test breakout bars (those breaking above 12-bar high)
print(f"\n{'='*60}")
print("BREAKOUT ANALYSIS (high > 12-bar high with momentum)")
print(f"{'dir':>4} {'mom>':>4} {'cnt':>6} {'up':>6} {'dn':>6} {'ratio':>7} {'W@8/24':>7}")
print("-"*45)

for mom_thresh in [0.3, 0.5]:
    for direction in ['bull', 'bear']:
        total = 0; ups = 0; downs = 0; win = 0
        for i in range(lookback, n - lookback):
            body = abs(c[i] - o[i])
            mom = body / (h[i] - l[i] + 0.01)
            hi = h[i-lookback:i+1].max()
            lo = l[i-lookback:i+1].min()
            bullish = c[i] > o[i]
            
            if direction == 'bull':
                if not (bullish and h[i] >= hi and mom > mom_thresh): continue
            else:
                if not (not bullish and l[i] <= lo and mom > mom_thresh): continue
            
            total += 1
            fwd_h = h[i+1:i+13].max()
            fwd_l = l[i+1:i+13].min()
            ups += fwd_h - c[i]
            downs += c[i] - fwd_l
            
            sl_hit = fwd_l <= c[i] - 8
            tp_hit = fwd_h >= c[i] + 24
            if tp_hit and not sl_hit: win += 1
            elif tp_hit and sl_hit:
                mid = (fwd_h + fwd_l) / 2
                if mid > c[i]: win += 1
        
        if total > 20:
            up_avg = ups / total; dn_avg = downs / total
            rat = up_avg / max(dn_avg, 0.01)
            wr_ = win / total * 100
            print(f"{direction:4s} {mom_thresh:4.1f} {total:6d} {up_avg:6.1f} {dn_avg:6.1f} {rat:7.2f} {wr_:7.1f}")

# Test EMA20 touch bars
print(f"\n{'='*60}")
print("EMA20 TOUCH + MOMENTUM ANALYSIS")
print(f"{'dir':>4} {'cond':>12} {'cnt':>6} {'up':>6} {'dn':>6} {'W@8/24':>7}")
print("-"*45)

# EMA20
ema20 = np.full(n, np.nan)
m = 2.0 / 21
ema20[19] = np.mean(c[:20])
for i in range(20, n): ema20[i] = (c[i] - ema20[i-1]) * m + ema20[i-1]

for direction in ['bull', 'bear']:
    total = 0; wins = 0
    for i in range(50, n - lookback):
        if np.isnan(ema20[i]): continue
        
        if direction == 'bull':
            touch = l[i] <= ema20[i] <= c[i]  # touched EMA20
            mom = abs(c[i] - o[i]) / (h[i] - l[i] + 0.01)
            if not (touch and mom > 0.4): continue
        else:
            touch = c[i] <= ema20[i] <= h[i]
            mom = abs(c[i] - o[i]) / (h[i] - l[i] + 0.01)
            if not (touch and mom > 0.4): continue
        
        total += 1
        fwd_h = h[i+1:i+13].max()
        fwd_l = l[i+1:i+13].min()
        
        sl_hit = fwd_l <= c[i] - 8
        tp_hit = fwd_h >= c[i] + 24
        if direction == 'bear':
            sl_hit, tp_hit = tp_hit, sl_hit
            tp_hit = fwd_l <= c[i] - 24
            sl_hit = fwd_h >= c[i] + 8
        
        if tp_hit and not sl_hit: wins += 1
        elif tp_hit and sl_hit:
            mid = (fwd_h + fwd_l) / 2
            if (direction == 'bull' and mid > c[i]) or (direction == 'bear' and mid < c[i]):
                wins += 1
    
    if total > 10:
        wr_ = wins / total * 100
        fwd_h_avg = np.mean([h[i+1:i+13].max() for i in range(50, n-lookback) if ...])
        print(f"{direction:4s} touch+enter {total:6d} {wr_:7.1f}")

# Now let's do a comprehensive grid search for the BEST entry conditions
print(f"\n{'='*60}")
print("GRID SEARCH: Best Entry Conditions")
print(f"{'look':>4} {'rng<':>4} {'mom>':>4} {'entry':>10} {'cnt':>5} {'W8/24':>6} {'W5/15':>6}")
print("-"*45)

results = []
for lb in [8, 12, 16, 20]:
    for rmax in [15, 20, 25, 30]:
        for mth in [0.3, 0.5, 0.7]:
            for entry in ['close_above_high', 'high_above_high', 'close_in_top', 'any_bull']:
                total = 0; win8 = 0; win5 = 0
                for i in range(lb, n - 12):
                    body = abs(c[i] - o[i])
                    mom = body / (h[i] - l[i] + 0.01)
                    hi = h[i-lb:i+1].max()
                    rng = hi - l[i-lb:i+1].min()
                    
                    if not (c[i] > o[i] and mom > mth and rng < rmax): continue
                    
                    # Different entry definitions
                    if entry == 'close_above_high' and not (c[i] > hi): continue
                    if entry == 'high_above_high' and not (h[i] > hi): continue
                    if entry == 'close_in_top' and not (c[i] > hi - hi*0.003): continue
                    # any_bull = no extra condition
                    
                    total += 1
                    fwd_h = h[i+1:i+13].max()
                    fwd_l = l[i+1:i+13].min()
                    
                    for sl, tp, w_arr in [(8, 24, 'win8'), (5, 15, 'win5')]:
                        sl_hit = fwd_l <= c[i] - sl
                        tp_hit = fwd_h >= c[i] + tp
                        hit = tp_hit and not sl_hit
                        if tp_hit and sl_hit:
                            mid = (fwd_h + fwd_l) / 2
                            hit = mid > c[i]
                        if hit:
                            if sl == 8: win8 += 1
                            else: win5 += 1
                
                if total > 20:
                    results.append((lb, rmax, mth, entry, total, win8/total*100, win5/total*100))

results.sort(key=lambda x: x[5], reverse=True)
for r in results[:20]:
    print(f"{r[0]:4d} {r[1]:4d} {r[2]:4.1f} {r[3]:10s} {r[4]:5d} {r[5]:6.1f} {r[6]:6.1f}")

mt5.shutdown()
