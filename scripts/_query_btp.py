"""分析BTP实盘交易 - 修复comment匹配"""
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from collections import defaultdict

if not mt5.initialize():
    print("MT5 init fail"); exit()

# 点差
info = mt5.symbol_info("XAUUSD")
tick = mt5.symbol_info_tick("XAUUSD")
spread = tick.ask - tick.bid if tick else 0
print(f"=== XAUUSD 点差: {spread:.2f} ===")

# 获取所有成交
to = datetime.now(timezone.utc)
frm = to - timedelta(days=3)
deals = mt5.history_deals_get(frm, to)
if deals is None:
    print("无成交"); mt5.shutdown(); exit()

# 按 position_id 分组
positions = defaultdict(list)
for d in deals:
    positions[d.position_id].append(d)

# 配对 IN/OUT
pairs = []
for pos_id, dlist in positions.items():
    ins = [d for d in dlist if d.entry == mt5.DEAL_ENTRY_IN]
    outs = [d for d in dlist if d.entry == mt5.DEAL_ENTRY_OUT]
    if ins and outs:
        pairs.append((ins[0], outs[0]))

# 分类
broker_tp_sl = []  # [tp ...] or [sl ...]
program_close = []  # python close / manual
other = []

for d_in, d_out in pairs:
    cmt = d_out.comment or ""
    if "[tp" in cmt or "[sl" in cmt:
        broker_tp_sl.append((d_in, d_out))
    elif "python close" in cmt or "manual" in cmt:
        program_close.append((d_in, d_out))
    else:
        other.append((d_in, d_out))

print(f"总持仓: {len(pairs)}")
print(f"  Broker TP/SL: {len(broker_tp_sl)}")
print(f"  程序平仓: {len(program_close)}")
print(f"  其他: {len(other)}")

# 分析 Broker TP/SL 交易 (新BTP)
if broker_tp_sl:
    print(f"\n{'='*80}")
    print(f"=== Broker TP/SL 交易 (新BTP策略) ===")
    print(f"{'='*80}")

    wins = 0; losses = 0; total = 0.0
    tp_distances = []; sl_distances = []
    buy_tp = []; sell_tp = []; buy_sl = []; sell_sl = []

    for d_in, d_out in broker_tp_sl:
        profit = d_out.profit
        entry = d_in.price; exit_p = d_out.price
        direction = "BUY" if d_in.type == mt5.DEAL_TYPE_BUY else "SELL"
        cmt = d_out.comment or ""
        total += profit
        if profit > 0: wins += 1
        else: losses += 1

        if "[tp" in cmt:
            if direction == "BUY":
                dist = exit_p - entry; buy_tp.append(dist)
            else:
                dist = entry - exit_p; sell_tp.append(dist)
            tp_distances.append(dist)
        elif "[sl" in cmt:
            if direction == "BUY":
                dist = entry - exit_p; buy_sl.append(dist)
            else:
                dist = exit_p - entry; sell_sl.append(dist)
            sl_distances.append(dist)

    n = len(broker_tp_sl)
    print(f"交易数: {n}")
    print(f"胜: {wins}  负: {losses}  胜率: {wins/n*100:.1f}%")
    print(f"总盈亏: {total:+.2f}U")

    if tp_distances:
        avg_tp = sum(tp_distances) / len(tp_distances)
        print(f"\nTP触发: {len(tp_distances)}次  平均距离: {avg_tp:.3f} (设定0.40)")
        print(f"  范围: {min(tp_distances):.3f} ~ {max(tp_distances):.3f}")
        if buy_tp: print(f"  BUY TP: {sum(buy_tp)/len(buy_tp):.3f} ({len(buy_tp)}次)")
        if sell_tp: print(f"  SELL TP: {sum(sell_tp)/len(sell_tp):.3f} ({len(sell_tp)}次)")

    if sl_distances:
        avg_sl = sum(sl_distances) / len(sl_distances)
        print(f"\nSL触发: {len(sl_distances)}次  平均距离: {avg_sl:.3f} (设定0.20)")
        print(f"  范围: {min(sl_distances):.3f} ~ {max(sl_distances):.3f}")
        if buy_sl: print(f"  BUY SL: {sum(buy_sl)/len(buy_sl):.3f} ({len(buy_sl)}次)")
        if sell_sl: print(f"  SELL SL: {sum(sell_sl)/len(sell_sl):.3f} ({len(sell_sl)}次)")

    if tp_distances and sl_distances:
        avg_tp = sum(tp_distances) / len(tp_distances)
        avg_sl = sum(sl_distances) / len(sl_distances)
        print(f"\n实际风险回报: TP={avg_tp:.3f} SL={avg_sl:.3f} = 1:{avg_sl/avg_tp:.2f}")
        print(f"设定风险回报: TP=0.400 SL=0.200 = 1:0.50")
        print(f"\n点差={spread:.2f}")
        print(f"  TP少赚: 0.40-{avg_tp:.3f}={0.40-avg_tp:.3f}")
        print(f"  SL多亏: {avg_sl:.3f}-0.20={avg_sl-0.20:.3f}")

    # 明细
    print(f"\n{'时间':>10} {'方向':>4} {'入场':>8} {'出场':>8} {'距离':>7} {'盈亏':>7} {'持仓':>4} {'原因'}")
    for d_in, d_out in broker_tp_sl:
        profit = d_out.profit
        entry = d_in.price; exit_p = d_out.price
        direction = "BUY" if d_in.type == mt5.DEAL_TYPE_BUY else "SELL"
        cmt = d_out.comment or ""
        dt = datetime.fromtimestamp(d_in.time, tz=timezone.utc)
        hold = d_out.time - d_in.time
        dist = exit_p - entry if direction == "BUY" else entry - exit_p
        print(f"{dt.strftime('%m-%d %H:%M'):>10} {direction:>4} {entry:>8.2f} {exit_p:>8.2f} "
              f"{dist:>+7.3f} {profit:>+7.2f} {hold:>3}s {cmt}")

# 分析程序平仓交易 (旧策略)
if program_close:
    print(f"\n{'='*80}")
    print(f"=== 程序平仓交易 (旧策略/快速平仓) ===")
    print(f"{'='*80}")
    wins = sum(1 for _, d in program_close if d.profit > 0)
    losses = len(program_close) - wins
    total = sum(d.profit for _, d in program_close)
    print(f"交易数: {len(program_close)}  胜: {wins}  负: {losses}  PnL: {total:+.2f}U")

    # 按日期分组
    daily = defaultdict(lambda: [0, 0, 0.0])  # [wins, losses, pnl]
    for d_in, d_out in program_close:
        day = datetime.fromtimestamp(d_in.time, tz=timezone.utc).strftime("%m-%d")
        if d_out.profit > 0: daily[day][0] += 1
        else: daily[day][1] += 1
        daily[day][2] += d_out.profit
    print("\n按日统计:")
    for day in sorted(daily.keys()):
        w, l, p = daily[day]
        print(f"  {day}: {w}胜{l}负 PnL{p:+.2f}U")

mt5.shutdown()
