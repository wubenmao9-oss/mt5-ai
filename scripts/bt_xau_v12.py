"""XAU v1.2 参数验证 — 基于v3分析结论

v3发现:
  - 亚洲时段(0-7UTC)是利润主力, 不应过滤
  - 移SL到BE有害(3:1 R:R下)
  - ATR<10亏损严重, ATR≥20表现最好
  - score=3仍正期望, score=6边际

v1.2优化:
  1. ATR门槛: 3→10 (过滤低波动)
  2. Score: 测试3/4/5在不同ATR区间
  3. RANGING模式: 加严score要求(仅50%WR)
  4. TRENDING模式: 可放宽(56%WR)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone
import MetaTrader5 as mt5
import numpy as np
from collections import defaultdict

from src.strategy.indicators import EMA, ATR, RSI
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
    h1_h = np.array([b["h"] for b in h1])
    h1_l = np.array([b["l"] for b in h1])
    h1_c = np.array([b["c"] for b in h1])
    h1_at = ATR(h1_h, h1_l, h1_c, 14)
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
        cache[h1_idx] = {
            "regime": regime, "conf": conf, "atr": atr_val,
            "levels": levels, "fvgs": fvgs, "obs": obs,
            "h1_slice": h1[:h1_idx+1],
        }
    return cache


def _score_fast(m5, idx, signal_dir, atr_val, m5_rsi, h1_levels, h1_fvgs, h1_obs):
    if idx < 5: return 0
    score = 0; c = m5[idx]; prev = m5[idx - 1]; cl = c["c"]
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
    return score


def _check_range(cl, atr, levels, fvgs, obs):
    near_s = near_r = False
    for lv in levels:
        if abs(cl - lv["price"]) < atr * RANGE_KEY_LEVEL_DIST:
            if lv["type"] in ("support", "both") and cl < lv["price"]: near_s = True
            elif lv["type"] in ("resist", "both") and cl > lv["price"]: near_r = True
    in_b_fvg = any(f["type"] == "bull" and f["low"] <= cl <= f["high"] for f in fvgs)
    in_b_ob  = any(o["type"] == "bull_ob" and o["low"] <= cl <= o["high"] for o in obs)
    in_s_fvg = any(f["type"] == "bear" and f["low"] <= cl <= f["high"] for f in fvgs)
    in_s_ob  = any(o["type"] == "bear_ob" and o["low"] <= cl <= o["high"] for o in obs)
    if near_s or in_b_fvg or in_b_ob: return "buy"
    if near_r or in_s_fvg or in_s_ob: return "sell"
    return None


def _check_trend(cl, h1_slice, fvgs, obs):
    if len(h1_slice) < 50: return None
    c = np.array([b["c"] for b in h1_slice])
    e20 = EMA(c, TREND_EMA_FAST); e50 = EMA(c, TREND_EMA_SLOW)
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


def collect_signals_v12(m5, h1, h4, h1_cache, m5_rsi,
                        min_atr=3.0, max_atr=30.0,
                        range_min_score=3, trend_min_score=3):
    """v1.2信号收集: 分模式score门槛 + ATR过滤"""
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

        if atr < min_atr or atr > max_atr: continue

        # RANGE模式: 用range_min_score
        if regime in ("RANGING", "VOLATILE"):
            sig = _check_range(cl, atr, levels, fvgs, obs)
            if sig:
                sc = _score_fast(m5, i, sig, atr, m5_rsi, levels, fvgs, obs)
                ms = range_min_score + (1 if sq < 0.5 else 0)
                if sc >= ms:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr,
                                    "open_i":i, "mode":"RANGE", "session_q":sq,
                                    "regime":regime, "utc_hour":utc_hour})
                    daily_trades += 1; continue

        # TREND模式: 用trend_min_score
        if regime == "TRENDING":
            sig = _check_trend(cl, c["h1_slice"], fvgs, obs)
            if sig:
                sc = _score_fast(m5, i, sig, atr, m5_rsi, levels, fvgs, obs)
                ms = trend_min_score + (1 if sq < 0.5 else 0)
                if sc >= ms:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr,
                                    "open_i":i, "mode":"TREND", "session_q":sq,
                                    "regime":regime, "utc_hour":utc_hour})
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


def stats_detail(trades):
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
    print("XAU v1.2 参数验证")
    print("=" * 120)

    m5, h1, h4 = fetch_data()
    print(f"M5={len(m5)} H1={len(h1)} H4={len(h4)}")
    if len(m5) < 100: print("数据不足"); mt5.shutdown(); return
    days = round(len(m5)/288)
    print(f"约 {days} 天\n")

    # 预计算缓存(只做一次)
    print("预计算H1缓存...")
    h1_cache = precompute_h1_cache(h1, h4)
    m5_rsi = RSI(np.array([b["c"] for b in m5]), M5_RSI_PERIOD)
    print(f"H1缓存: {len(h1_cache)}条\n")

    # ── 1. ATR门槛对比 (score≥4, 全模式) ──
    print("=" * 120)
    print("1. ATR门槛对比 (score≥4 both):")
    for min_atr in [3, 5, 8, 10, 12, 15]:
        sigs = collect_signals_v12(m5, h1, h4, h1_cache, m5_rsi,
                                   min_atr=min_atr, range_min_score=4, trend_min_score=4)
        trades = run_bt(sigs, m5)
        line, pnl, pf, dd = stats_detail(trades)
        r_trades = [t for t in trades if t["mode"]=="RANGE"]
        t_trades = [t for t in trades if t["mode"]=="TREND"]
        r_line, _, _, _ = stats_detail(r_trades) if r_trades else ("0笔", 0, 0, 0)
        t_line, _, _, _ = stats_detail(t_trades) if t_trades else ("0笔", 0, 0, 0)
        print(f"  ATR≥{min_atr:2d}: {line}")
        print(f"          RANGE: {r_line}  TREND: {t_line}")

    # ── 2. 分模式score门槛 (ATR≥10) ──
    print(f"\n{'='*120}")
    print("2. 分模式score门槛 (ATR≥10):")
    for rsc, tsc in [(3,3), (3,4), (4,3), (4,4), (4,5), (5,3), (5,4), (5,5)]:
        sigs = collect_signals_v12(m5, h1, h4, h1_cache, m5_rsi,
                                   min_atr=10, range_min_score=rsc, trend_min_score=tsc)
        trades = run_bt(sigs, m5)
        line, pnl, pf, dd = stats_detail(trades)
        print(f"  RANGE≥{rsc} TREND≥{tsc}: {line}")

    # ── 3. 最优配置组合搜索 ──
    print(f"\n{'='*120}")
    print("3. 综合最优搜索 (ATR + 分模式score):")
    best = {"pnl": -999, "cfg": "", "dd": 999}
    for min_atr in [8, 10, 12]:
        for rsc, tsc in [(3,3), (3,4), (4,3), (4,4), (5,4)]:
            sigs = collect_signals_v12(m5, h1, h4, h1_cache, m5_rsi,
                                       min_atr=min_atr, range_min_score=rsc, trend_min_score=tsc)
            trades = run_bt(sigs, m5)
            line, pnl, pf, dd = stats_detail(trades)
            if pnl < 200 or len(trades) < 50: continue  # 过滤掉太差的
            cfg = f"ATR≥{min_atr} R≥{rsc} T≥{tsc}"
            print(f"  {cfg:20s}: {line}")
            # 以PnL/DD比作为评分标准 (风险调整收益)
            score = pnl / dd if dd > 0 else pnl
            if pnl > best["pnl"]:
                best = {"pnl": pnl, "cfg": cfg, "dd": dd, "line": line, "score": score}

    print(f"\n最优(PnL): {best['cfg']} → {best['line']}")

    # ── 4. 最优配置详细分析 ──
    # 用ATR≥10, RANGE≥4, TREND≥3 做详细分析
    print(f"\n{'='*120}")
    print("4. 推荐配置详细分析 (ATR≥10, RANGE≥4, TREND≥3):")
    sigs = collect_signals_v12(m5, h1, h4, h1_cache, m5_rsi,
                               min_atr=10, range_min_score=4, trend_min_score=3)
    trades = run_bt(sigs, m5)
    line, pnl, pf, dd = stats_detail(trades)
    print(f"  整体: {line}")

    # 按模式
    for mode in ["RANGE", "TREND"]:
        mt = [t for t in trades if t["mode"]==mode]
        if mt: print(f"  {mode}: {stats_detail(mt)[0]}")

    # 按regime
    for reg in ["RANGING", "VOLATILE", "TRENDING"]:
        rt = [t for t in trades if t["regime"]==reg]
        if rt: print(f"  {reg}: {stats_detail(rt)[0]}")

    # 按出场原因
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
