"""G1 策略历史回测 + 参数网格优化脚本

从 MT5 拉取 M5 历史数据, 跑 G1 信号逻辑 (时段/趋势/回撤入场),
支持 tp_sl_ratio × sl_atr_mult 网格搜索找最优参数组合。
按 ASIAN / EURO / USLATE 三个时段分别统计。

前置: MT5 终端已登录运行。
用法: python scripts/bt_g1.py
"""
import sys, json, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import numpy as np

from src.strategy.indicators import EMA, RSI, ATR
from src.strategy.engine_g1 import (
    SESSION, TRADEABLE_SESSIONS, PULLBACK_LEVELS,
    MAX_SL_PCT, MIN_SL_PCT, DEFAULT_PARAMS,
    ACCOUNT_SIZE, MAX_VOLUME, MAX_DAILY_LOSS, DAILY_TARGET, MAX_DAILY_TRADES,
)

SYMBOL = "XAUUSD"
SPREAD_COST = 0.34  # USD 每笔 (0.01 手)

# ── 优化参数网格 ──────────────────────────────────────────────
TP_SL_RATIOS = [1.3, 1.5, 1.8, 2.0, 2.2, 2.5, 3.0]
SL_ATR_MULTS = [0.8, 1.0, 1.2, 1.5, 1.8]


def current_session_bj(hour, minute):
    """返回北京时间 hour:minute 所处的时段名, None=不可交易时段"""
    ts = hour * 60 + minute
    for name, (sh, sm, eh, em) in SESSION.items():
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= ts < end:
            return name
    return None


def fetch_m5(days=90):
    """拉取 M5 历史 K 线。"""
    count = int(days * 24 * 12) + 500
    r = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, count)
    if r is None or len(r) == 0:
        return None
    bars = []
    for row in r:
        dt = datetime.fromtimestamp(row[0], tz=timezone.utc)
        bars.append({
            "t": dt, "ts": float(row[0]),
            "o": float(row[1]), "h": float(row[2]),
            "l": float(row[3]), "c": float(row[4]), "v": int(row[5]),
        })
    return bars


