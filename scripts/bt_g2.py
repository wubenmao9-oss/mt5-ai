"""G2 三线顺微利策略回测 (M5)"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import numpy as np
from src.strategy.indicators import EMA, RSI, ATR

SYMBOL = "XAUUSD"
SPREAD_COST = 0.34
TP_FIXED = 2.0
SL_FIXED = 1.0
MAX_DAILY_LOSS = 4.0
DAILY_TARGET = 8.0
MAX_DAILY_TRADES = 15
CONSEC_LOSS_LIMIT = 3


def points_to_price(profit_usd):
    return max(profit_usd * 1.5, 0.1)


def fetch_m5(days=120):
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


def backtest(bars):
    n = len(bars)
    if n < 100:
        return []

    closes = np.array([b["c"] for b in bars])
    highs = np.array([b["h"] for b in bars])
    lows = np.array([b["l"] for b in bars])
    e10 = EMA(closes, 10)
    e20 = EMA(closes, 20)
    e30 = EMA(closes, 30)
    rs = RSI(closes, 14)
    at = ATR(highs, lows, closes, 14)

    trades = []
    pos = None
    day_id = -1
    daily_pnl = 0.0
    daily_loss = 0.0
    daily_trades = 0
    consec_loss = 0

    for i in range(60, n):
        if any(np.isnan(v) for v in [e10[i], e20[i], e30[i], rs[i], at[i]]):
            continue
        cl = closes[i]; lo = lows[i]; hi = highs[i]
        ts = bars[i]["ts"]
        dt = bars[i]["t"]
        bj = dt + timedelta(hours=8)

        dy = int(ts / 86400)
        if dy != day_id:
            day_id = dy
            daily_pnl = 0.0; daily_loss = 0.0; daily_trades = 0; consec_loss = 0

        # 持仓
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
                hh, mm = bj.hour, bj.minute
                bjt = hh*60+mm
                sess = "ASIAN" if 8*60<=bjt<15*60+20 else "BLOCK1" if 15*60+30<=bjt<16*60 else "EURO" if 16*60<=bjt<20*60+20 else "BLOCK2" if 20*60+30<=bjt<22*60 else "USLATE" if 22*60<=bjt<23*60+30 else "OFF"
                trades.append({
                    "dir": pos["dir"], "entry": pos["entry"], "exit": triggered[1],
                    "pnl": pnl, "reason": triggered[0],
                    "open_i": pos["open_i"], "close_i": i, "session": sess,
                })
                daily_pnl += pnl; daily_trades += 1
                if pnl < 0: daily_loss += abs(pnl); consec_loss += 1
                else: consec_loss = 0
                pos = None

        if pos is not None:
            continue

        if daily_pnl >= DAILY_TARGET: continue
        if daily_loss >= MAX_DAILY_LOSS: continue
        if daily_trades >= MAX_DAILY_TRADES: continue
        if consec_loss >= CONSEC_LOSS_LIMIT: continue

        bull = e10[i] > e20[i] > e30[i]
        bear = e10[i] < e20[i] < e30[i]
        if not (bull or bear):
            continue

        atr_val = float(at[i])
        signal = None
        if bull:
            lower = e20[i] - atr_val * 0.3
            if lo <= e20[i] and cl >= lower:
                if cl > e20[i] or (hi - lo) > atr_val * 0.5:
                    if rs[i] < 75:
                        signal = "buy"
        elif bear:
            upper = e20[i] + atr_val * 0.3
            if hi >= e20[i] and cl <= upper:
                if cl < e20[i] or (hi - lo) > atr_val * 0.5:
                    if rs[i] > 25:
                        signal = "sell"

        if not signal:
            continue

        sl_dist = points_to_price(SL_FIXED)
        tp_dist = points_to_price(TP_FIXED)
        if sl_dist <= 0 or tp_dist <= 0:
            continue
        if signal == "buy":
            sl = round(cl - sl_dist, 2); tp = round(cl + tp_dist, 2)
        else:
            sl = round(cl + sl_dist, 2); tp = round(cl - tp_dist, 2)
        pos = {"dir": signal, "entry": cl, "sl": sl, "tp": tp, "open_i": i}

    return trades


def summarize(trades, label):
    if not trades:
        return f"  {label}: 0 笔"
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / n * 100
    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    gw = sum(t["pnl"] for t in wins)
    gl = sum(abs(t["pnl"]) for t in losses)
    pf = round(gw / gl, 2) if gl > 0 else 999.0
    aw = round(gw / len(wins), 2) if wins else 0
    al = round(gl / len(losses), 2) if losses else 0
    avg_hold = round(sum(t["close_i"] - t["open_i"] for t in trades) / n * 5, 1)
    daily_pnls = {}
    for t in trades:
        d = t["open_i"] // 288
        daily_pnls.setdefault(d, 0)
        daily_pnls[d] += t["pnl"]
    win_d = sum(1 for p in daily_pnls.values() if p > 0)
    loss_d = sum(1 for p in daily_pnls.values() if p < 0)
    by_sess = {}
    for t in trades:
        s = t.get("session", "?")
        by_sess.setdefault(s, []).append(t)
    sess_str = " | ".join(f"{s}:{len(v)}笔{round(sum(x['pnl'] for x in v),1)}U" for s,v in sorted(by_sess.items()))
    return (
        f"  {label}: {n}笔 WR{wr:.0f}% PnL{total_pnl:+.2f}U "
        f"PF{pf} 盈{aw:+.1f}U/亏{al:.1f}U 持仓{avg_hold:.0f}m "
        f"盈日{win_d}/亏{loss_d} "
        f"| {sess_str}"
    )


def main():
    if not mt5.initialize():
        print("MT5 init fail"); return
    print("G2 三线顺微利策略回测 (M5)")
    print(f"{SYMBOL} | 0.01手 | TP+{TP_FIXED}U SL-{SL_FIXED}U | 点差+{SPREAD_COST}U")
    print("数学优势: 胜率>45%即盈利, 盈亏比1:2")
    print("=" * 110)

    bars = fetch_m5(120)
    if bars is None or len(bars) < 100:
        print("数据拉取失败"); mt5.shutdown(); return
    days_avail = round(len(bars) / 288)
    print(f"拉取 {len(bars)} 根 M5 K 线 (约 {days_avail} 天)\n")

    trades = backtest(bars)
    if not trades:
        print("回测: 0 笔交易"); mt5.shutdown(); return

    print(summarize(trades, "全部"))

    total_bars = len(bars)
    for days in [30, 60, 90]:
        limit_bars = days * 288
        cutoff = max(0, total_bars - limit_bars)
        seg = [t for t in trades if t["open_i"] >= cutoff]
        print(summarize(seg, f"{days}天"))

    print()
    print("  ── 时段明细 ──")
    for sn in ["ASIAN", "EURO", "USLATE", "BLOCK1", "BLOCK2", "OFF"]:
        seg = [t for t in trades if t.get("session") == sn]
        if seg:
            print(summarize(seg, f"  {sn}"))

    print()
    daily_pnls = {}
    for t in trades:
        d = t["open_i"] // 288
        daily_pnls.setdefault(d, 0)
        daily_pnls[d] += t["pnl"]
    wd = sum(1 for p in daily_pnls.values() if p > 0)
    ld = sum(1 for p in daily_pnls.values() if p < 0)
    zd = sum(1 for p in daily_pnls.values() if p == 0)
    print(f"  ── 日盈亏分布 ({len(daily_pnls)}天) ──")
    print(f"  盈利日: {wd}天 | 亏损日: {ld}天 | 持平: {zd}天")
    print(f"  日均: {sum(daily_pnls.values())/len(daily_pnls):+.2f}U | 最佳: {max(daily_pnls.values()):+.2f}U | 最差: {min(daily_pnls.values()):+.2f}U")

    mt5.shutdown()


if __name__ == "__main__":
    main()
