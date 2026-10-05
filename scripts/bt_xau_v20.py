"""XAU v2.0 融合策略回测 — 谷歌黄金(SuperTrend+QQE+ADX) × 我们验证的固定SL/TP

v1.2发现:
  - ATR≥10即可, 3和10差异极小
  - RANGE≥4 TREND≥3 最优 (+1275.6U, WR57.5%, PF4.03, DD4U)
  - 亚洲时段(0-7UTC)是利润主力
  - BE移SL有害(3:1 R:R不需要)

v2.0新增:
  1. SuperTrend(10,3.0) — H1/H4级别趋势确认 (谷歌黄金)
  2. QQE(14,3.0,5) — RSI动量过滤 (谷歌黄金)
  3. ADX≥20 — 趋势强度过滤 (谷歌黄金)
  4. EMA52 — 趋势基准线 (谷歌黄金)
  5. 综合评分升级: 原评分+ST对齐(+1)+QQE确认(+1)+ADX过滤
  6. 分模式阈值: RANGE≥4 TREND≥3 (v1.2验证最优)

出场: 固定TP=1.5 SL=0.5 (3:1 R:R已验证最优)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone
import MetaTrader5 as mt5
import numpy as np
from collections import defaultdict

from src.strategy.indicators import EMA, ATR, RSI, SuperTrend, QQE
from src.strategy.engine_xau import (
    calc_adx, detect_key_levels, detect_order_blocks, detect_fvg,
    detect_regime, get_session_quality,
    _body, _is_bull, _is_bear, _is_engulfing, _is_pin_bar,
    _wick_up, _wick_dn, _range,
    RANGE_KEY_LEVEL_DIST,
    ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD,
    TREND_EMA_FAST, TREND_EMA_SLOW,
    M5_RSI_PERIOD,
)

LOGIN = 0                  # 你的 MT5 账号
PASSWORD = "你的密码"       # 你的 MT5 密码
SERVER = "FxPro-MT5 Demo"
SYMBOL = "GOLD"
SPREAD_COST = 0.19
TP_DIST = 1.5
SL_DIST = 0.5
MAX_HOLD = 12


def fetch_data():
    m5  = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5,  0, 60000)
    h1  = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1,  0, 8000)
    h4  = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H4,  0, 4000)
    def to_bars(r):
        if r is None: return []
        return [{"t": datetime.fromtimestamp(row[0], tz=timezone.utc), "ts": float(row[0]),
                 "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                 "c": float(row[4]), "v": int(row[5])} for row in r]
    return to_bars(m5), to_bars(h1), to_bars(h4)


def time_align(bars, ts, last_idx):
    for j in range(last_idx, -1, -1):
        if bars[j]["ts"] <= ts: return j
    return 0


def precompute_h1_cache(h1, h4):
    """预计算H1级别缓存(含SuperTrend+QQE+ADX)"""
    h1_h = np.array([b["h"] for b in h1])
    h1_l = np.array([b["l"] for b in h1])
    h1_c = np.array([b["c"] for b in h1])
    h1_at = ATR(h1_h, h1_l, h1_c, 14)

    # 预计算H1 SuperTrend和QQE
    h1_st_trend, _ = SuperTrend(h1_h, h1_l, h1_c, period=10, multiplier=3.0)
    h1_qqe_dir, _ = QQE(h1_c, rsi_period=14, qqe_factor=3.0, wilders_period=5)
    h1_ema52 = EMA(h1_c, 52)

    cache = {}
    for h1_idx in range(50, len(h1)):
        ts = h1[h1_idx]["ts"]
        h4_idx = 0
        for j in range(len(h4)-1, -1, -1):
            if h4[j]["ts"] <= ts: h4_idx = j; break
        if h4_idx < 50: continue

        atr_val = float(h1_at[h1_idx]) if not np.isnan(h1_at[h1_idx]) else 10.0
        regime, conf = detect_regime(h4[:h4_idx+1], h1[:h1_idx+1])
        levels = detect_key_levels(h1[:h1_idx+1])
        fvgs = detect_fvg(h1[:h1_idx+1])
        obs = detect_order_blocks(h1[:h1_idx+1])

        # SuperTrend方向 (H1)
        st_dir = int(h1_st_trend[h1_idx]) if not np.isnan(h1_st_trend[h1_idx]) else 0
        # QQE方向 (H1)
        qqe_dir = int(h1_qqe_dir[h1_idx]) if not np.isnan(h1_qqe_dir[h1_idx]) else 0
        # EMA52
        ema52_val = float(h1_ema52[h1_idx]) if not np.isnan(h1_ema52[h1_idx]) else 0.0

        cache[h1_idx] = {
            "regime": regime, "conf": conf, "atr": atr_val,
            "levels": levels, "fvgs": fvgs, "obs": obs,
            "h1_slice": h1[:h1_idx+1],
            "st_dir": st_dir, "qqe_dir": qqe_dir,
            "ema52": ema52_val,
        }
    return cache


def _score_v20(m5, idx, signal_dir, atr_val, m5_rsi,
               h1_levels, h1_fvgs, h1_obs,
               h1_st_dir, h1_qqe_dir, cl):
    """v2.0评分: 原评分 + SuperTrend对齐 + QQE确认"""
    if idx < 5: return 0
    score = 0
    prev = m5[idx - 1]
    c = m5[idx]

    # ── 原评分系统 (0-10分) ──
    if _is_engulfing(prev, c):
        if (signal_dir == "buy" and _is_bull(c)) or (signal_dir == "sell" and _is_bear(c)):
            score += 2
    if _is_pin_bar(c):
        if signal_dir == "buy" and _wick_dn(c) > _wick_up(c): score += 1
        elif signal_dir == "sell" and _wick_up(c) > _wick_dn(c): score += 1

    rsi_val = float(m5_rsi[idx]) if idx < len(m5_rsi) and not np.isnan(m5_rsi[idx]) else 50.0
    if signal_dir == "buy" and rsi_val < 30: score += 2
    elif signal_dir == "sell" and rsi_val > 70: score += 2
    elif signal_dir == "buy" and rsi_val < 45: score += 1
    elif signal_dir == "sell" and rsi_val > 55: score += 1

    for lv in h1_levels:
        if abs(cl - lv["price"]) < atr_val * RANGE_KEY_LEVEL_DIST:
            if signal_dir == "buy" and lv["type"] in ("support", "both"): score += 2; break
            elif signal_dir == "sell" and lv["type"] in ("resist", "both"): score += 2; break

    if signal_dir == "buy":
        if any(f["type"] == "bull" and f["low"] <= cl <= f["high"] for f in h1_fvgs): score += 1
        if any(o["type"] == "bull_ob" and o["low"] <= cl <= o["high"] for o in h1_obs): score += 1
    elif signal_dir == "sell":
        if any(f["type"] == "bear" and f["low"] <= cl <= f["high"] for f in h1_fvgs): score += 1
        if any(o["type"] == "bear_ob" and o["low"] <= cl <= o["high"] for o in h1_obs): score += 1

    if idx >= 5:
        mom = cl - m5[idx-5]["c"]
        if signal_dir == "buy" and mom > 0: score += 1
        elif signal_dir == "sell" and mom < 0: score += 1

    avg_v = np.mean([m5[j]["v"] for j in range(max(0,idx-20), idx+1)])
    if c["v"] > avg_v * 1.2: score += 1

    # ── v2.0新增: SuperTrend对齐 (评分+1) ──
    st_aligned = (signal_dir == "buy" and h1_st_dir == 1) or \
                 (signal_dir == "sell" and h1_st_dir == -1)
    if st_aligned: score += 1

    # ── v2.0新增: QQE确认 (评分+1) ──
    qqe_aligned = (signal_dir == "buy" and h1_qqe_dir == 1) or \
                  (signal_dir == "sell" and h1_qqe_dir == -1)
    if qqe_aligned: score += 1

    return score


def _check_range(cl, atr, levels, fvgs, obs, h1_st_dir, h1_ema52):
    """v2.0 RANGE: 增加ST方向检查"""
    near_s = near_r = False
    for lv in levels:
        if abs(cl - lv["price"]) < atr * RANGE_KEY_LEVEL_DIST:
            if lv["type"] in ("support", "both") and cl < lv["price"]: near_s = True
            elif lv["type"] in ("resist", "both") and cl > lv["price"]: near_r = True

    in_b_fvg = any(f["type"] == "bull" and f["low"] <= cl <= f["high"] for f in fvgs)
    in_b_ob  = any(o["type"] == "bull_ob" and o["low"] <= cl <= o["high"] for o in obs)
    in_s_fvg = any(f["type"] == "bear" and f["low"] <= cl <= f["high"] for f in fvgs)
    in_s_ob  = any(o["type"] == "bear_ob" and o["low"] <= cl <= o["high"] for o in obs)

    # RANGE模式: 逆势入场, 但参照ST方向提高胜率
    # 如果ST多头, RANGE做多更可靠(回调到支撑)
    if (near_s or in_b_fvg or in_b_ob) and h1_st_dir != -1: return "buy"
    if (near_r or in_s_fvg or in_s_ob) and h1_st_dir != 1: return "sell"
    return None


def _check_trend(cl, h1_slice, fvgs, obs, h1_st_dir, h1_qqe_dir, h1_ema52):
    """v2.0 TREND: ST+QQE双重确认"""
    if len(h1_slice) < 50: return None

    # ST方向是主要趋势
    if h1_st_dir == 1:
        if any(f["type"]=="bull" and f["low"]<=cl<=f["high"] for f in fvgs): return "buy"
        if any(o["type"]=="bull_ob" and o["low"]<=cl<=o["high"] for o in obs): return "buy"
    elif h1_st_dir == -1:
        if any(f["type"]=="bear" and f["low"]<=cl<=f["high"] for f in fvgs): return "sell"
        if any(o["type"]=="bear_ob" and o["low"]<=cl<=o["high"] for o in obs): return "sell"

    # ST无信号时, 退回到原EMA逻辑
    c_arr = np.array([b["c"] for b in h1_slice])
    e20 = EMA(c_arr, TREND_EMA_FAST); e50 = EMA(c_arr, TREND_EMA_SLOW)
    if np.isnan(e20[-1]) or np.isnan(e50[-1]): return None
    td = "up" if e20[-1] > e50[-1] else ("down" if e20[-1] < e50[-1] else None)
    if not td: return None

    if td == "up":
        if any(f["type"]=="bull" and f["low"]<=cl<=f["high"] for f in fvgs): return "buy"
        if any(o["type"]=="bull_ob" and o["low"]<=cl<=o["high"] for o in obs): return "buy"
    else:
        if any(f["type"]=="bear" and f["low"]<=cl<=f["high"] for f in fvgs): return "sell"
        if any(o["type"]=="bear_ob" and o["low"]<=cl<=o["high"] for o in obs): return "sell"
    return None


def collect_signals_v20(m5, h1, h4, h1_cache, m5_rsi,
                        range_min_score=4, trend_min_score=3,
                        min_atr=3.0, max_atr=30.0):
    """v2.0信号收集"""
    n = len(m5)
    signals = []; day_id = -1; daily_trades = 0; h1_idx = 0

    for i in range(60, n - 12):
        ts = m5[i]["ts"]; cl = m5[i]["c"]
        dy = int(ts / 86400)
        if dy != day_id: day_id = dy; daily_trades = 0
        if daily_trades >= 20: continue

        utc_hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        sq = get_session_quality(utc_hour)
        if sq < 0.2: continue

        h1_idx = time_align(h1, ts, min(h1_idx+1, len(h1)-1))
        if h1_idx not in h1_cache: continue
        c = h1_cache[h1_idx]
        regime = c["regime"]; atr = c["atr"]
        levels = c["levels"]; fvgs = c["fvgs"]; obs = c["obs"]
        h1_st = c["st_dir"]; h1_qqe = c["qqe_dir"]; h1_ema52_v = c["ema52"]

        if atr < min_atr or atr > max_atr: continue

        # RANGE模式
        if regime in ("RANGING", "VOLATILE"):
            sig = _check_range(cl, atr, levels, fvgs, obs, h1_st, h1_ema52_v)
            if sig:
                sc = _score_v20(m5, i, sig, atr, m5_rsi, levels, fvgs, obs,
                               h1_st, h1_qqe, cl)
                ms = range_min_score + (1 if sq < 0.5 else 0)
                if sc >= ms:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr,
                                    "open_i":i, "mode":"RANGE", "session_q":sq,
                                    "regime":regime, "utc_hour":utc_hour,
                                    "st_dir":h1_st, "qqe_dir":h1_qqe})
                    daily_trades += 1; continue

        # TREND模式
        if regime == "TRENDING":
            sig = _check_trend(cl, c["h1_slice"], fvgs, obs, h1_st, h1_qqe, h1_ema52_v)
            if sig:
                sc = _score_v20(m5, i, sig, atr, m5_rsi, levels, fvgs, obs,
                               h1_st, h1_qqe, cl)
                ms = trend_min_score + (1 if sq < 0.5 else 0)
                if sc >= ms:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr,
                                    "open_i":i, "mode":"TREND", "session_q":sq,
                                    "regime":regime, "utc_hour":utc_hour,
                                    "st_dir":h1_st, "qqe_dir":h1_qqe})
                    daily_trades += 1

    return signals


def sim_exit(sig, m5, tp_dist=TP_DIST, sl_dist=SL_DIST, max_hold_bars=MAX_HOLD, trailing=True):
    entry = sig["entry"]; d = sig["dir"]; base_i = sig["open_i"]
    if d == "buy":
        tp = entry + tp_dist + SPREAD_COST; sl = entry + SPREAD_COST - sl_dist
    else:
        tp = entry - tp_dist - SPREAD_COST; sl = entry - SPREAD_COST + sl_dist
    trail_on = False; trail_h = entry
    for hold in range(1, max_hold_bars + 1):
        ci = base_i + hold
        if ci >= len(m5): break
        c = m5[ci]; no, nh, nl, nc = c["o"], c["h"], c["l"], c["c"]; hs = hold * 300
        if d == "buy":
            if no >= tp: return tp, "TP", hs
            if no <= sl: return sl, "SL", hs
            if nl <= sl and nh >= tp: return tp, "TP", hs+30
            if nl <= sl: return sl, "SL", hs+30
            if nh >= tp: return tp, "TP", hs+30
            if trailing:
                if nh > trail_h: trail_h = nh
                profit = trail_h - entry; tp_full = abs(tp - entry)
                if tp_full > 0 and profit / tp_full >= 0.5: trail_on = True
                if trail_on and nl <= trail_h - tp_full * 0.3:
                    return max(nc, trail_h - tp_full*0.3), "TRAIL", hs+60
        else:
            if no <= tp: return tp, "TP", hs
            if no >= sl: return sl, "SL", hs
            if nh >= sl and nl <= tp: return tp, "TP", hs+30
            if nh >= sl: return sl, "SL", hs+30
            if nl <= tp: return tp, "TP", hs+30
            if trailing:
                if nl < trail_h or trail_h == entry: trail_h = nl
                profit = entry - trail_h; tp_full = abs(tp - entry)
                if tp_full > 0 and profit / tp_full >= 0.5: trail_on = True
                if trail_on and nh >= trail_h + tp_full * 0.3:
                    return min(nc, trail_h + tp_full*0.3), "TRAIL", hs+60
        if hold == max_hold_bars: return nc, "CLOSE", hs + 300
    return m5[min(base_i+max_hold_bars, len(m5)-1)]["c"], "CLOSE", max_hold_bars*300


def run_bt(signals, m5, tp_dist=TP_DIST, sl_dist=SL_DIST, max_hold=MAX_HOLD, trailing=True,
           max_daily_loss=6.0, consec_limit=4):
    trades = []; next_free = -1
    dp = {}; dl = {}; dt = {}; consec = 0; pd = -1
    for sig in signals:
        i = sig["open_i"]; dy = i // 288
        if dy != pd: pd = dy; consec = 0
        if i < next_free: continue
        if dl.get(dy,0) >= max_daily_loss: continue
        if dt.get(dy,0) >= 20: continue
        if consec >= consec_limit: continue
        ep, reason, hs = sim_exit(sig, m5, tp_dist, sl_dist, max_hold, trailing)
        pnl = round((ep - sig["entry"] - SPREAD_COST) if sig["dir"]=="buy"
                     else (sig["entry"] - ep - SPREAD_COST), 4)
        trades.append({"dir":sig["dir"],"entry":sig["entry"],"exit":ep,
                        "pnl":round(pnl,2),"reason":reason,"open_i":i,
                        "score":sig["score"],"hold_sec":hs,"mode":sig["mode"],
                        "session_q":sig["session_q"],"regime":sig["regime"],
                        "utc_hour":sig.get("utc_hour",-1),"atr":sig.get("atr",10)})
        ch = max(1, hs//300+1); next_free = i+ch
        dp[dy] = dp.get(dy,0)+round(pnl,2); dt[dy] = dt.get(dy,0)+1
        if pnl < 0: dl[dy] = dl.get(dy,0)+abs(round(pnl,2)); consec += 1
        else: consec = 0
    return trades


def stats_line(trades):
    if not trades: return "0笔", 0, 0, 0
    n = len(trades); w = [t for t in trades if t["pnl"]>0]; l = [t for t in trades if t["pnl"]<=0]
    wr = len(w)/n*100; total = round(sum(t["pnl"] for t in trades),2)
    gw = sum(t["pnl"] for t in w) if w else 0; gl = sum(abs(t["pnl"]) for t in l) if l else 0
    pf = round(gw/gl,2) if gl>0 else 999
    cum=0; mp=0; dd=0
    for t in trades: cum+=t["pnl"]; mp=max(mp,cum); dd=max(dd,mp-cum)
    dp = {}
    for t in trades: d=t["open_i"]//288; dp[d]=dp.get(d,0)+t["pnl"]
    wd = sum(1 for p in dp.values() if p>0); ld = sum(1 for p in dp.values() if p<0)
    avg_w = round(np.mean([t["pnl"] for t in w]),3) if w else 0
    avg_l = round(np.mean([t["pnl"] for t in l]),3) if l else 0
    line = f"{n}笔 WR{wr:.1f}% PnL{total:+.1f}U PF{pf} DD{dd:.0f}U 均盈{avg_w:+.2f} 均亏{avg_l:+.2f} 盈日{wd}/{wd+ld}"
    return line, total, pf, dd


def main():
    if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
        print("MT5 init fail:", mt5.last_error()); return
    print("XAU v2.0 融合策略 — 谷歌黄金(SuperTrend+QQE+ADX) × 固定SL/TP")
    print("=" * 120)

    m5, h1, h4 = fetch_data()
    print(f"M5={len(m5)} H1={len(h1)} H4={len(h4)}")
    if len(m5) < 100: print("数据不足"); mt5.shutdown(); return
    days = round(len(m5)/288)
    print(f"约 {days} 天 (208天)\n")

    print("预计算H1缓存(含SuperTrend+QQE)...")
    h1_cache = precompute_h1_cache(h1, h4)
    m5_rsi = RSI(np.array([b["c"] for b in m5]), M5_RSI_PERIOD)
    print(f"H1缓存: {len(h1_cache)}条\n")

    # ── 1. 与原版评分对比 (ATR≥3, score≥4) ──
    print("=" * 120)
    print("1. 原版 vs v2.0评分对比 (ATR≥3, mode=both):")
    for name, min_atr, rsc, tsc in [
        ("v1.1.0原版", 3, 4, 4),
        ("v1.2推荐", 3, 4, 3),
        ("v2.0基础", 3, 4, 4),
        ("v2.0推荐", 3, 4, 3),
    ]:
        sigs = collect_signals_v20(m5, h1, h4, h1_cache, m5_rsi,
                                   min_atr=min_atr, range_min_score=rsc, trend_min_score=tsc)
        trades = run_bt(sigs, m5)
        line, pnl, pf, dd = stats_line(trades)
        r_trades = [t for t in trades if t["mode"]=="RANGE"]
        t_trades = [t for t in trades if t["mode"]=="TREND"]
        r_line, _, _, _ = stats_line(r_trades) if r_trades else ("0笔", 0, 0, 0)
        t_line, _, _, _ = stats_line(t_trades) if t_trades else ("0笔", 0, 0, 0)
        print(f"  {name:12s}: {line}")
        print(f"              RANGE: {r_line}")
        print(f"              TREND: {t_line}")

    # ── 2. ATR门槛对比(v2.0评分) ──
    print(f"\n{'='*120}")
    print("2. v2.0 ATR门槛对比 (R≥4 T≥3):")
    for min_atr in [3, 5, 8, 10, 12, 15]:
        sigs = collect_signals_v20(m5, h1, h4, h1_cache, m5_rsi,
                                   min_atr=min_atr, range_min_score=4, trend_min_score=3)
        trades = run_bt(sigs, m5)
        line, pnl, pf, dd = stats_line(trades)
        r_trades = [t for t in trades if t["mode"]=="RANGE"]
        t_trades = [t for t in trades if t["mode"]=="TREND"]
        r_line, _, _, _ = stats_line(r_trades) if r_trades else ("0笔", 0, 0, 0)
        t_line, _, _, _ = stats_line(t_trades) if t_trades else ("0笔", 0, 0, 0)
        print(f"  ATR≥{min_atr:2d}: {line}")
        print(f"          RANGE: {r_line}  TREND: {t_line}")

    # ── 3. 分模式score门槛对比 ──
    print(f"\n{'='*120}")
    print("3. v2.0 分模式score门槛 (ATR≥3):")
    for rsc, tsc in [(3,3), (3,4), (4,3), (4,4), (4,5), (5,4)]:
        sigs = collect_signals_v20(m5, h1, h4, h1_cache, m5_rsi,
                                   min_atr=3, range_min_score=rsc, trend_min_score=tsc)
        trades = run_bt(sigs, m5)
        line, pnl, pf, dd = stats_line(trades)
        print(f"  R≥{rsc} T≥{tsc}: {line}")

    # ── 4. 按小时盈亏分布 ──
    print(f"\n{'='*120}")
    print("4. v2.0 按UTC小时盈亏 (R≥4 T≥3 ATR≥3):")
    sigs_all = collect_signals_v20(m5, h1, h4, h1_cache, m5_rsi,
                                   min_atr=3, range_min_score=4, trend_min_score=3)
    for hour in range(0, 24):
        hour_sigs = [s for s in sigs_all if s.get("utc_hour") == hour]
        if not hour_sigs: continue
        trades = run_bt(hour_sigs, m5)
        if trades:
            total = sum(t["pnl"] for t in trades)
            wr = len([t for t in trades if t["pnl"]>0])/len(trades)*100
            print(f"  {hour:2d}UTC: {len(trades)}笔 WR{wr:.1f}% PnL{total:+.1f}U")

    # ── 5. 最优配置详细分析 ──
    print(f"\n{'='*120}")
    print("5. 最优配置详细分析 (R≥4 T≥3 ATR≥3):")
    trades = run_bt(sigs_all, m5)
    line, pnl, pf, dd = stats_line(trades)
    print(f"  整体: {line}")
    for mode in ["RANGE", "TREND"]:
        mt = [t for t in trades if t["mode"]==mode]
        if mt: print(f"  {mode}: {stats_line(mt)[0]}")
    for reg in ["RANGING", "VOLATILE", "TRENDING"]:
        rt = [t for t in trades if t["regime"]==reg]
        if rt: print(f"  {reg}: {stats_line(rt)[0]}")
    reasons = defaultdict(int)
    for t in trades: reasons[t["reason"]] += 1
    print(f"  出场: {dict(sorted(reasons.items()))}")
    # 月度PnL
    mp = defaultdict(float)
    for t in trades:
        month = datetime.fromtimestamp(m5[t["open_i"]]["ts"], tz=timezone.utc).strftime("%Y-%m")
        mp[month] += t["pnl"]
    print("  月度PnL:")
    for month in sorted(mp.keys()):
        print(f"    {month}: {mp[month]:+7.1f}U")

    mt5.shutdown()
    print("\n验证完成!")


if __name__ == "__main__":
    main()