def backtest(bars, tp_sl_ratio, sl_atr_mult, max_daily_trades=6):
    """单组参数回测, 返回交易列表和统计。"""
    n = len(bars)
    if n < 80:
        return [], {}

    closes = np.array([b["c"] for b in bars])
    highs = np.array([b["h"] for b in bars])
    lows = np.array([b["l"] for b in bars])

    e20 = EMA(closes, 20)
    e50 = EMA(closes, 50)
    rs = RSI(closes, 14)
    at = ATR(highs, lows, closes, 14)

    trades = []
    pos = None  # {dir, entry, sl, tp, open_is, session}
    day_id = -1
    daily_pnl = 0.0
    daily_loss = 0.0
    daily_trades = 0
    consec_loss = 0
    max_daily_trades = 10
    max_daily_loss = 8.0
    daily_target = 8.0
    consec_loss_limit = 5
    pullback_levels = [0.3, 0.5, 1.5]
    vwap_sum = 0.0
    vwap_vol = 0

    def day_reset():
        nonlocal daily_pnl, daily_loss, daily_trades
        daily_pnl = 0.0; daily_loss = 0.0; daily_trades = 0

    for i in range(60, n):
        if np.isnan(e20[i]) or np.isnan(e50[i]) or np.isnan(rs[i]) or np.isnan(at[i]):
            continue
        cl = closes[i]; hi = highs[i]; lo = lows[i]; v = bars[i]["v"]
        ts = bars[i]["ts"]
        dt = bars[i]["t"]

        # 日重置
        dy = int(ts / 86400)
        if dy != day_id:
            day_id = dy; day_reset()
            vwap_sum = 0.0; vwap_vol = 0

        # VWAP
        vwap_sum += cl * v; vwap_vol += v
        vwap = vwap_sum / vwap_vol if vwap_vol > 0 else 0.0

        # 北京时间
        bj = dt + timedelta(hours=8)
        sess = current_session_bj(bj.hour, bj.minute)

        # ── 持仓管理 (SL/TP 模拟) ──
        if pos is not None:
            triggered = None
            if pos["dir"] == "buy":
                if lo <= pos["sl"]: triggered = ("SL", pos["sl"])
                elif hi >= pos["tp"]: triggered = ("TP", pos["tp"])
            else:
                if hi >= pos["sl"]: triggered = ("SL", pos["sl"])
                elif lo <= pos["tp"]: triggered = ("TP", pos["tp"])
            if triggered:
                pnl = round((triggered[1] - pos["entry"]) - SPREAD_COST, 2) if pos["dir"] == "buy" else round((pos["entry"] - triggered[1]) - SPREAD_COST, 2)
                trades.append({
                    "dir": pos["dir"], "entry": pos["entry"], "exit": triggered[1],
                    "pnl": pnl, "reason": triggered[0],
                    "open_i": pos["open_i"], "close_i": i,
                    "session": pos["session"],
                    "sl_dist": round(abs(pos["entry"] - pos["sl"]), 2),
                    "tp_dist": round(abs(pos["tp"] - pos["entry"]), 2),
                })
                daily_pnl += pnl
                daily_trades += 1
                if pnl < 0:
                    daily_loss += abs(pnl)
                    consec_loss += 1
                else:
                    consec_loss = 0
                pos = None

        if pos is not None:
            continue

        # ── 风控 ──
        if daily_pnl >= daily_target: continue
        if daily_loss >= max_daily_loss: continue
        if daily_trades >= max_daily_trades: continue
        if sess is None or sess not in TRADEABLE_SESSIONS: continue
        if consec_loss >= consec_loss_limit: continue

        # ── 趋势 ──
        cl = closes[i]
        trend_up = cl > e50[i] and e20[i] > e50[i]
        trend_dn = cl < e50[i] and e20[i] < e50[i]
        if not (trend_up or trend_dn):
            continue

        # ── 按時段选择入场模式 ──
        atr_val = float(at[i])
        signal = None
        if sess == "EURO":
            # 欧盘: VWAP 支撑/阻力入场
            if vwap and vwap > 0:
                if trend_up:
                    vr = atr_val * 0.5
                    if lo <= vwap and cl > vwap and lo >= vwap - vr:
                        if rs[i] < 68:
                            signal = "buy"
                elif trend_dn:
                    vr = atr_val * 0.5
                    if hi >= vwap and cl < vwap and hi <= vwap + vr:
                        if rs[i] > 32:
                            signal = "sell"
        else:
            # 亚盘/美盘: 回撤入场
            if trend_up:
                entry_levels = []
                if not np.isnan(e20[i]): entry_levels.append(e20[i])
                if vwap > 0: entry_levels.append(vwap)
                for lv in pullback_levels:
                    entry_levels.append(cl - atr_val * lv / 2)
                candidates = [e for e in entry_levels if e <= cl and e > cl - atr_val * 1.5]
                if candidates:
                    best = max(candidates)
                    if lo <= best or (i >= 1 and lows[i-1] <= best):
                        if rs[i] < 68:
                            signal = "buy"
                else:
                    if i >= 1 and lows[i-1] <= max(entry_levels):
                        if rs[i] < 68:
                            signal = "buy"
            elif trend_dn:
                entry_levels = []
                if not np.isnan(e20[i]): entry_levels.append(e20[i])
                if vwap > 0: entry_levels.append(vwap)
                for lv in pullback_levels:
                    entry_levels.append(cl + atr_val * lv / 2)
                candidates = [e for e in entry_levels if e >= cl and e < cl + atr_val * 1.5]
                if candidates:
                    best = min(candidates)
                    if hi >= best or (i >= 1 and highs[i-1] >= best):
                        if rs[i] > 32:
                            signal = "sell"
                else:
                    if i >= 1 and highs[i-1] >= min(entry_levels):
                        if rs[i] > 32:
                            signal = "sell"

        if not signal:
            continue

        # ── 开仓 ──
        sl_dist = max(atr_val * sl_atr_mult, cl * MIN_SL_PCT)
        max_sl_v = cl * MAX_SL_PCT
        if sl_dist > max_sl_v: sl_dist = max_sl_v
        tp_dist = sl_dist * tp_sl_ratio
        rr = tp_dist / sl_dist
        if rr < 1.3:
            continue
        if signal == "buy":
            sl = round(cl - sl_dist, 2); tp = round(cl + tp_dist, 2)
        else:
            sl = round(cl + sl_dist, 2); tp = round(cl - tp_dist, 2)
        pos = {"dir": signal, "entry": cl, "sl": sl, "tp": tp,
               "open_i": i, "session": sess}

    return trades, {}


