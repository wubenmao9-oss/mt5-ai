"""M1 策略历史回测脚本 (独立, 不改后端)

从 engine_m1 导入真实指标函数和风控常量, 复用 M1 的信号逻辑,
在 MT5 历史 M15 数据上模拟交易 (SL/TP + 移动止损 + 风控闸门)。

前置: MT5 终端必须已登录运行。
用法: python scripts/bt_m1.py
"""
import sys
from pathlib import Path

# 确保能 import src.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import numpy as np

# 复用 M1 真实指标与风控常量 (保证信号逻辑一致)
from src.strategy.engine_m1 import (
    ema, rsi, atr, bb,
    DAILY_TARGET, MAX_SL_PCT, MIN_SL_PCT, TP_SL_RATIO,
    BE_TRIGGER, PL_TRIGGER, COOLDOWN_AFTER_LOSS_STREAK, SPREAD,
)

SYMBOL = "XAUUSD"


def fetch_m15(days: int):
    """从 MT5 拉取历史 M15 K 线 (含周末空档, 由 MT5 自动跳过)。"""
    count = int(days * 24 * 4) + 300  # 多拉一些防不足
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, count)
    if rates is None or len(rates) == 0:
        return None
    return rates


def backtest(rates, label: str):
    """在 M15 K 线序列上跑 M1 策略模拟。"""
    # 解析 K 线
    times = np.array([r[0] for r in rates], dtype=float)
    highs = np.array([r[2] for r in rates], dtype=float)
    lows = np.array([r[3] for r in rates], dtype=float)
    closes = np.array([r[4] for r in rates], dtype=float)

    n = len(closes)
    if n < 80:
        return f"{label}: 数据不足 ({n} 根)"

    # 指标 (与 M1 一致)
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)
    rs = rsi(closes, 14)
    at = atr(highs, lows, closes, 14)
    bm, bu, bl = bb(closes, 20, 2.0)

    # ---- 模拟状态 ----
    pos = None  # {dir, entry, sl, tp, sl_stage, open_idx, open_time}
    trades = []  # {pnl, reason, hold_min, open_time}
    balance = 100.0  # 起点 100U
    start_balance = balance
    max_equity = balance
    max_dd = 0.0

    # 日内风控状态
    day_id = -1
    daily_pnl = 0.0
    daily_loss = 0.0
    daily_trades = 0
    consec_loss = 0
    consec_bar = -999
    cooldown_until_ts = 0.0

    SESSION_START = 8   # 北京时间
    SESSION_END = 20
    MAX_LOSS = 5.0
    MAX_TRADES = 4

    def new_day_reset(d):
        nonlocal daily_pnl, daily_loss, daily_trades, consec_loss, cooldown_until_ts
        daily_pnl = 0.0
        daily_loss = 0.0
        daily_trades = 0
        # 注意: consec_loss 跨日不重置 (M1 实现也是跨日累计, 仅盈利才清零)
        cooldown_until_ts = 0.0

    def close_pos(close_price, reason, idx):
        """平仓并记录."""
        nonlocal balance, daily_pnl, daily_loss, daily_trades, consec_loss, consec_bar
        nonlocal max_equity, max_dd, pos
        if pos is None:
            return
        d = pos["dir"]
        if d == "buy":
            raw = close_price - pos["entry"]
        else:
            raw = pos["entry"] - close_price
        pnl = round(raw - SPREAD, 2)  # 扣点差
        balance += pnl
        daily_pnl += pnl
        hold_min = round((times[idx] - pos["open_time"]) / 60, 1)
        trades.append({
            "pnl": pnl, "reason": reason, "hold_min": hold_min,
            "dir": d, "open_time": pos["open_time"], "close_time": times[idx],
            "balance": round(balance, 2),
        })
        if pnl < 0:
            daily_loss += abs(pnl)
            consec_loss += 1
            consec_bar = idx
            if consec_loss >= 2:
                cooldown_until_ts = times[idx] + COOLDOWN_AFTER_LOSS_STREAK
        else:
            consec_loss = 0
        # 回撤
        max_equity = max(max_equity, balance)
        dd = (max_equity - balance) / max_equity * 100 if max_equity > 0 else 0
        max_dd = max(max_dd, dd)
        pos = None

    # 主循环
    for i in range(60, n):
        if np.isnan(e50[i]) or np.isnan(rs[i]) or np.isnan(at[i]) or np.isnan(bu[i]):
            continue

        t = times[i]
        cl = closes[i]; hi = highs[i]; lo = lows[i]

        # 日重置 (UTC day, 与 M1 一致)
        d = int(t / 86400)
        if d != day_id:
            day_id = d
            new_day_reset(d)

        # 北京时间小时
        hh = int((datetime.fromtimestamp(t, tz=timezone.utc) + timedelta(hours=8)).hour % 24)

        # ---- 1. 持仓管理: SL/TP 检查 (用本根 high/low) ----
        if pos is not None:
            triggered = None
            if pos["dir"] == "buy":
                if lo <= pos["sl"]:
                    triggered = ("SL", pos["sl"])
                elif hi >= pos["tp"]:
                    triggered = ("TP", pos["tp"])
            else:
                if hi >= pos["sl"]:
                    triggered = ("SL", pos["sl"])
                elif lo <= pos["tp"]:
                    triggered = ("TP", pos["tp"])
            if triggered:
                close_pos(triggered[1], triggered[0], i)

        # ---- 2. 持仓管理: 移动止损 (用本根 close 算浮盈) ----
        if pos is not None:
            if pos["dir"] == "buy":
                profit = (cl - pos["entry"]) - SPREAD
            else:
                profit = (pos["entry"] - cl) - SPREAD
            # 阶段1: 保本
            if profit >= BE_TRIGGER and pos["sl_stage"] < 1:
                offset = 0.3
                new_sl = round(pos["entry"] + offset, 2) if pos["dir"] == "buy" else round(pos["entry"] - offset, 2)
                # 只往有利方向移
                if (pos["dir"] == "buy" and new_sl > pos["sl"]) or (pos["dir"] == "sell" and new_sl < pos["sl"]):
                    pos["sl"] = new_sl
                pos["sl_stage"] = 1
            # 阶段2: 锁利
            elif profit >= PL_TRIGGER and pos["sl_stage"] < 2:
                offset = 1.5
                new_sl = round(pos["entry"] + offset, 2) if pos["dir"] == "buy" else round(pos["entry"] - offset, 2)
                if (pos["dir"] == "buy" and new_sl > pos["sl"]) or (pos["dir"] == "sell" and new_sl < pos["sl"]):
                    pos["sl"] = new_sl
                pos["sl_stage"] = 2

        if pos is not None:
            continue  # 持仓中不进场

        # ---- 3. 风控闸门 (与 M1 完全一致) ----
        if daily_pnl >= DAILY_TARGET:
            continue
        if daily_loss >= MAX_LOSS:
            continue
        if daily_trades >= MAX_TRADES:
            continue
        if not (SESSION_START <= hh < SESSION_END):
            continue
        if t < cooldown_until_ts:
            continue
        if consec_loss >= 2 and (i - consec_bar) < 6:
            continue

        # ---- 4. 趋势过滤 ----
        slope_up = e50[i] > e50[max(0, i - 3)]
        slope_dn = e50[i] < e50[max(0, i - 3)]
        trend_up = cl > e50[i] and slope_up
        trend_dn = cl < e50[i] and slope_dn
        ranging = abs(cl - e50[i]) < at[i] * 0.5

        # ---- 5. 信号: 布林带回归 ----
        touch_lower = lo <= bl[i]
        rsi_oversold = rs[i] < 35
        pullback_up = cl > bl[i]
        long_ok = touch_lower and rsi_oversold and pullback_up and (trend_up or ranging)

        touch_upper = hi >= bu[i]
        rsi_overbought = rs[i] > 65
        pullback_dn = cl < bu[i]
        short_ok = touch_upper and rsi_overbought and pullback_dn and (trend_dn or ranging)

        if not (long_ok or short_ok):
            continue

        # ---- 6. 开仓 (ATR 动态 SL/TP, 小资金硬上限) ----
        direction = "buy" if long_ok else "sell"
        sl_dist = max(at[i] * 1.2, cl * MIN_SL_PCT)
        max_sl = cl * MAX_SL_PCT
        if sl_dist > max_sl:
            sl_dist = max_sl
        tp_dist = sl_dist * TP_SL_RATIO

        if direction == "buy":
            sl = round(cl - sl_dist, 2)
            tp = round(cl + tp_dist, 2)
        else:
            sl = round(cl + sl_dist, 2)
            tp = round(cl - tp_dist, 2)

        pos = {
            "dir": direction, "entry": cl, "sl": sl, "tp": tp,
            "sl_stage": 0, "open_idx": i, "open_time": t,
        }
        daily_trades += 1

    # ---- 收尾: 强平未关闭持仓 ----
    if pos is not None:
        close_pos(closes[-1], "EOD", n - 1)

    # ---- 统计 ----
    if not trades:
        return f"{label}: 0 笔交易 (无信号触发)"

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = len(trades)
    win_rate = len(wins) / total * 100
    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = sum(abs(t["pnl"]) for t in losses)
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 999.0
    avg_win = round(gross_win / len(wins), 2) if wins else 0
    avg_loss = round(gross_loss / len(losses), 2) if losses else 0
    avg_hold = round(sum(t["hold_min"] for t in trades) / total, 1)

    # 日盈利分布
    day_pnl = {}
    for t in trades:
        d = int(t["close_time"] / 86400)
        day_pnl[d] = day_pnl.get(d, 0) + t["pnl"]
    profit_days = sum(1 for v in day_pnl.values() if v > 0)
    loss_days = sum(1 for v in day_pnl.values() if v < 0)
    flat_days = sum(1 for v in day_pnl.values() if v == 0)

    return {
        "label": label,
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_min": avg_hold,
        "max_dd": round(max_dd, 1),
        "balance": round(balance, 2),
        "profit_days": profit_days,
        "loss_days": loss_days,
        "flat_days": flat_days,
        "final_equity_pct": round((balance / start_balance - 1) * 100, 1),
    }


