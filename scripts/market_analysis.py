"""XAUUSD 市场特征深度分析
- 各时段波动率/交易量
- 日内波动模式
- 各时段点差
- 趋势/震荡比例
- 关键价位回测
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5
import numpy as np
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── 连接FxPro Demo (GOLD, 点差0.23) ──
LOGIN = 0                  # 你的 MT5 账号
PASSWORD = "你的密码"       # 你的 MT5 密码
SERVER = "FxPro-MT5 Demo"

if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
    print("MT5连接失败:", mt5.last_error())
    exit(1)

print("MT5连接成功:", SERVER)

SYMBOL = "GOLD"

# ── 1. 基础信息 ──
info = mt5.symbol_info(SYMBOL)
tick = mt5.symbol_info_tick(SYMBOL)
if info and tick:
    spread = tick.ask - tick.bid
    print(f"\n{'='*80}")
    print(f"品种: {SYMBOL}")
    print(f"当前价: Bid={tick.bid} Ask={tick.ask}")
    print(f"点差: {spread:.2f} ({spread:.2f}U/0.01手)")
    print(f"Digits: {info.digits} Point: {info.point}")

# ── 2. 获取多周期数据 ──
print(f"\n{'='*80}")
print("获取历史数据...")

m1_raw = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, 100000)
m5_raw = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 50000)
m15_raw = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 30000)
h1_raw = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 10000)
h4_raw = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H4, 0, 5000)
d1_raw = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_D1, 0, 500)

print(f"M1: {len(m1_raw) if m1_raw is not None else 0}根")
print(f"M5: {len(m5_raw) if m5_raw is not None else 0}根")
print(f"M15: {len(m15_raw) if m15_raw is not None else 0}根")
print(f"H1: {len(h1_raw) if h1_raw is not None else 0}根")
print(f"H4: {len(h4_raw) if h4_raw is not None else 0}根")
print(f"D1: {len(d1_raw) if d1_raw is not None else 0}根")

# ── 3. 日内波动分析 (按UTC小时) ──
print(f"\n{'='*80}")
print("=== 日内波动分析 (按UTC小时) ===")

if m1_raw is not None and len(m1_raw) > 0:
    # 按小时统计: 波动(H-L), 交易量, 涨跌幅
    hour_stats = defaultdict(lambda: {"ranges": [], "volumes": [], "moves": [], "count": 0})
    
    for row in m1_raw:
        dt = datetime.fromtimestamp(row[0], tz=timezone.utc)
        h = dt.hour
        rng = float(row[2]) - float(row[3])  # H - L
        vol = int(row[5])
        move = float(row[4]) - float(row[1])  # C - O
        hour_stats[h]["ranges"].append(rng)
        hour_stats[h]["volumes"].append(vol)
        hour_stats[h]["moves"].append(move)
        hour_stats[h]["count"] += 1
    
    print(f"{'UTC时':>5} {'根数':>6} {'平均波动':>8} {'中位波动':>8} {'平均量':>8} {'偏多%':>6}")
    print("-" * 55)
    for h in sorted(hour_stats.keys()):
        s = hour_stats[h]
        avg_r = np.mean(s["ranges"])
        med_r = np.median(s["ranges"])
        avg_v = np.mean(s["volumes"])
        bull_pct = sum(1 for m in s["moves"] if m > 0) / len(s["moves"]) * 100
        print(f"{h:5d} {s['count']:6d} {avg_r:8.2f} {med_r:8.2f} {avg_v:8.0f} {bull_pct:6.1f}")

# ── 4. 按交易时段分析 ──
print(f"\n{'='*80}")
print("=== 交易时段分析 ===")
# 亚洲: 00-07 UTC, 伦敦: 07-16 UTC, 纽约: 13-21 UTC, 伦敦-纽约重叠: 13-16 UTC

sessions = {
    "亚洲(00-07)": (0, 7),
    "伦敦(07-16)": (7, 16),
    "纽约(13-21)": (13, 21),
    "伦纽重叠(13-16)": (13, 16),
    "纽约午后(16-21)": (16, 21),
}

if m1_raw is not None and len(m1_raw) > 0:
    for name, (start_h, end_h) in sessions.items():
        ranges = []
        volumes = []
        for row in m1_raw:
            dt = datetime.fromtimestamp(row[0], tz=timezone.utc)
            if start_h <= dt.hour < end_h:
                rng = float(row[2]) - float(row[3])
                vol = int(row[5])
                ranges.append(rng)
                volumes.append(vol)
        if ranges:
            print(f"  {name:20s}: {len(ranges):6d}根 平均波动={np.mean(ranges):.2f} "
                  f"中位={np.median(ranges):.2f} 最大={np.max(ranges):.2f} 平均量={np.mean(volumes):.0f}")

# ── 5. H1 K线波动分析 ──
print(f"\n{'='*80}")
print("=== H1 K线波动统计 ===")
if h1_raw is not None and len(h1_raw) > 0:
    h1_ranges = [float(row[2]) - float(row[3]) for row in h1_raw]
    h1_bodies = [abs(float(row[4]) - float(row[1])) for row in h1_raw]
    print(f"  总根数: {len(h1_raw)}")
    print(f"  H1波动: 平均={np.mean(h1_ranges):.2f} 中位={np.median(h1_ranges):.2f} "
          f"P25={np.percentile(h1_ranges,25):.2f} P75={np.percentile(h1_ranges,75):.2f} "
          f"P90={np.percentile(h1_ranges,90):.2f} P95={np.percentile(h1_ranges,95):.2f}")
    print(f"  H1实体: 平均={np.mean(h1_bodies):.2f} 中位={np.median(h1_bodies):.2f}")
    
    # 按小时统计H1波动
    h1_hour_ranges = defaultdict(list)
    for row in h1_raw:
        dt = datetime.fromtimestamp(row[0], tz=timezone.utc)
        rng = float(row[2]) - float(row[3])
        h1_hour_ranges[dt.hour].append(rng)
    
    print(f"\n  H1按UTC小时波动:")
    print(f"  {'UTC时':>5} {'平均':>6} {'中位':>6} {'P75':>6} {'P95':>6}")
    print("  " + "-" * 35)
    for h in sorted(h1_hour_ranges.keys()):
        r = h1_hour_ranges[h]
        print(f"  {h:5d} {np.mean(r):6.2f} {np.median(r):6.2f} {np.percentile(r,75):6.2f} {np.percentile(r,95):6.2f}")

# ── 6. D1 日波幅统计 ──
print(f"\n{'='*80}")
print("=== D1 日波幅统计 ===")
if d1_raw is not None and len(d1_raw) > 0:
    d1_ranges = [float(row[2]) - float(row[3]) for row in d1_raw]
    d1_moves = [float(row[4]) - float(row[1]) for row in d1_raw]
    print(f"  天数: {len(d1_raw)}")
    print(f"  日波幅: 平均={np.mean(d1_ranges):.2f} 中位={np.median(d1_ranges):.2f} "
          f"最小={np.min(d1_ranges):.2f} 最大={np.max(d1_ranges):.2f}")
    print(f"  日涨跌: 平均={np.mean(d1_moves):.2f} 标准差={np.std(d1_moves):.2f}")
    print(f"  偏多日: {sum(1 for m in d1_moves if m>0)/len(d1_moves)*100:.1f}%")
    
    # 连续涨/跌统计
    streak_up = 0; streak_dn = 0; max_up = 0; max_dn = 0
    for m in d1_moves:
        if m > 0:
            streak_up += 1; streak_dn = 0
            max_up = max(max_up, streak_up)
        else:
            streak_dn += 1; streak_up = 0
            max_dn = max(max_dn, streak_dn)
    print(f"  最长连涨: {max_up}天 最长连跌: {max_dn}天")

# ── 7. 100U本金风险分析 ──
print(f"\n{'='*80}")
print("=== 100U本金风险分析 (0.01手 GOLD) ===")
print(f"  1价格点 = 1U (0.01手)")
print(f"  100U = 100价格点最大回撤")
print(f"  点差 {spread:.2f} = {spread:.2f}U成本/笔")
if d1_raw is not None and len(d1_raw) > 0:
    avg_daily_range = np.mean(d1_ranges)
    print(f"  日均波幅 {avg_daily_range:.2f}点 = {avg_daily_range:.2f}U")
    print(f"  2%风险(2U) = 2.0点SL (日均波幅的{2.0/avg_daily_range*100:.1f}%)")
    print(f"  3%风险(3U) = 3.0点SL (日均波幅的{3.0/avg_daily_range*100:.1f}%)")
    print(f"  5%风险(5U) = 5.0点SL (日均波幅的{5.0/avg_daily_range*100:.1f}%)")
    print(f"  点差占2%风险SL: {spread/2.0*100:.0f}%")
    print(f"  点差占3%风险SL: {spread/3.0*100:.0f}%")

# ── 8. 趋势vs震荡分析 (H1) ──
print(f"\n{'='*80}")
print("=== H1 趋势/震荡分析 ===")
if h1_raw is not None and len(h1_raw) > 100:
    # 用ADX思路: 连续同方向K线 vs 反转
    h1_dirs = [1 if float(row[4]) > float(row[1]) else -1 for row in h1_raw]
    
    # 连续同方向段
    runs = []; cur_dir = h1_dirs[0]; cur_len = 1
    for d in h1_dirs[1:]:
        if d == cur_dir:
            cur_len += 1
        else:
            runs.append((cur_dir, cur_len))
            cur_dir = d; cur_len = 1
    runs.append((cur_dir, cur_len))
    
    up_runs = [r[1] for r in runs if r[0] == 1]
    dn_runs = [r[1] for r in runs if r[0] == -1]
    
    print(f"  上涨段: {len(up_runs)}次 平均长度={np.mean(up_runs):.1f}根H1 中位={np.median(up_runs):.1f}")
    print(f"  下跌段: {len(dn_runs)}次 平均长度={np.mean(dn_runs):.1f}根H1 中位={np.median(dn_runs):.1f}")
    
    # 趋势段(连续>=3根同向) vs 震荡段(1-2根反复)
    trend_count = sum(1 for r in runs if r[1] >= 3)
    range_count = sum(1 for r in runs if r[1] <= 2)
    print(f"  趋势段(≥3根同向): {trend_count}次 ({trend_count/len(runs)*100:.1f}%)")
    print(f"  震荡段(≤2根反复): {range_count}次 ({range_count/len(runs)*100:.1f}%)")

# ── 9. M15 关键模式统计 ──
print(f"\n{'='*80}")
print("=== M15 关键模式频率 ===")
if m15_raw is not None and len(m15_raw) > 100:
    # 吞没, Pin Bar, 内包, Doji
    engulf_count = 0; pin_count = 0; ib_count = 0; doji_count = 0
    for i in range(1, len(m15_raw)):
        prev_o, prev_h, prev_l, prev_c = float(m15_raw[i-1][1]), float(m15_raw[i-1][2]), float(m15_raw[i-1][3]), float(m15_raw[i-1][4])
        curr_o, curr_h, curr_l, curr_c = float(m15_raw[i][1]), float(m15_raw[i][2]), float(m15_raw[i][3]), float(m15_raw[i][4])
        
        prev_body = abs(prev_c - prev_o)
        curr_body = abs(curr_c - curr_o)
        curr_range = curr_h - curr_l
        
        # 吞没
        if curr_body > prev_body and prev_body > 0:
            if curr_c > curr_o and prev_c < prev_o and curr_c > prev_o and curr_o < prev_c:
                engulf_count += 1
            elif curr_c < curr_o and prev_c > prev_o and curr_c < prev_o and curr_o > prev_c:
                engulf_count += 1
        
        # Pin bar (长下影或长上影)
        if curr_range > 0:
            lower_wick = min(curr_o, curr_c) - curr_l
            upper_wick = curr_h - max(curr_o, curr_c)
            if lower_wick / curr_range > 0.6 or upper_wick / curr_range > 0.6:
                pin_count += 1
        
        # 内包
        if curr_h <= prev_h and curr_l >= prev_l:
            ib_count += 1
        
        # Doji
        if curr_range > 0 and curr_body / curr_range < 0.1:
            doji_count += 1
    
    total = len(m15_raw) - 1
    print(f"  M15总根数: {total+1}")
    print(f"  吞没形态: {engulf_count}次 ({engulf_count/total*100:.2f}%)")
    print(f"  Pin Bar: {pin_count}次 ({pin_count/total*100:.2f}%)")
    print(f"  内包形态: {ib_count}次 ({ib_count/total*100:.2f}%)")
    print(f"  Doji: {doji_count}次 ({doji_count/total*100:.2f}%)")

mt5.shutdown()
print(f"\n{'='*80}")
print("分析完成!")