def summarize(trades, label):
    """汇总交易统计。"""
    if not trades:
        return f"  {label}: 0 笔 (无信号)"
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n = len(trades)
    wr = len(wins) / n * 100
    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    gw = sum(t["pnl"] for t in wins)
    gl = sum(abs(t["pnl"]) for t in losses)
    pf = round(gw / gl, 2) if gl > 0 else 999.0
    aw = round(gw / len(wins), 2) if wins else 0
    al = round(gl / len(losses), 2) if losses else 0
    avg_hold = round(sum(t["close_i"] - t["open_i"] for t in trades) / n * 5, 1)  # M5 → 分钟

    # 时段分布
    by_session = {}
    for t in trades:
        s = t.get("session", "?")
        by_session.setdefault(s, []).append(t)

    session_str = " | ".join(
        f"{s}:{len(v)}笔{round(sum(x['pnl'] for x in v),1)}U"
        for s, v in sorted(by_session.items())
    )
    return (
        f"  {label}: {n}笔 WR{wr:.0f}% PnL{total_pnl:+.1f}U "
        f"PF{pf} AW{aw:+.1f} AL{al:.1f} 持仓{avg_hold:.0f}m "
        f"| {session_str}"
    )


def main():
    if not mt5.initialize():
        print(f"MT5 初始化失败: {mt5.last_error()}")
        return
    print("G1 策略回测 + 参数网格优化")
    print(f"品种: {SYMBOL} | 本金: {ACCOUNT_SIZE}U | 手数: {MAX_VOLUME}")
    print(f"交易时段: ASIAN/EURO/USLATE | 点差成本: {SPREAD_COST}U/笔")
    print("=" * 100)

    bars = fetch_m5(120)
    if bars is None or len(bars) < 100:
        print("数据拉取失败")
        mt5.shutdown()
        return
    print(f"拉取 {len(bars)} 根 M5 K 线\n")

    # ── 1. 默认参数回测 ──
    print("─" * 100)
    print("【1】默认参数回测 (tp_sl_ratio=2.0, sl_atr_mult=1.2)")
    for days in [30, 60, 90]:
        need = days * 288  # M5: 24*12=288/天
        seg = bars[-need:] if len(bars) >= need else bars
        trades, _ = backtest(seg, 2.0, 1.2)
        print(summarize(trades, f"{days}天"))

    print()

    # ── 2. 参数网格搜索 (找最优) ──
    print("─" * 100)
    print("【2】参数网格搜索 (tp_sl_ratio × sl_atr_mult) - 90天数据")
    best_score = -999
    best_pair = (None, None)
    results = []

    for tp_r in TP_SL_RATIOS:
        for sl_m in SL_ATR_MULTS:
            trades, _ = backtest(bars, tp_r, sl_m)
            if len(trades) < 5:
                continue
            wins = [t for t in trades if t["pnl"] > 0]
            total_pnl = sum(t["pnl"] for t in trades)
            wr = len(wins) / len(trades) * 100
            gw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
            gl = sum(abs(t["pnl"]) for t in trades if t["pnl"] <= 0)
            pf = gw / gl if gl > 0 else 999
            # 综合评分: PnL × wr_boost × pf_boost
            score = total_pnl
            if wr < 35: score *= 0.7
            if pf > 1.5: score *= 1.15
            results.append((tp_r, sl_m, len(trades), wr, total_pnl, pf, score))
            if score > best_score:
                best_score = score
                best_pair = (tp_r, sl_m)

    # 按 score 排序输出 top 10
    results.sort(key=lambda x: x[6], reverse=True)
    print(f"{'TP/SL':>8} {'SL×ATR':>8} {'笔数':>6} {'胜率':>6} {'总PnL':>8} {'PF':>6} {'得分':>8}")
    for r in results[:10]:
        print(f"{r[0]:>8.1f} {r[1]:>8.1f} {r[2]:>6} {r[3]:>5.0f}% {r[4]:>+7.1f}U {r[5]:>5.2f} {r[6]:>+7.0f}")
    print()
    if best_pair[0]:
        print(f"🎯 最优参数: tp_sl_ratio={best_pair[0]:.1f}, sl_atr_mult={best_pair[1]:.1f} (score={best_score:.0f})")

        # 用最优参数重跑各时段
        print()
        print("─" * 100)
        print(f"【3】最优参数 ({best_pair[0]:.1f}/{best_pair[1]:.1f}) — 按时段明细")
        opt_trades, _ = backtest(bars, best_pair[0], best_pair[1])
        if opt_trades:
            for sess_name in ["ASIAN", "EURO", "USLATE"]:
                sess_t = [t for t in opt_trades if t.get("session") == sess_name]
                print(summarize(sess_t, f"  {sess_name}"))
            print(summarize(opt_trades, "  合计"))
    else:
        print("网格搜索未找到有效参数组合")

    mt5.shutdown()
    print()
    print("说明: 点差成本 0.34U/笔 已扣除; 时段未标注的交易归属 ASIAN")


if __name__ == "__main__":
    main()
