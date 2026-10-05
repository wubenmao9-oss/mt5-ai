"""XAU v3 回测 — 精细化分析 + 增强策略验证

分析维度:
  1. 按小时盈亏 → 精确时段优化
  2. 按评分盈亏 → 门槛优化
  3. 按regime+mode → 组合过滤
  4. 连亏后表现 → 动态门槛
  5. 移SL到成本价 → 保护效果
  6. 动量过滤 → M5动量+H1趋势方向对齐
  7. ATR区域过滤 → 低/高ATR分别优化SL/TP
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

# 最优参数 (v2验证)
TP_DIST = 1.5
SL_DIST = 0.5
MAX_HOLD = 12  # M5 bars = 60min
MIN_SCORE = 4


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
    if idx < 5: return 0, "x"
    score = 0; details = []
    c = m5[idx]; prev = m5[idx - 1]; cl = c["c"]

    if _is_engulfing(prev, c):
        if (signal_dir == "buy" and _is_bull(c)) or (signal_dir == "sell" and _is_bear(c)):
            score += 2; details.append("eng")
    if _is_pin_bar(c):
        if signal_dir == "buy" and _wick_dn(c) > _wick_up(c):
            score += 1; details.append("pin_b")
        elif signal_dir == "sell" and _wick_up(c) > _wick_dn(c):
            score += 1; details.append("pin_s")

    rsi_val = float(m5_rsi[idx]) if idx < len(m5_rsi) and not np.isnan(m5_rsi[idx]) else 50.0
    if signal_dir == "buy" and rsi_val < 30: score += 2; details.append("rsi_os")
    elif signal_dir == "sell" and rsi_val > 70: score += 2; details.append("rsi_ob")
    elif signal_dir == "buy" and rsi_val < 45: score += 1; details.append("rsi_lo")
    elif signal_dir == "sell" and rsi_val > 55: score += 1; details.append("rsi_hi")

    for lv in h1_levels:
        if abs(cl - lv["price"]) < atr_val * RANGE_KEY_LEVEL_DIST:
            if signal_dir == "buy" and lv["type"] in ("support", "both"):
                score += 2; details.append("key_s"); break
            elif signal_dir == "sell" and lv["type"] in ("resist", "both"):
                score += 2; details.append("key_r"); break

    if signal_dir == "buy":
        if any(f["type"] == "bull" and f["low"] <= cl <= f["high"] for f in h1_fvgs): score += 1; details.append("fvg")
        if any(o["type"] == "bull_ob" and o["low"] <= cl <= o["high"] for o in h1_obs): score += 1; details.append("ob")
    elif signal_dir == "sell":
        if any(f["type"] == "bear" and f["low"] <= cl <= f["high"] for f in h1_fvgs): score += 1; details.append("fvg")
        if any(o["type"] == "bear_ob" and o["low"] <= cl <= o["high"] for o in h1_obs): score += 1; details.append("ob")

    if idx >= 5:
        mom = cl - m5[idx-5]["c"]
        if signal_dir == "buy" and mom > 0: score += 1; details.append("mom+")
        elif signal_dir == "sell" and mom < 0: score += 1; details.append("mom-")

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


def collect_signals(m5, h1, h4, min_score=3, modes="both"):
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

        if atr < 3.0 or atr > 30.0: continue

        # RANGE模式
        if modes in ("both", "range") and regime in ("RANGING", "VOLATILE"):
            sig = _check_range(cl, atr, levels, fvgs, obs)
            if sig:
                sc, det = _score_fast(m5, i, sig, atr, m5_rsi, levels, fvgs, obs)
                if sc >= min_score:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr,
                                    "open_i":i, "mode":"RANGE", "session_q":sq,
                                    "regime":regime, "utc_hour":utc_hour,
                                    "session_min":sq + (0.1 if 15<=utc_hour<17 else 0)})
                    daily_trades += 1; continue

        # TREND模式
        if modes in ("both", "trend") and regime == "TRENDING":
            sig = _check_trend(cl, c["h1_slice"], fvgs, obs)
            if sig:
                sc, det = _score_fast(m5, i, sig, atr, m5_rsi, levels, fvgs, obs)
                if sc >= min_score:
                    signals.append({"dir":sig, "entry":cl, "score":sc, "atr":atr,
                                    "open_i":i, "mode":"TREND", "session_q":sq,
                                    "regime":regime, "utc_hour":utc_hour,
                                    "session_min":sq + (0.1 if 15<=utc_hour<17 else 0)})
                    daily_trades += 1

    return signals


def sim_exit(sig, m5, tp_dist, sl_dist, max_hold_bars=12, trailing=True, breakeven_pct=0.5):
    """出场模拟: 固定SL/TP + 跟踪止盈 + 移SL到成本价"""
    entry = sig["entry"]; d = sig["dir"]; base_i = sig["open_i"]

    if d == "buy":
        tp = entry + tp_dist + SPREAD_COST
        sl = entry + SPREAD_COST - sl_dist
    else:
        tp = entry - tp_dist - SPREAD_COST
        sl = entry - SPREAD_COST + sl_dist

    trail_on = False; trail_h = entry
    be_moved = False  # 是否已移SL到成本价

    for hold in range(1, max_hold_bars + 1):
        ci = base_i + hold
        if ci >= len(m5): break
        c = m5[ci]
        no, nh, nl, nc = c["o"], c["h"], c["l"], c["c"]
        hs = hold * 300

        if d == "buy":
            # 移极移SL: 盈利达breakeven_pct*TP → SL移到成本价
            if not be_moved and nh > entry + SPREAD_COST + tp_dist * breakeven_pct:
                sl = entry + SPREAD_COST + 0.05  # 成本价+微小缓冲
                be_moved = True

            if no >= tp: return tp, "TP", hs
            if no <= sl: return sl, "SL" if not be_moved else "BE", hs
            if nl <= sl and nh >= tp: return tp, "TP", hs+30
            if nl <= sl: return sl, "SL" if not be_moved else "BE", hs+30
            if nh >= tp: return tp, "TP", hs+30
            if trailing:
                if nh > trail_h: trail_h = nh
                profit = trail_h - entry; tp_full = abs(tp - entry)
                if tp_full > 0 and profit / tp_full >= 0.5: trail_on = True
                if trail_on and nl <= trail_h - tp_full * 0.3:
                    return max(nc, trail_h - tp_full*0.3), "TRAIL", hs+60
        else:
            if not be_moved and nl < entry - SPREAD_COST - tp_dist * breakeven_pct:
                sl = entry - SPREAD_COST - 0.05
                be_moved = True

            if no <= tp: return tp, "TP", hs
            if no >= sl: return sl, "SL" if not be_moved else "BE", hs
            if nh >= sl and nl <= tp: return tp, "TP", hs+30
            if nh >= sl: return sl, "SL" if not be_moved else "BE", hs+30
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


def run_bt(signals, m5, tp_dist=TP_DIST, sl_dist=SL_DIST, max_hold=MAX_HOLD,
           trailing=True, breakeven_pct=0.5,
           max_daily_loss=6.0, consec_limit=4,
           filter_fn=None):
    """运行回测, filter_fn可选过滤函数"""
    trades = []; next_free = -1
    dp = {}; dl = {}; dt = {}; consec = 0; pd = -1

    for sig in signals:
        if filter_fn and not filter_fn(sig):
            continue
        i = sig["open_i"]; dy = i // 288
        if dy != pd: pd = dy; consec = 0
        if i < next_free: continue
        if dl.get(dy,0) >= max_daily_loss: continue
        if dt.get(dy,0) >= 20: continue
        if consec >= consec_limit: continue

        ep, reason, hs = sim_exit(sig, m5, tp_dist, sl_dist, max_hold, trailing, breakeven_pct)
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


def stats(trades, label=""):
    if not trades: return f"{label}0笔"
    n = len(trades); w = [t for t in trades if t["pnl"]>0]; l = [t for t in trades if t["pnl"]<=0]
    wr = len(w)/n*100; total = round(sum(t["pnl"] for t in trades),2)
    gw = sum(t["pnl"] for t in w) if w else 0; gl = sum(abs(t["pnl"]) for t in l) if l else 0
    pf = round(gw/gl,2) if gl>0 else 999
    dp = {}
    for t in trades: d=t["open_i"]//288; dp[d]=dp.get(d,0)+t["pnl"]
    wd = sum(1 for p in dp.values() if p>0); ld = sum(1 for p in dp.values() if p<0)
    cum=0; mp=0; dd=0
    for t in trades: cum+=t["pnl"]; mp=max(mp,cum); dd=max(dd,mp-cum)
    avg_w = round(np.mean([t["pnl"] for t in w]),3) if w else 0
    avg_l = round(np.mean([t["pnl"] for t in l]),3) if l else 0
    return f"{label}{n}笔 WR{wr:.1f}% PnL{total:+.1f}U PF{pf} DD{dd:.0f}U 均盈{avg_w:+.2f} 均亏{avg_l:+.2f}"


def main():
    if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
        print("MT5 init fail:", mt5.last_error()); return
    print("XAU v3 精细化分析")
    print(f"{SYMBOL} | TP={TP_DIST} SL={SL_DIST} | 点差{SPREAD_COST}")
    print("=" * 120)

    m5, h1, h4 = fetch_data()
    print(f"M5={len(m5)} H1={len(h1)} H4={len(h4)}")
    if len(m5) < 100: print("数据不足"); mt5.shutdown(); return
    days = round(len(m5)/288)
    print(f"约 {days} 天\n")

    # 收集所有信号
    print("收集入场信号 (score≥3)...")
    signals_all = collect_signals(m5, h1, h4, min_score=3, modes="both")
    print(f"总信号: {len(signals_all)}个\n")

    # ── 基线: v1.1.0结果 ──
    sigs4 = [s for s in signals_all if s["score"] >= 4]
    base_trades = run_bt(sigs4, m5, breakeven_pct=999)  # 不移SL
    print("=" * 120)
    print("基线 (v1.1.0, 无移SL, 无跟踪):")
    print(stats(base_trades))

    # ── 1. 按小时盈亏分布 ──
    print(f"\n{'='*120}")
    print("1. 按UTC小时盈亏分布:")
    base_sigs = sigs4
    for hour in range(0, 24):
        hour_sigs = [s for s in base_sigs if s.get("utc_hour") == hour]
        if not hour_sigs: continue
        trades = run_bt(hour_sigs, m5, breakeven_pct=999)
        if trades:
            total = sum(t["pnl"] for t in trades)
            wr = len([t for t in trades if t["pnl"]>0])/len(trades)*100
            sq = get_session_quality(hour)
            print(f"  {hour:2d}UTC (q={sq:.1f}): {len(trades)}笔 WR{wr:.1f}% PnL{total:+.1f}U")

    # ── 2. 按评分盈亏 ──
    print(f"\n{'='*120}")
    print("2. 按评分盈亏分布:")
    for sc in range(3, 9):
        exact_sigs = [s for s in signals_all if s["score"] == sc]
        if not exact_sigs: continue
        trades = run_bt(exact_sigs, m5, breakeven_pct=999)
        if trades:
            total = sum(t["pnl"] for t in trades)
            wr = len([t for t in trades if t["pnl"]>0])/len(trades)*100
            print(f"  score={sc}: {len(trades)}笔 WR{wr:.1f}% PnL{total:+.1f}U")

    # ── 3. 按regime+mode组合 ──
    print(f"\n{'='*120}")
    print("3. 按regime+mode组合:")
    for regime in ["RANGING", "VOLATILE", "TRENDING"]:
        for mode in ["RANGE", "TREND"]:
            combo_sigs = [s for s in sigs4 if s["regime"]==regime and s["mode"]==mode]
            if not combo_sigs: continue
            trades = run_bt(combo_sigs, m5, breakeven_pct=999)
            if trades:
                total = sum(t["pnl"] for t in trades)
                wr = len([t for t in trades if t["pnl"]>0])/len(trades)*100
                print(f"  {regime:8s}+{mode:5s}: {len(trades)}笔 WR{wr:.1f}% PnL{total:+.1f}U")

    # ── 4. 移SL到成本价效果 ──
    print(f"\n{'='*120}")
    print("4. 移SL到成本价 (breakeven) 效果:")
    for be_pct in [999, 0.3, 0.4, 0.5, 0.6]:
        trades = run_bt(sigs4, m5, breakeven_pct=be_pct)
        label = f"BE@{be_pct:.0%}" if be_pct < 999 else "NoBE"
        reasons = defaultdict(int)
        for t in trades: reasons[t["reason"]] += 1
        print(f"  {label:8s}: {stats(trades)}  出场:{dict(sorted(reasons.items()))}")

    # ── 5. 时段过滤优化 ──
    print(f"\n{'='*120}")
    print("5. 时段过滤对比:")
    # 去掉UTC 21-7 (深夜+亚洲)
    for desc, hours_keep in [
        ("全天", list(range(24))),
        ("8-21UTC", list(range(8, 21))),
        ("8-17UTC", list(range(8, 17))),
        ("13-21UTC", list(range(13, 21))),
        ("8-12+13-17", list(range(8, 12)) + list(range(13, 17))),
    ]:
        filtered = [s for s in sigs4 if s.get("utc_hour") in hours_keep]
        trades = run_bt(filtered, m5, breakeven_pct=999)
        print(f"  {desc:15s}: {stats(trades)}")

    # ── 6. ATR区间过滤 ──
    print(f"\n{'='*120}")
    print("6. ATR区间分析:")
    for atr_lo, atr_hi in [(3,10), (10,15), (15,20), (20,30)]:
        atr_sigs = [s for s in sigs4 if atr_lo <= s["atr"] < atr_hi]
        if not atr_sigs: continue
        trades = run_bt(atr_sigs, m5, breakeven_pct=999)
        if trades:
            total = sum(t["pnl"] for t in trades)
            wr = len([t for t in trades if t["pnl"]>0])/len(trades)*100
            print(f"  ATR [{atr_lo:2d},{atr_hi:2d}): {len(trades)}笔 WR{wr:.1f}% PnL{total:+.1f}U")

    # ── 7. 综合最优配置 ──
    print(f"\n{'='*120}")
    print("7. 综合最优配置搜索:")

    best_pnl = -999; best_cfg = None
    for be_pct in [999, 0.4, 0.5]:
        for hours_desc, hours_keep in [
            ("全天", list(range(24))),
            ("8-21", list(range(8, 21))),
            ("8-12+13-17", list(range(8, 12)) + list(range(13, 17))),
        ]:
            for min_sc in [4, 5]:
                sigs = [s for s in signals_all if s["score"] >= min_sc and s.get("utc_hour") in hours_keep]
                trades = run_bt(sigs, m5, breakeven_pct=be_pct)
                total = sum(t["pnl"] for t in trades)
                n = len(trades)
                if n < 100: continue  # 太少无意义
                wr = len([t for t in trades if t["pnl"]>0])/n*100
                dd = 0; cum=0; mp=0
                for t in trades: cum+=t["pnl"]; mp=max(mp,cum); dd=max(dd,mp-cum)
                pf_gw = sum(t["pnl"] for t in trades if t["pnl"]>0)
                pf_gl = sum(abs(t["pnl"]) for t in trades if t["pnl"]<=0)
                pf = pf_gw/pf_gl if pf_gl>0 else 999
                cfg = f"BE={'No' if be_pct==999 else f'{be_pct:.0%}'} hrs={hours_desc} sc≥{min_sc}"
                print(f"  {cfg:35s}: {n}笔 WR{wr:.1f}% PnL{total:+.1f}U PF{pf:.2f} DD{dd:.0f}U")
                if total > best_pnl:
                    best_pnl = total; best_cfg = cfg

    print(f"\n最优配置: {best_cfg} → PnL{best_pnl:+.1f}U")

    mt5.shutdown()
    print("\n分析完成!")


if __name__ == "__main__":
    main()
