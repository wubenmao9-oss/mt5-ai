"""XAU v2.1 OOS验证 + 增强版

Nexus v9.20核心理念: 样本内训练 → 样本外验证

方法:
  前140天(2025-09~2026-01) → 参数搜索
  后68天(2026-02~2026-07) → OOS验证最优参数

v2.1新增:
  1. OOS验证框架 (防止过拟合)
  2. 动态仓位: ATR基础的手数计算 (谷歌黄金风格)
  3. 增强跟踪止盈: 更激进地移动止损 (飓风风格)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone
import MetaTrader5 as mt5
import numpy as np
from collections import defaultdict
import json

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

# 动态仓位参数
ACCOUNT_SIZE = 100.0  # 100U本金
RISK_PER_TRADE = 0.02  # 每笔风险2%
MAX_LOT = 0.03
MIN_LOT = 0.01


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


def _score_v1(m5, idx, signal_dir, atr_val, m5_rsi, h1_levels, h1_fvgs, h1_obs):
    """v1评分 (原版, 已验证)"""
    if idx < 5: return 0
    score = 0; c = m5[idx]; prev = m5[idx-1]; cl = c["c"]
    if _is_engulfing(prev, c):
        if (signal_dir == "buy" and _is_bull(c)) or (signal_dir == "sell" and _is_bear(c)): score += 2
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


def collect_signals(m5, h1, h4, h1_cache, m5_rsi, min_score=4, modes="both", min_atr=3.0, max_atr=30.0):
    """信号收集 (可指定min_atr)"""
    n = len(m5); signals = []; day_id = -1; daily_trades = 0; h1_idx = 0
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
        c = h1_cache[h1_idx]; regime = c["regime"]; atr = c["atr"]
        levels = c["levels"]; fvgs = c["fvgs"]; obs = c["obs"]
        if atr < min_atr or atr > max_atr: continue

        if modes in ("both", "range") and regime in ("RANGING", "VOLATILE"):
            sig = _check_range(cl, atr, levels, fvgs, obs)
            if sig:
                sc = _score_v1(m5, i, sig, atr, m5_rsi, levels, fvgs, obs)
                ms = min_score + (1 if sq < 0.5 else 0)
                if sc >= ms:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr, "open_i":i, "mode":"RANGE", "regime":regime})
                    daily_trades += 1; continue
        if modes in ("both", "trend") and regime == "TRENDING":
            sig = _check_trend(cl, c["h1_slice"], fvgs, obs)
            if sig:
                sc = _score_v1(m5, i, sig, atr, m5_rsi, levels, fvgs, obs)
                ms = min_score + (1 if sq < 0.5 else 0)
                if sc >= ms:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr, "open_i":i, "mode":"TREND", "regime":regime})
                    daily_trades += 1
    return signals


def sim_exit_trail_v2(sig, m5, tp_dist, sl_dist, max_hold_bars,
                      trail_after_pct=0.5, trail_dist_pct=0.3):
    """增强出场: 更激进的跟踪止盈 (谷歌黄金风格)"""
    entry = sig["entry"]; d = sig["dir"]; base_i = sig["open_i"]
    if d == "buy":
        tp = entry + tp_dist + SPREAD_COST
        sl = entry + SPREAD_COST - sl_dist
        trail_target = trail_after_pct * tp_dist
        trail_back = trail_dist_pct * tp_dist
    else:
        tp = entry - tp_dist - SPREAD_COST
        sl = entry - SPREAD_COST + sl_dist
        trail_target = trail_after_pct * tp_dist
        trail_back = trail_dist_pct * tp_dist

    trail_active = False; trail_extreme = entry

    for hold in range(1, max_hold_bars + 1):
        ci = base_i + hold
        if ci >= len(m5): break
        c = m5[ci]; no, nh, nl, nc = c["o"], c["h"], c["l"], c["c"]; hs = hold * 300

        if d == "buy":
            if no >= tp: return tp, "TP", hs, 1.0
            if no <= sl: return sl, "SL", hs, 0.0
            if nl <= sl and nh >= tp: return tp, "TP", hs+30, 1.0
            if nl <= sl: return sl, "SL", hs+30, 0.0
            if nh >= tp: return tp, "TP", hs+30, 1.0

            if nh > trail_extreme: trail_extreme = nh
            profit = trail_extreme - entry
            if not trail_active and profit >= trail_target:
                trail_active = True
                sl = entry + SPREAD_COST  # 移到成本价
            if trail_active and nl <= trail_extreme - trail_back:
                exit_price = max(nc, trail_extreme - trail_back)
                pnl_ratio = (exit_price - entry - SPREAD_COST) / tp_dist
                return exit_price, "TRAIL", hs+60, pnl_ratio
        else:
            if no <= tp: return tp, "TP", hs, 1.0
            if no >= sl: return sl, "SL", hs, 0.0
            if nh >= sl and nl <= tp: return tp, "TP", hs+30, 1.0
            if nh >= sl: return sl, "SL", hs+30, 0.0
            if nl <= tp: return tp, "TP", hs+30, 1.0

            if nl < trail_extreme or trail_extreme == entry: trail_extreme = nl
            profit = entry - trail_extreme
            if not trail_active and profit >= trail_target:
                trail_active = True
                sl = entry - SPREAD_COST
            if trail_active and nh >= trail_extreme + trail_back:
                exit_price = min(nc, trail_extreme + trail_back)
                pnl_ratio = (entry - exit_price - SPREAD_COST) / tp_dist
                return exit_price, "TRAIL", hs+60, pnl_ratio

        if hold == max_hold_bars:
            pnl_ratio = (nc - entry - SPREAD_COST) / tp_dist if d == "buy" else (entry - nc - SPREAD_COST) / tp_dist
            return nc, "CLOSE", hs+300, pnl_ratio

    ci = min(base_i+max_hold_bars, len(m5)-1)
    nc = m5[ci]["c"]
    pnl_ratio = (nc - entry - SPREAD_COST) / tp_dist if d == "buy" else (entry - nc - SPREAD_COST) / tp_dist
    return nc, "CLOSE", max_hold_bars*300, pnl_ratio


def run_bt_adaptive(signals, m5, tp_dist, sl_dist, max_hold,
                    trailing=True, max_daily_loss=6.0, consec_limit=4,
                    trail_after_pct=0.5, trail_dist_pct=0.3,
                    adaptive_lot=True):
    """回测(含动态仓位 + 增强跟踪)"""
    trades = []; next_free = -1; dp = {}; dl = {}; dt = {}; consec = 0; pd = -1

    for sig in signals:
        i = sig["open_i"]; dy = i // 288; atr_val = sig.get("atr", 10)
        if dy != pd: pd = dy; consec = 0
        if i < next_free: continue
        if dl.get(dy,0) >= max_daily_loss: continue
        if dt.get(dy,0) >= 20: continue
        if consec >= consec_limit: continue

        # 动态仓位 (谷歌黄金风格)
        if adaptive_lot:
            risk_per_trade = ACCOUNT_SIZE * RISK_PER_TRADE
            # 基于ATR: 低波动→大仓位, 高波动→小仓位
            vol_factor = max(0.5, min(2.0, 12.0 / max(atr_val, 5)))
            lot = min(MAX_LOT, max(MIN_LOT, round((risk_per_trade / sl_dist) * vol_factor * 0.01, 2)))
        else:
            lot = 0.01

        if trailing:
            ep, reason, hs, pnl_ratio = sim_exit_trail_v2(
                sig, m5, tp_dist, sl_dist, max_hold, trail_after_pct, trail_dist_pct)
        else:
            ep, reason, hs, pnl_ratio = sim_exit_trail_v2(
                sig, m5, tp_dist, sl_dist, max_hold, trail_after_pct=1.0, trail_dist_pct=0.5)

        pnl = round((ep - sig["entry"] - SPREAD_COST) * (lot/0.01) if sig["dir"]=="buy"
                     else (sig["entry"] - ep - SPREAD_COST) * (lot/0.01), 4)

        trades.append({"dir":sig["dir"], "entry":sig["entry"], "exit":ep,
                        "pnl":round(pnl,2), "reason":reason, "open_i":i,
                        "score":sig["score"], "hold_sec":hs, "mode":sig["mode"],
                        "regime":sig["regime"], "lot":lot, "pnl_ratio":pnl_ratio})
        ch = max(1, hs//300+1); next_free = i+ch
        dp[dy] = dp.get(dy,0)+round(pnl,2); dt[dy] = dt.get(dy,0)+1
        if pnl < 0: dl[dy] = dl.get(dy,0)+abs(round(pnl,2)); consec += 1
        else: consec = 0
    return trades


def stats_detail(trades, label=""):
    if not trades: return f"{label}: 0笔"
    n = len(trades); w = [t for t in trades if t["pnl"]>0]; l = [t for t in trades if t["pnl"]<=0]
    wr = len(w)/n*100; total = round(sum(t["pnl"] for t in trades),2)
    gw = sum(t["pnl"] for t in w) if w else 0; gl = sum(abs(t["pnl"]) for t in l) if l else 0
    pf = round(gw/gl,2) if gl>0 else 999
    cum=0; mp=0; dd=0
    for t in trades: cum+=t["pnl"]; mp=max(mp,cum); dd=max(dd,mp-cum)
    avg_w = round(np.mean([t["pnl"] for t in w]),3) if w else 0
    avg_l = round(np.mean([t["pnl"] for t in l]),3) if l else 0
    avg_lot = round(np.mean([t.get("lot", 0.01) for t in trades]), 3)
    line = f"{n}笔 WR{wr:.1f}% PnL{total:+7.1f}U PF{pf} DD{dd:.0f}U 均盈{avg_w:+.2f} 均亏{avg_l:+.2f} 均手{avg_lot}"
    if label: line = f"{label:12s}: {line}"
    return line


def main():
    if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
        print("MT5 init fail:", mt5.last_error()); return
    print("XAU v2.1 OOS验证 + 增强版")
    print("Nexus核心理念: 样本内训练 → 样本外验证")
    print("=" * 120)

    m5, h1, h4 = fetch_data()
    print(f"M5={len(m5)} H1={len(h1)} H4={len(h4)}")
    if len(m5) < 100: print("数据不足"); mt5.shutdown(); return
    total_days = round(len(m5)/288)
    print(f"总数据: {total_days}天 (208天)\n")

    print("预计算H1缓存...")
    h1_cache = precompute_h1_cache(h1, h4)
    m5_rsi = RSI(np.array([b["c"] for b in m5]), M5_RSI_PERIOD)
    print(f"H1缓存: {len(h1_cache)}条\n")

    # ── 分数据段: 样本内(前140天) / 样本外(后68天) ──
    split_idx = 140 * 288  # 140天的M5 bar数
    m5_is = m5[:split_idx]; m5_oos = m5[split_idx:]

    # 找到对应的H1和H4边界
    split_ts = m5[split_idx]["ts"] if split_idx < len(m5) else m5[-1]["ts"]

    # 重新计算各段的缓存
    def filter_bars(bars, max_ts):
        return [b for b in bars if b["ts"] <= max_ts]

    h1_is = filter_bars(h1, split_ts)
    h4_is = filter_bars(h4, split_ts)

    # 建各段缓存
    print("计算样本内(IS)缓存 前140天...")
    h1_cache_is = precompute_h1_cache(h1_is, h4_is)
    m5_rsi_is = RSI(np.array([b["c"] for b in m5_is]), M5_RSI_PERIOD)
    print(f"  IS M5={len(m5_is)} H1缓存={len(h1_cache_is)}条")

    print("计算样本外(OOS)缓存 后68天...")
    # OOS需要用完整历史来算指标, 但只交易split之后的部分
    h1_cache_oos = h1_cache  # 复用完整缓存, 但只取split后的信号
    m5_rsi_oos = m5_rsi
    oos_start_idx = split_idx
    print(f"  OOS M5={len(m5_oos)} 起始index={oos_start_idx}")

    # ── 样本内参数搜索 ──
    print("\n" + "="*120)
    print("阶段1: 样本内参数搜索 (前140天)")
    print("="*120)

    results_is = []
    for min_score in [3, 4, 5]:
        for min_atr in [3, 8, 12]:
            sigs = collect_signals(m5_is, h1_is, h4_is, h1_cache_is, m5_rsi_is,
                                   min_score=min_score, modes="both", min_atr=min_atr)
            trades = run_bt_adaptive(sigs, m5_is, TP_DIST, SL_DIST, MAX_HOLD,
                                     trailing=True, adaptive_lot=False)
            line = stats_detail(trades, f"IS S≥{min_score} A≥{min_atr}")
            pnl = sum(t["pnl"] for t in trades)
            dd = 0; cum=0; mp=0
            for t in trades: cum+=t["pnl"]; mp=max(mp,cum); dd=max(dd,mp-cum)
            results_is.append({"min_score": min_score, "min_atr": min_atr,
                               "trades": trades, "pnl": pnl, "dd": dd,
                               "line": line})
            print(line)

    # 选出IS最佳的3个配置
    results_is.sort(key=lambda x: x["pnl"] / max(x["dd"], 1), reverse=True)
    top3 = results_is[:3]
    print(f"\nIS最优3个:")
    for r in top3:
        print(f"  S≥{r['min_score']} A≥{r['min_atr']}: PnL{r['pnl']:+.1f}U DD{r['dd']:.0f}U 评分{r['pnl']/max(r['dd'],1):.1f}")

    # ── 样本外验证 ──
    print("\n" + "="*120)
    print("阶段2: 样本外验证 (后68天)")
    print("="*120)

    for r in top3:
        ms = r['min_score']; ma = r['min_atr']
        sigs = collect_signals(m5, h1, h4, h1_cache_oos, m5_rsi_oos,
                               min_score=ms, modes="both", min_atr=ma)
        # 只取OOS部分的信号
        oos_sigs = [s for s in sigs if s["open_i"] >= oos_start_idx and s["open_i"] < len(m5)]
        # 调整index
        for s in oos_sigs:
            s["open_i"] = s["open_i"] - oos_start_idx

        # 基线 (固定手数0.01)
        trades_base = run_bt_adaptive(oos_sigs, m5_oos, TP_DIST, SL_DIST, MAX_HOLD,
                                      trailing=True, adaptive_lot=False)
        line_base = stats_detail(trades_base, f"OOS S≥{ms} A≥{ma}")
        pnl = sum(t["pnl"] for t in trades_base)
        print(line_base)

        # 动态仓位版
        trades_dyn = run_bt_adaptive(oos_sigs, m5_oos, TP_DIST, SL_DIST, MAX_HOLD,
                                     trailing=True, adaptive_lot=True)
        line_dyn = stats_detail(trades_dyn, f"OOS+动态仓")
        print(f"  {line_dyn}")

    # ── 最终推荐配置的全周期测试 ──
    best_is = top3[0]  # IS最优
    ms = best_is['min_score']; ma = best_is['min_atr']

    print("\n" + "="*120)
    print(f"阶段3: 最优配置全周期测试 (S≥{ms} A≥{ma})")
    print("="*120)

    # 取全部信号
    sigs_all = collect_signals(m5, h1, h4, h1_cache, m5_rsi_oos,
                               min_score=ms, modes="both", min_atr=ma)

    # 测试不同的trailing策略
    for trail_after, trail_back, name in [
        (0.5, 0.3, "标准跟踪"),
        (0.4, 0.2, "激进跟踪"),
        (0.7, 0.5, "保守跟踪"),
        (1.0, 0.5, "无跟踪(仅TP/SL)"),
    ]:
        trades = run_bt_adaptive(sigs_all, m5, TP_DIST, SL_DIST, MAX_HOLD,
                                 trailing=True, adaptive_lot=False,
                                 trail_after_pct=trail_after,
                                 trail_dist_pct=trail_back)
        line = stats_detail(trades, name)
        r_counts = defaultdict(int)
        for t in trades: r_counts[t["reason"]] += 1
        print(f"  {line}  出场:{dict(r_counts)}")

    # 最终推荐配置的详细分析
    print(f"\n{'='*120}")
    print("详细分析 (标准跟踪, 固定手数):")
    trades_final = run_bt_adaptive(sigs_all, m5, TP_DIST, SL_DIST, MAX_HOLD,
                                   trailing=True, adaptive_lot=False,
                                   trail_after_pct=0.5, trail_dist_pct=0.3)
    print(stats_detail(trades_final, "全周期"))

    for mode in ["RANGE", "TREND"]:
        mt = [t for t in trades_final if t["mode"]==mode]
        if mt: print(stats_detail(mt, f"  {mode}"))

    reasons = defaultdict(int)
    for t in trades_final: reasons[t["reason"]] += 1
    print(f"  出场: {dict(sorted(reasons.items()))}")

    # 月度PnL
    mp = defaultdict(float)
    for t in trades_final:
        month = datetime.fromtimestamp(m5[t["open_i"]]["ts"], tz=timezone.utc).strftime("%Y-%m")
        mp[month] += t["pnl"]
    print("  月度PnL:")
    for month in sorted(mp.keys()):
        print(f"    {month}: {mp[month]:+7.1f}U")

    mt5.shutdown()
    print("\n验证完成！")


if __name__ == "__main__":
    main()
