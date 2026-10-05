"""BTP 超单回测 (M1入场, 多参数矩阵, 智能出场, 含点差)

核心: 乏力+发力 → 进场 → 见利就走
  M15: 关键位
  M5:  方向 (EMA20+FVG)
  M1:  入场 (动能枯竭+发力确认)

出场条件:
  1. TP/SL: 见利就走 + 止损 (基于实际成交价, 含点差)
  2. 价格停滞 + 均线缠绕 → 离场 (市场不确定, 落袋为安)
  3. 收反向K线 + 均线稳定 → 离场 (趋势可能反转, 及时撤退)
  4. 最大持仓K线数到达 → 强制平仓

多参数测试: TP × SL × 持仓K线数 × 智能出场
点差模型:
  BUY:  以ASK成交(cl+spread), TP/SL基于ASK, BID触发 → TP需多涨spread, SL少跌spread
  SELL: 以BID成交(cl),     TP/SL基于BID, ASK触发 → TP需多跌spread, SL少涨spread
  每笔PnL扣除spread成本
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone
import MetaTrader5 as mt5
import numpy as np
from src.strategy.indicators import EMA, ATR
from src.strategy.engine_btp import (
    detect_fvg, detect_key_levels, judge_direction,
    detect_exhaustion_strict, _body, _is_bull, _is_bear,
    _wick_up, _wick_dn, _range, _is_engulfing,
)

SYMBOL = "GOLD"
SPREAD_COST = 0.23  # FxPro Demo GOLD点差


def fetch_data():
    r1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, 58000)
    r5 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 26000)
    r15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 18000)

    def to_bars(r):
        if r is None: return []
        return [{"t": datetime.fromtimestamp(row[0], tz=timezone.utc), "ts": float(row[0]),
                 "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                 "c": float(row[4]), "v": int(row[5])} for row in r]
    return to_bars(r1), to_bars(r5), to_bars(r15)


def collect_signals(m1, m5, m15):
    """收集所有入场信号 + 前向K线数据 + 均线缠绕计数"""
    n = len(m1)
    if n < 60: return [], []

    m1_closes = np.array([b["c"] for b in m1])
    m1_highs = np.array([b["h"] for b in m1])
    m1_lows = np.array([b["l"] for b in m1])
    m1_e20 = EMA(m1_closes, 20)
    m1_at = ATR(m1_highs, m1_lows, m1_closes, 14)

    # 预计算均线穿越计数 (最近5根K线内价格穿越EMA20的次数)
    cross_arr = np.zeros(n)
    raw_cross = np.zeros(n)
    for i in range(1, n):
        if not np.isnan(m1_e20[i]) and not np.isnan(m1_e20[i-1]):
            if (m1_closes[i-1] - m1_e20[i-1]) * (m1_closes[i] - m1_e20[i]) < 0:
                raw_cross[i] = 1
    for i in range(5, n):
        cross_arr[i] = sum(raw_cross[i-4:i+1])

    m5_closes = np.array([b["c"] for b in m5]) if m5 else np.array([])
    m5_e20 = EMA(m5_closes, 20) if len(m5_closes) > 20 else np.array([])

    signals = []
    day_id = -1; daily_loss = 0.0; daily_trades = 0; consec_loss = 0

    for i in range(60, n - 4):  # 留4根前向K线
        if np.isnan(m1_e20[i]) or np.isnan(m1_at[i]): continue
        cl = m1_closes[i]; atr_val = float(m1_at[i])
        ts = m1[i]["ts"]

        dy = int(ts / 86400)
        if dy != day_id:
            day_id = dy; daily_loss = 0.0; daily_trades = 0; consec_loss = 0

        if daily_loss >= 6.0: continue
        if daily_trades >= 50: continue
        if consec_loss >= 5: continue

        # M15 关键位
        m15_levels = []
        if len(m15) > 10:
            m15_idx = len(m15) - 1
            for j in range(len(m15)-1, -1, -1):
                if m15[j]["ts"] <= ts: m15_idx = j; break
            m15_levels = detect_key_levels(m15[:m15_idx+1])

        # M5 方向
        m5_dir = "RANGING"; m5_fvgs = []
        if len(m5) > 30 and len(m5_e20) > 0:
            m5_idx = len(m5) - 1
            for j in range(len(m5)-1, -1, -1):
                if m5[j]["ts"] <= ts: m5_idx = j; break
            if not np.isnan(m5_e20[m5_idx]):
                m5_dir = judge_direction(m5, m5_e20, m5_idx)
            m5_fvgs = detect_fvg(m5[:m5_idx+1])

        m1_fvgs = detect_fvg(m1[:i+1])
        signal = detect_exhaustion_strict(m1, i, atr_val)
        if not signal: continue

        # 方向过滤: 仅M5逆势不做
        if m5_dir == "UP" and signal == "sell": continue
        if m5_dir == "DOWN" and signal == "buy": continue

        # 评分
        score = 0
        if m5_dir != "RANGING":
            if (m5_dir == "UP" and signal == "buy") or (m5_dir == "DOWN" and signal == "sell"):
                score += 2
        for fvg in m5_fvgs[-3:]:
            if signal == "buy" and fvg["type"] == "bull": score += 1; break
            if signal == "sell" and fvg["type"] == "bear": score += 1; break
        for fvg in m1_fvgs[-3:]:
            if signal == "buy" and fvg["type"] == "bull": score += 1; break
            if signal == "sell" and fvg["type"] == "bear": score += 1; break
        for lv in m15_levels:
            if signal == "buy" and lv["type"] in ("support","both") and abs(cl-lv["price"]) < atr_val*0.5:
                score += 1; break
            if signal == "sell" and lv["type"] in ("resist","both") and abs(cl-lv["price"]) < atr_val*0.5:
                score += 1; break
        if i >= 1 and _is_engulfing(m1[i-1], m1[i]):
            if (signal == "buy" and _is_bull(m1[i])) or (signal == "sell" and _is_bear(m1[i])):
                score += 1

        # K线止损
        entry_candle = m1[i]
        if signal == "buy":
            candle_sl = entry_candle["l"]
            if _range(entry_candle) > atr_val * 1.5:
                candle_sl = cl - _body(entry_candle) * 0.5
        else:
            candle_sl = entry_candle["h"]
            if _range(entry_candle) > atr_val * 1.5:
                candle_sl = cl + _body(entry_candle) * 0.5

        # 前向3根K线
        fwd = []
        for k in range(1, 4):
            if i + k < n:
                c = m1[i + k]
                fwd.append({"o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]})

        # 第1根前向K线
        nc = m1[i + 1]
        signals.append({
            "dir": signal, "entry": cl, "score": score,
            "candle_sl": candle_sl, "atr": atr_val,
            "no": nc["o"], "nh": nc["h"], "nl": nc["l"], "nc": nc["c"],
            "fwd": fwd,
            "open_i": i,
            "ema_cross": int(cross_arr[i]),
        })
        daily_trades += 1

    return signals, cross_arr


def sim_exit(sig, tp_dist, sl_price, max_hold, smart_exit, cross_arr):
    """多K线剥头皮出场模拟

    每根K线依次检查:
      1. 开盘有利 → TP (开盘即走)
      2. 开盘跳空到止损 → SL
      3. 两者都触及 → TP先到 (剥头皮速度优先)
      4. 只触止损 → SL
      5. 只到止盈 → TP
      6. 智能出场:
         a. 价格停滞(小实体) + 均线缠绕(穿越>=2) → 离场
         b. 反向K线 + 均线稳定(穿越<=1) → 离场
      7. 最后一根 → 收盘平
    """
    entry = sig["entry"]
    d = sig["dir"]
    atr = sig["atr"]
    base_i = sig["open_i"]

    for hold in range(max_hold):
        # 获取当前检查的K线
        if hold == 0:
            candle = {"o": sig["no"], "h": sig["nh"], "l": sig["nl"], "c": sig["nc"]}
        else:
            if hold - 1 >= len(sig["fwd"]):
                break
            candle = sig["fwd"][hold - 1]

        ci = base_i + hold + 1
        crosses = int(cross_arr[ci]) if ci < len(cross_arr) else 0
        no, nh, nl, nc = candle["o"], candle["h"], candle["l"], candle["c"]
        body = abs(nc - no)
        hold_sec = hold * 60

        if d == "buy":
            # BUY以ASK成交, TP/SL基于ASK, BID触发
            # TP_BID = entry + tp_dist + spread (BID需涨到ASK+tp_dist)
            # SL_BID = sl_price (已在run_combo中调整为entry+spread-sl_val)
            tp = entry + tp_dist + SPREAD_COST

            # 1-2. 开盘检查
            if no >= tp:
                return no, "TP", hold_sec
            if no <= sl_price:
                return no, "SL", hold_sec

            # 3-5. TP/SL检查
            if nl <= sl_price and nh >= tp:
                return tp, "TP", hold_sec + 5
            if nl <= sl_price:
                return sl_price, "SL", hold_sec + 5
            if nh >= tp:
                return tp, "TP", hold_sec + 5

            # 6. 智能出场
            if smart_exit:
                # 价格停滞 + 均线缠绕
                if body < atr * 0.2 and crosses >= 2:
                    return nc, "EXIT_S", hold_sec + 30
                # 反向K线 + 均线稳定
                if nc < no and body > 0.02 and crosses <= 1:
                    return nc, "EXIT_R", hold_sec + 30

            # 7. 最后一根强制平
            if hold == max_hold - 1:
                return nc, "CLOSE", hold_sec + 60

        else:  # sell
            # SELL以BID成交, TP/SL基于BID, ASK触发
            # TP_BID = entry - tp_dist - spread (ASK需跌到BID-tp_dist, 即BID需跌到entry-tp_dist-spread)
            # SL_BID = sl_price (已在run_combo中调整为entry+sl_val-spread)
            tp = entry - tp_dist - SPREAD_COST

            if no <= tp:
                return no, "TP", hold_sec
            if no >= sl_price:
                return no, "SL", hold_sec

            if nh >= sl_price and nl <= tp:
                return tp, "TP", hold_sec + 5
            if nh >= sl_price:
                return sl_price, "SL", hold_sec + 5
            if nl <= tp:
                return tp, "TP", hold_sec + 5

            if smart_exit:
                if body < atr * 0.2 and crosses >= 2:
                    return nc, "EXIT_S", hold_sec + 30
                if nc > no and body > 0.02 and crosses <= 1:
                    return nc, "EXIT_R", hold_sec + 30

            if hold == max_hold - 1:
                return nc, "CLOSE", hold_sec + 60

    # 兜底
    return sig["nc"], "CLOSE", 60


def run_combo(signals, cross_arr, tp_dist, sl_mode, sl_val, max_hold, smart_exit, score_min=0):
    """运行一组参数, 返回交易列表 (持仓感知: 同一时间只有一仓)"""
    trades = []
    next_free = -1  # 下一根可开仓的K线索引
    day_pnl = {}; day_loss = {}; day_trades = {}; consec = 0; prev_day = -1

    for sig in signals:
        i = sig["open_i"]
        dy = i // 1440

        if dy != prev_day:
            prev_day = dy; consec = 0

        # 评分过滤
        if sig["score"] < score_min:
            continue

        # 持仓冲突: 上一单还没平仓
        if i < next_free:
            continue

        # 日限
        if day_loss.get(dy, 0) >= 6.0: continue
        if day_pnl.get(dy, 0) >= 8.0: continue
        if day_trades.get(dy, 0) >= 50: continue
        if consec >= 5: continue

        # 计算SL (BID价格, 含点差调整)
        if sl_mode == "candle":
            # candle模式: SL=candle低/高 (BID价格), 无点差调整
            sl = sig["candle_sl"]
        elif sl_mode == "cap":
            if sig["dir"] == "buy":
                # BUY: fixed部分 = entry + spread - sl_val (ASK-sl_val转BID)
                sl = max(sig["candle_sl"], sig["entry"] + SPREAD_COST - sl_val)
            else:
                # SELL: fixed部分 = entry + sl_val - spread (BID+sl_val, ASK触发转BID)
                sl = min(sig["candle_sl"], sig["entry"] + sl_val - SPREAD_COST)
        else:  # fixed
            if sig["dir"] == "buy":
                # BUY: SL设在ASK-sl_val, BID触发 → sl_bid = entry + spread - sl_val
                sl = sig["entry"] + SPREAD_COST - sl_val
            else:
                # SELL: SL设在BID+sl_val, ASK触发 → sl_bid = entry + sl_val - spread
                sl = sig["entry"] + sl_val - SPREAD_COST

        exit_price, reason, hold_sec = sim_exit(
            sig, tp_dist, sl, max_hold, smart_exit, cross_arr
        )

        # PnL: 扣除点差 (BUY: buy@ASK sell@BID, SELL: sell@BID buy@ASK)
        if sig["dir"] == "buy":
            pnl = round(exit_price - sig["entry"] - SPREAD_COST, 4)
        else:
            pnl = round(sig["entry"] - exit_price - SPREAD_COST, 4)
        pnl_usd = round(pnl, 2)

        trades.append({
            "dir": sig["dir"], "entry": sig["entry"], "exit": exit_price,
            "pnl": pnl_usd, "reason": reason, "open_i": i,
            "score": sig["score"], "hold_sec": hold_sec,
            "sl_dist": round(abs(sig["entry"] - sl), 2),
        })

        # 更新状态
        candles_held = max(1, hold_sec // 60 + 1)
        next_free = i + candles_held
        day_pnl[dy] = day_pnl.get(dy, 0) + pnl_usd
        day_trades[dy] = day_trades.get(dy, 0) + 1
        if pnl_usd < 0:
            day_loss[dy] = day_loss.get(dy, 0) + abs(pnl_usd)
            consec += 1
        else:
            consec = 0

    return trades


def quick_stats(trades):
    if not trades: return "0笔"
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins)/n*100
    total = round(sum(t["pnl"] for t in trades), 2)
    gw = sum(t["pnl"] for t in wins) if wins else 0
    gl = sum(abs(t["pnl"]) for t in losses) if losses else 0
    pf = round(gw/gl, 2) if gl > 0 else 999
    aw = round(gw/len(wins), 3) if wins else 0
    al = round(gl/len(losses), 3) if losses else 0
    avg_hold = round(sum(t["hold_sec"] for t in trades)/n, 0) if n else 0
    avg_sl = round(sum(t["sl_dist"] for t in trades)/n, 2) if n else 0

    dp = {}
    for t in trades:
        d = t["open_i"] // 1440
        dp[d] = dp.get(d, 0) + t["pnl"]
    wd = sum(1 for p in dp.values() if p > 0)
    ld = sum(1 for p in dp.values() if p < 0)

    return "%d笔 WR%.1f%% PnL%+.1fU PF%s 盈%+.3f/亏%.3f 持%.0fs SL%.2f 盈日%d/亏%d" % (
        n, wr, total, pf, aw, al, avg_hold, avg_sl, wd, ld)


def main():
    if not mt5.initialize(): print("MT5 init fail"); return
    print("BTP 超单回测 — 全参数搜索 (智能出场, 含点差%.2f)" % SPREAD_COST)
    print("%s | 0.01手 | XAUUSD 1价格点=1U" % SYMBOL)
    print("=" * 140)

    m1, m5, m15 = fetch_data()
    print("M1=%d M5=%d M15=%d" % (len(m1), len(m5), len(m15)))
    if len(m1) < 100: print("M1数据不足"); mt5.shutdown(); return

    days = round(len(m1) / 1440)
    print("M1 约 %d 天\n" % days)

    signals, cross_arr = collect_signals(m1, m5, m15)
    print("入场信号: %d 个\n" % len(signals))
    if not signals: print("无信号"); mt5.shutdown(); return

    # ── 参数网格 (含点差, TP/SL需远大于0.37) ──
    tp_list = [0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    sl_list = [0.4, 0.5, 0.6, 0.8, 1.0, 1.5]
    hold_list = [1, 2, 3]
    exit_modes = [(False, "无智能"), (True, "智能出场")]

    # ── 1. 智能出场对比 (固定 SL=0.5, hold=2) ──
    print("  ═══ 智能出场效果对比 (SL=0.5, 持仓2根) ═══")
    print("  %-10s │ %-30s │ %-30s │ %-10s" % ("TP", "无智能出场", "智能出场", "提升"))
    print("  " + "-" * 90)
    for tp in tp_list:
        t1 = run_combo(signals, cross_arr, tp, "fixed", 0.5, 2, False)
        t2 = run_combo(signals, cross_arr, tp, "fixed", 0.5, 2, True)
        s1 = quick_stats(t1)
        s2 = quick_stats(t2)
        p1 = round(sum(t["pnl"] for t in t1), 1) if t1 else 0
        p2 = round(sum(t["pnl"] for t in t2), 1) if t2 else 0
        diff = p2 - p1
        marker = " ↑" if diff > 0 else (" ↓" if diff < 0 else " =")
        # 提取关键数据
        w1 = len([t for t in t1 if t["pnl"] > 0])/max(len(t1),1)*100 if t1 else 0
        w2 = len([t for t in t2 if t["pnl"] > 0])/max(len(t2),1)*100 if t2 else 0
        print("  TP=%.2f    │ %d笔 WR%.0f%% PnL%+.1fU          │ %d笔 WR%.0f%% PnL%+.1fU          │ %+.1fU%s" % (
            tp, len(t1), w1, p1, len(t2), w2, p2, diff, marker))
    print()

    # ── 2. 持仓K线数对比 (固定 SL=0.5, 智能出场) ──
    print("  ═══ 持仓K线数对比 (SL=0.5, 智能出场) ═══")
    print("  %-10s │ %-25s │ %-25s │ %-25s" % ("TP", "持仓1根", "持仓2根", "持仓3根"))
    print("  " + "-" * 95)
    for tp in tp_list:
        results = []
        for h in hold_list:
            t = run_combo(signals, cross_arr, tp, "fixed", 0.5, h, True)
            p = round(sum(x["pnl"] for x in t), 1) if t else 0
            w = len([x for x in t if x["pnl"] > 0])/max(len(t),1)*100 if t else 0
            results.append((len(t), w, p))
        print("  TP=%.2f    │ %d笔 WR%.0f%% PnL%+.1fU     │ %d笔 WR%.0f%% PnL%+.1fU     │ %d笔 WR%.0f%% PnL%+.1fU" % (
            tp, results[0][0], results[0][1], results[0][2],
            results[1][0], results[1][1], results[1][2],
            results[2][0], results[2][1], results[2][2]))
    print()

    # ── 3. 全参数搜索: 找最优 ──
    print("  ═══ 全参数搜索: TOP 10 最优组合 ═══")
    all_results = []
    for tp in tp_list:
        for sl_val in sl_list:
            for hold in hold_list:
                for smart, smart_label in exit_modes:
                    trades = run_combo(signals, cross_arr, tp, "fixed", sl_val, hold, smart)
                    if trades:
                        total = round(sum(t["pnl"] for t in trades), 2)
                        wins = [t for t in trades if t["pnl"] > 0]
                        wr = len(wins)/len(trades)*100
                        gw = sum(t["pnl"] for t in wins)
                        gl = sum(abs(t["pnl"]) for t in [x for x in trades if x["pnl"] <= 0])
                        pf = round(gw/gl, 2) if gl > 0 else 999
                        dp = {}
                        for t in trades:
                            d = t["open_i"] // 1440
                            dp[d] = dp.get(d, 0) + t["pnl"]
                        wd = sum(1 for p in dp.values() if p > 0)
                        ld = sum(1 for p in dp.values() if p < 0)
                        all_results.append({
                            "tp": tp, "sl": sl_val, "hold": hold, "smart": smart,
                            "smart_label": smart_label, "trades": trades,
                            "total": total, "wr": wr, "pf": pf,
                            "wd": wd, "ld": ld, "n": len(trades),
                            "avg_hold": round(sum(t["hold_sec"] for t in trades)/len(trades), 0),
                        })

    # 按总盈利排序
    all_results.sort(key=lambda x: x["total"], reverse=True)

    print("  %-5s %-4s %-4s %-6s %-6s %-6s %-5s %-5s %-5s %-6s" % (
        "TP", "SL", "Hold", "出场", "笔数", "WR%", "PnL", "PF", "盈日", "持仓s"))
    print("  " + "-" * 75)
    for r in all_results[:10]:
        print("  %-5.2f %-4.1f %-4d %-6s %-6d %-6.1f %-+6.1f %-5.2f %d/%d  %-6.0f" % (
            r["tp"], r["sl"], r["hold"], r["smart_label"][:4],
            r["n"], r["wr"], r["total"], r["pf"], r["wd"], r["ld"], r["avg_hold"]))

    # ── 4. 最佳组合详细分析 ──
    best = all_results[0]
    trades = best["trades"]
    print("\n" + "=" * 140)
    print("  ★ 最优组合: TP=%.2f SL=%.1f Hold=%d %s | PnL=%+.2fU WR=%.1f%% PF=%.2f" % (
        best["tp"], best["sl"], best["hold"], best["smart_label"], best["total"], best["wr"], best["pf"]))
    print("=" * 140)

    # 按时间段
    total_m1 = len(m1)
    for d in [10, 20, 30]:
        if d > days: break
        cutoff = max(0, total_m1 - d * 1440)
        seg = [t for t in trades if t["open_i"] >= cutoff]
        if seg:
            p = round(sum(t["pnl"] for t in seg), 1)
            w = len([t for t in seg if t["pnl"] > 0])/len(seg)*100
            print("  近%d天: %s" % (d, quick_stats(seg)))

    # 按评分
    print("\n  ── 按评分 ──")
    for sc in range(0, 8):
        seg = [t for t in trades if t["score"] == sc]
        if seg:
            print("  评分=%d: %s" % (sc, quick_stats(seg)))

    # 按方向
    print("\n  ── 按方向 ──")
    buys = [t for t in trades if t["dir"] == "buy"]
    sells = [t for t in trades if t["dir"] == "sell"]
    if buys: print("  BUY:  %s" % quick_stats(buys))
    if sells: print("  SELL: %s" % quick_stats(sells))

    # 按出场原因
    print("\n  ── 按出场原因 ──")
    reason_labels = [
        ("TP", "止盈(见利走)"),
        ("SL", "止损"),
        ("EXIT_S", "智能-价格停滞"),
        ("EXIT_R", "智能-反向K线"),
        ("CLOSE", "收盘平"),
    ]
    for reason, label in reason_labels:
        seg = [t for t in trades if t["reason"] == reason]
        if seg:
            p = round(sum(t["pnl"] for t in seg), 1)
            print("  %-12s: %s" % (label, quick_stats(seg)))

    # 日盈亏
    print("\n  ── 日盈亏 ──")
    dp = {}
    for t in trades:
        d = t["open_i"] // 1440
        dp[d] = dp.get(d, 0) + t["pnl"]
    wd = sum(1 for p in dp.values() if p > 0)
    ld = sum(1 for p in dp.values() if p < 0)
    avg_d = sum(dp.values()) / len(dp) if dp else 0
    max_d = max(dp.values()) if dp else 0
    min_d = min(dp.values()) if dp else 0
    print("  盈利日: %d | 亏损日: %d | 日均: %+.2fU | 最好: %+.2fU | 最差: %+.2fU | 日均交易: %.1f笔" % (
        wd, ld, avg_d, max_d, min_d, len(trades) / max(len(dp), 1)))

    # 翻仓模拟
    print("\n  ── 翻仓模拟 (150U起步, 固定0.01手) ──")
    bal = 150.0
    for d in sorted(dp.keys()):
        bal += dp[d]
        if bal <= 0:
            print("  Day%d: 爆仓!" % d); break
    else:
        print("  %d天后: 150U → %.2fU (%+.1f%%)" % (len(dp), bal, (bal/150-1)*100))
        daily_rate = (bal/150) ** (1.0/max(len(dp),1)) - 1
        print("  日均收益率: %.2f%% | 预计30天: %.0fU | 预计90天: %.0fU" % (
            daily_rate*100, 150*(1+daily_rate)**30, 150*(1+daily_rate)**90))

    # ── 5. 评分阈值优化 (用最优TP/SL/Hold) ──
    print("\n" + "=" * 140)
    print("  ═══ 评分阈值优化 (TP=%.2f SL=%.1f Hold=%d) ═══" % (
        best["tp"], best["sl"], best["hold"]))
    print("  %-8s %-6s %-6s %-6s %-5s %-5s %-5s" % (
        "评分≥", "笔数", "WR%", "PnL", "PF", "盈日", "日均"))
    print("  " + "-" * 55)
    best_score_result = None
    for sc in range(0, 7):
        t = run_combo(signals, cross_arr, best["tp"], "fixed", best["sl"],
                       best["hold"], best["smart"], score_min=sc)
        if not t: break
        p = round(sum(x["pnl"] for x in t), 1)
        w = len([x for x in t if x["pnl"] > 0])/len(t)*100
        gw = sum(x["pnl"] for x in t if x["pnl"] > 0)
        gl = sum(abs(x["pnl"]) for x in t if x["pnl"] <= 0)
        pf = round(gw/gl, 2) if gl > 0 else 999
        dp2 = {}
        for x in t:
            d = x["open_i"] // 1440
            dp2[d] = dp2.get(d, 0) + x["pnl"]
        wd2 = sum(1 for v in dp2.values() if v > 0)
        avg_d = sum(dp2.values()) / len(dp2) if dp2 else 0
        marker = " ★" if sc == 0 else ""
        print("  ≥%-6d %-6d %-6.1f %-+6.1f %-5.2f %-5d %-+5.2f%s" % (
            sc, len(t), w, p, pf, wd2, avg_d, marker))
        if best_score_result is None or p > best_score_result["pnl"]:
            best_score_result = {"score_min": sc, "pnl": p, "trades": t, "wr": w, "pf": pf}

    # ── 6. 评分阈值+智能出场交叉对比 ──
    print("\n  ═══ 评分×智能出场 交叉对比 (TP=%.2f SL=%.1f Hold=%d) ═══" % (
        best["tp"], best["sl"], best["hold"]))
    print("  %-8s │ %-25s │ %-25s │ %-10s" % ("评分≥", "无智能", "智能出场", "差值"))
    print("  " + "-" * 75)
    for sc in range(0, 6):
        t1 = run_combo(signals, cross_arr, best["tp"], "fixed", best["sl"],
                        best["hold"], False, score_min=sc)
        t2 = run_combo(signals, cross_arr, best["tp"], "fixed", best["sl"],
                        best["hold"], True, score_min=sc)
        if not t1 and not t2: break
        p1 = round(sum(x["pnl"] for x in t1), 1) if t1 else 0
        p2 = round(sum(x["pnl"] for x in t2), 1) if t2 else 0
        w1 = len([x for x in t1 if x["pnl"] > 0])/max(len(t1),1)*100 if t1 else 0
        w2 = len([x for x in t2 if x["pnl"] > 0])/max(len(t2),1)*100 if t2 else 0
        diff = p2 - p1
        marker = " ↑" if diff > 0.5 else (" ↓" if diff < -0.5 else " =")
        print("  ≥%-6d │ %d笔 WR%.0f%% PnL%+.1fU       │ %d笔 WR%.0f%% PnL%+.1fU       │ %+.1fU%s" % (
            sc, len(t1), w1, p1, len(t2), w2, p2, diff, marker))

    # ── 7. 评分阈值最优组合翻仓模拟 ──
    if best_score_result and best_score_result["score_min"] > 0:
        t = best_score_result["trades"]
        print("\n  ── 评分≥%d 翻仓模拟 (150U起步) ──" % best_score_result["score_min"])
        dp3 = {}
        for x in t:
            d = x["open_i"] // 1440
            dp3[d] = dp3.get(d, 0) + x["pnl"]
        bal = 150.0
        for d in sorted(dp3.keys()):
            bal += dp3[d]
            if bal <= 0:
                print("  Day%d: 爆仓!" % d); break
        else:
            print("  %d天后: 150U → %.2fU (%+.1f%%) | 日均%.2fU | WR%.1f%% PF%.2f" % (
                len(dp3), bal, (bal/150-1)*100,
                sum(dp3.values())/len(dp3), best_score_result["wr"], best_score_result["pf"]))

    mt5.shutdown()


if __name__ == "__main__":
    main()