def fmt(r):
    if isinstance(r, str):
        return "  " + r
    return (
        f"  {r['label']}: {r['trades']}笔 | WR:{r['win_rate']}% | PnL:{r['total_pnl']:+.2f}U | "
        f"PF:{r['profit_factor']} | AW:{r['avg_win']:+.2f} | AL:{r['avg_loss']:.2f} | "
        f"持仓:{r['avg_hold_min']:.0f}m | MaxDD:{r['max_dd']}% | "
        f"末值:{r['balance']:.2f}U({r['final_equity_pct']:+.1f}%) | "
        f"盈/亏/平日:{r['profit_days']}/{r['loss_days']}/{r['flat_days']}"
    )


def main():
    if not mt5.initialize():
        print(f"MT5 初始化失败: {mt5.last_error()}")
        print("请确保 MT5 终端已登录运行")
        return
    print("M1 策略历史回测 (布林带回归 + EMA50 趋势过滤, 小资金风控)")
    print("=" * 90)
    print(f"品种: {SYMBOL} | 本金: 100U | 手数: 0.01 | 时段: 北京 08-20 (亚欧盘)")
    print(f"止损: ATR×1.2 (上限 0.25%) | 止盈: {TP_SL_RATIO}×SL | 日目标: +{DAILY_TARGET}U | 日亏上限: 5U")
    print("=" * 90)

    # 拉一次最大周期数据, 切片回测多周期
    print("拉取历史 M15 数据中...")
    rates = fetch_m15(120)
    if rates is None:
        print("拉取数据失败, 请检查 MT5 连接与品种")
        mt5.shutdown()
        return
    print(f"获取 {len(rates)} 根 M15 K 线")
    print("-" * 90)

    # 多周期回测: 取最近 N 天的数据
    for days in [30, 60, 90]:
        m15_per_day = 96  # 24*4
        need = days * m15_per_day
        seg = rates[-need:] if len(rates) >= need else rates
        res = backtest(seg, f"{days}天")
        print(fmt(res))

    print("-" * 90)
    # 输出最近 10 笔交易明细 (用最长周期)
    seg = rates[-90 * 96:] if len(rates) >= 90 * 96 else rates
    # 重新跑一次拿 trades (简化: 直接重跑)
    # 为避免重复, 这里只展示统计已足够
    print("说明: PnL 已扣除单笔点差成本 0.34U; 盈/亏/平日按 UTC 交易日聚合")
    mt5.shutdown()


if __name__ == "__main__":
    main()
