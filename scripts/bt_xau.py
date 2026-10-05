"""XAU 终极策略回测 v2 — 固定SL/TP参数网格搜索

v1发现: ATR动态SL被MAX_RISK_PCT截断到3点, 但H1 ATR=10-15, SL远低于正常波动
v2方案: 固定SL/TP + XAU框架入场筛选 + 多参数网格

三层架构保留:
  H4: 市场状态 (ADX + EMA排列)
  H1: 市场结构 (关键位, Order Block, FVG)
  M5: 入场时机 (形态, RSI, 量确认)

出场改为固定:
  SL/TP: 固定价格点, 适配100U本金
  跟踪止盈: 盈利达50%TP → 跟踪, 回撤30%TP平仓
  超时: N根M5强平
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

# ── 连接FxPro Demo ──
LOGIN = 0                  # 你的 MT5 账号
PASSWORD = "你的密码"       # 你的 MT5 密码
SERVER = "FxPro-MT5 Demo"

SYMBOL = "GOLD"
SPREAD_COST = 0.19


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
    """预计算H1级别的regime/结构"""
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
    """快速M5评分"""
    if idx < 5: return 0, "x"
    score = 0; details = []
    c = m5[idx]; prev = m5[idx - 1]; cl = c["c"]

    # K线形态
    if _is_engulfing(prev, c):
        if (signal_dir == "buy" and _is_bull(c)) or (signal_dir == "sell" and _is_bear(c)):
            score += 2; details.append("eng")
    if _is_pin_bar(c):
        if signal_dir == "buy" and _wick_dn(c) > _wick_up(c):
            score += 1; details.append("pin_b")
        elif signal_dir == "sell" and _wick_up(c) > _wick_dn(c):
            score += 1; details.append("pin_s")

    # RSI
    rsi_val = float(m5_rsi[idx]) if idx < len(m5_rsi) and not np.isnan(m5_rsi[idx]) else 50.0
    if signal_dir == "buy" and rsi_val < 30: score += 2; details.append(f"rsi_os")
    elif signal_dir == "sell" and rsi_val > 70: score += 2; details.append(f"rsi_ob")
    elif signal_dir == "buy" and rsi_val < 45: score += 1; details.append(f"rsi_lo")
    elif signal_dir == "sell" and rsi_val > 55: score += 1; details.append(f"rsi_hi")

    # 关键位
    for lv in h1_levels:
        if abs(cl - lv["price"]) < atr_val * RANGE_KEY_LEVEL_DIST:
            if signal_dir == "buy" and lv["type"] in ("support", "both"):
                score += 2; details.append("key_s"); break
            elif signal_dir == "sell" and lv["type"] in ("resist", "both"):
                score += 2; details.append("key_r"); break

    # FVG/OB
    if signal_dir == "buy":
        if any(f["type"] == "bull" and f["low"] <= cl <= f["high"] for f in h1_fvgs): score += 1; details.append("fvg")
        if any(o["type"] == "bull_ob" and o["low"] <= cl <= o["high"] for o in h1_obs): score += 1; details.append("ob")
    elif signal_dir == "sell":
        if any(f["type"] == "bear" and f["low"] <= cl <= f["high"] for f in h1_fvgs): score += 1; details.append("fvg")
        if any(o["type"] == "bear_ob" and o["low"] <= cl <= o["high"] for o in h1_obs): score += 1; details.append("ob")

    # 动量
    if idx >= 5:
        mom = cl - m5[idx-5]["c"]
        if signal_dir == "buy" and mom > 0: score += 1; details.append("mom+")
        elif signal_dir == "sell" and mom < 0: score += 1; details.append("mom-")

    # 量
    avg_v = np.mean([m5[j]["v"] for j in range(max(0,idx-20), idx+1)])
    if c["v"] > avg_v * 1.2: score += 1; details.append("vol+")

    return score, ",".join(details) if details else "w"


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


def collect_signals(m5, h1, h4, min_score=4, modes="both"):
    """收集入场信号 (固定SL/TP版, modes: both/range/trend)"""
    n = len(m5)
    if n < 60: return []

    print("  预计算H1结构缓存...")
    h1_cache = precompute_h1_cache(h1, h4)
    print(f"  H1缓存: {len(h1_cache)}个条目")

    m5_rsi = RSI(np.array([b["c"] for b in m5]), M5_RSI_PERIOD)

    signals = []
    day_id = -1; daily_trades = 0
    h1_idx = 0

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

        # ATR过滤 (3-30点)
        if atr < 3.0 or atr > 30.0: continue

        # RANGE模式
        if modes in ("both", "range") and regime in ("RANGING", "VOLATILE"):
            sig = _check_range(cl, atr, levels, fvgs, obs)
            if sig:
                sc, det = _score_fast(m5, i, sig, atr, m5_rsi, levels, fvgs, obs)
                ms = min_score + (1 if sq < 0.5 else 0)  # 次要时段+1分
                if sc >= ms:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr,
                                    "open_i":i, "mode":"RANGE", "session_q":sq, "regime":regime})
                    daily_trades += 1; continue

        # TREND模式
        if modes in ("both", "trend") and regime == "TRENDING":
            sig = _check_trend(cl, c["h1_slice"], fvgs, obs)
            if sig:
                sc, det = _score_fast(m5, i, sig, atr, m5_rsi, levels, fvgs, obs)
                ms = min_score + (1 if sq < 0.5 else 0)
                if sc >= ms:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr,
                                    "open_i":i, "mode":"TREND", "session_q":sq, "regime":regime})
                    daily_trades += 1

    return signals


def sim_exit(sig, m5, tp_dist, sl_dist, max_hold_bars=12, trailing=True):
    """出场模拟: 固定SL/TP + 跟踪止盈"""
    entry = sig["entry"]; d = sig["dir"]; base_i = sig["open_i"]

    if d == "buy":
        tp = entry + tp_dist + SPREAD_COST
        sl = entry + SPREAD_COST - sl_dist
    else:
        tp = entry - tp_dist - SPREAD_COST
        sl = entry - SPREAD_COST + sl_dist

    trail_on = False; trail_h = entry

    for hold in range(1, max_hold_bars + 1):
        ci = base_i + hold
        if ci >= len(m5): break
        c = m5[ci]
        no, nh, nl, nc = c["o"], c["h"], c["l"], c["c"]
        hs = hold * 300

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

        if hold == max_hold_bars:
            return nc, "CLOSE", hs + 300

    return m5[min(base_i+max_hold_bars, len(m5)-1)]["c"], "CLOSE", max_hold_bars*300


def run_bt(signals, m5, tp_dist, sl_dist, max_hold=12, trailing=True,
           max_daily_loss=6.0, consec_limit=4):
    """运行回测"""
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
                        "session_q":sig["session_q"],"regime":sig["regime"]})

        ch = max(1, hs//300+1); next_free = i+ch
        dp[dy] = dp.get(dy,0)+round(pnl,2); dt[dy] = dt.get(dy,0)+1
        if pnl < 0: dl[dy] = dl.get(dy,0)+abs(round(pnl,2)); consec += 1
        else: consec = 0

    return trades


def quick_stats(trades):
    """单行统计"""
    if not trades: return "0笔"
    n = len(trades); w = [t for t in trades if t["pnl"]>0]; l = [t for t in trades if t["pnl"]<=0]
    wr = len(w)/n*100; total = round(sum(t["pnl"] for t in trades),2)
    gw = sum(t["pnl"] for t in w) if w else 0; gl = sum(abs(t["pnl"]) for t in l) if l else 0
    pf = round(gw/gl,2) if gl>0 else 999
    dp = {}
    for t in trades: d=t["open_i"]//288; dp[d]=dp.get(d,0)+t["pnl"]
    wd = sum(1 for p in dp.values() if p>0); ld = sum(1 for p in dp.values() if p<0)
    # max DD
    cum=0; mp=0; dd=0
    for t in trades: cum+=t["pnl"]; mp=max(mp,cum); dd=max(dd,mp-cum)
    return f"{n}笔 WR{wr:.1f}% PnL{total:+.1f}U PF{pf} 盈日{wd}/{wd+ld} DD{dd:.0f}U"


def main():
    if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
        print("MT5 init fail:", mt5.last_error()); return
    print("XAU v2 回测 — 固定SL/TP参数网格搜索")
    print(f"{SYMBOL} | 0.01手 | 点差{SPREAD_COST} | 100U本金")
    print("=" * 120)

    m5, h1, h4 = fetch_data()
    print(f"M5={len(m5)} H1={len(h1)} H4={len(h4)}")
    if len(m5) < 100: print("数据不足"); mt5.shutdown(); return
    days = round(len(m5)/288)
    print(f"约 {days} 天\n")

    # ── 参数网格 ──
    tp_list  = [0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    sl_list  = [0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 3.0]
    hold_list = [6, 12, 24]  # M5根: 30min, 1h, 2h
    score_list = [3, 4, 5]
    mode_list = ["both", "trend", "range"]

    # 先收集所有信号 (score_min=3, 最宽松)
    print("收集入场信号 (score_min=3)...")
    signals_all = collect_signals(m5, h1, h4, min_score=3, modes="both")
    print(f"总信号: {len(signals_all)}个\n")

    # ── 1. TP × SL × Hold 网格 (固定score≥4, mode=both) ──
    print("=" * 120)
    print("TP × SL × Hold 网格 (score≥4, mode=both, trailing=on):")
    sigs4 = [s for s in signals_all if s["score"] >= 4]
    print(f"  score≥4信号: {len(sigs4)}个\n")

    best_pnl = -999; best_params = None
    print(f"{'TP':>4} {'SL':>4} {'Hold':>5} | 结果")
    print("-" * 100)
    for tp in tp_list:
        for sl in sl_list:
            if tp <= sl: continue  # TP必须>SL
            for hold in hold_list:
                trades = run_bt(sigs4, m5, tp, sl, hold, trailing=True)
                s = quick_stats(trades)
                pnl = sum(t["pnl"] for t in trades)
                if pnl > best_pnl:
                    best_pnl = pnl; best_params = (tp, sl, hold)
                print(f"{tp:4.1f} {sl:4.1f} {hold*5:5d}m | {s}")

    print(f"\n最优: TP={best_params[0]} SL={best_params[1]} Hold={best_params[2]*5}min → PnL{best_pnl:+.1f}U")

    # ── 2. Score过滤对比 (用最优TP/SL/Hold) ──
    tp, sl, hold = best_params
    print(f"\n{'='*120}")
    print(f"Score过滤对比 (TP={tp} SL={sl} Hold={hold*5}min):")
    for sc in [3, 4, 5, 6]:
        sigs = [s for s in signals_all if s["score"] >= sc]
        trades = run_bt(sigs, m5, tp, sl, hold)
        print(f"  score≥{sc}: {len(sigs)}信号 → {quick_stats(trades)}")

    # ── 3. Mode对比 ──
    print(f"\n{'='*120}")
    print(f"Mode对比 (TP={tp} SL={sl} Hold={hold*5}min, score≥4):")
    for mode in ["both", "trend", "range"]:
        sigs_mode = [s for s in signals_all if s["score"]>=4 and
                     (mode=="both" or s["mode"].lower()==mode.upper())]
        trades = run_bt(sigs_mode, m5, tp, sl, hold)
        r_trades = [t for t in trades if t["mode"]=="RANGE"]
        t_trades = [t for t in trades if t["mode"]=="TREND"]
        print(f"  {mode:5s}: {quick_stats(trades)}")
        if r_trades:
            print(f"         RANGE: {quick_stats(r_trades)}")
        if t_trades:
            print(f"         TREND: {quick_stats(t_trades)}")

    # ── 4. Trailing vs No-Trailing ──
    print(f"\n{'='*120}")
    print(f"Trailing对比 (TP={tp} SL={sl} Hold={hold*5}min, score≥4):")
    sigs4 = [s for s in signals_all if s["score"]>=4]
    for trail in [True, False]:
        trades = run_bt(sigs4, m5, tp, sl, hold, trailing=trail)
        label = "Trailing" if trail else "NoTrail"
        print(f"  {label:8s}: {quick_stats(trades)}")
        reasons = defaultdict(int)
        for t in trades: reasons[t["reason"]] += 1
        print(f"           出场: {dict(reasons)}")

    # ── 5. 仅TREND模式详细 ──
    print(f"\n{'='*120}")
    print(f"仅TREND模式详细分析 (TP={tp} SL={sl} Hold={hold*5}min):")
    sigs_trend = [s for s in signals_all if s["score"]>=4 and s["mode"]=="TREND"]
    trades = run_bt(sigs_trend, m5, tp, sl, hold)
    if trades:
        total = sum(t["pnl"] for t in trades)
        wr = len([t for t in trades if t["pnl"]>0])/len(trades)*100
        print(f"  {quick_stats(trades)}")
        # 按session
        for lbl, lo, hi in [("主(13-17)",0.8,1.1),("辅(8-12)",0.6,0.8),("其他",0,0.6)]:
            st = [t for t in trades if lo<=t["session_q"]<hi]
            if st: print(f"    {lbl}: {quick_stats(st)}")
        # 按regime
        for reg in ["RANGING","TRENDING","VOLATILE"]:
            st = [t for t in trades if t["regime"]==reg]
            if st: print(f"    {reg}: {quick_stats(st)}")

    mt5.shutdown()
    print("\n回测完成!")


if __name__ == "__main__":
    main()
