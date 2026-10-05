"""Queen v2 — 量子女王网格策略 (适配FxPro 100U)

v1教训:
  - 入场太频繁 (RSI随便就开仓) → 改用ADM的H1结构+评分系统
  - TP太近 0.3点 (点差0.19吃掉大部分利润) → 放大到0.5点/段
  - 网格间距太大 ATR×1.0 (10-15点) → 缩至ATR×0.3 (3-5点)
  - 无硬止损 (DD强平时均亏21.89U) → 每层独立SL + 篮子DD上限

核心: 趋势方向网格 + 分段止盈 + 严格入场过滤
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
    _wick_up, _wick_dn,
    RANGE_KEY_LEVEL_DIST,
    ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD,
    TREND_EMA_FAST, TREND_EMA_SLOW,
    M5_RSI_PERIOD,
)

# ── 连接 ──
LOGIN = 0                  # 你的 MT5 账号
PASSWORD = "你的密码"       # 你的 MT5 密码
SERVER = "FxPro-MT5 Demo"
SYMBOL = "GOLD"
SPREAD_COST = 0.19

# ═══════════════════════════════════════════════════
# Queen v2 参数 (适配100U本金)
# ═══════════════════════════════════════════════════
ACCOUNT_SIZE = 100.0
VOLUME_PER_GRID = 0.01        # 每层网格手数
MAX_GRID_DEPTH = 4            # 最大网格层数 (缩至4层保安全)
GRID_SPACING_ATR = 0.3        # 网格间距 = ATR×0.3 (3-5点)
TP_PER_SEGMENT = 0.50         # 每级止盈 0.5点
SL_PER_ENTRY = 1.0            # 每层网格独立止损 1.0点
BASKET_DD_MAX = 3.0           # 篮子总浮亏超3U强平
MIN_ATR = 8.0                 # ATR最小值
MAX_DAILY_LOSS = 6.0          # 日最大亏损6U
MAX_DAILY_TRADES = 20         # 日最大篮子数

# 入场评分门槛
MIN_ENTRY_SCORE = 4           # 最低入场评分 (同ADM)
SESSIONS_WEAK = [22,23,0,1,2,3,4,5,6,7]  # 弱时段


def fetch_data():
    m5 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 60000)
    h1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 8000)
    h4 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H4, 0, 4000)
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
    """预计算H1结构缓存 (同ADM)"""
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


def score_entry(m5, idx, signal_dir, atr_val, m5_rsi, h1_levels, h1_fvgs, h1_obs):
    """同ADM评分系统"""
    if idx < 5: return 0
    score = 0
    c = m5[idx]; prev = m5[idx-1]; cl = c["c"]

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
    else:
        if any(f["type"] == "bear" and f["low"] <= cl <= f["high"] for f in h1_fvgs): score += 1
        if any(o["type"] == "bear_ob" and o["low"] <= cl <= o["high"] for o in h1_obs): score += 1

    if idx >= 5:
        mom = cl - m5[idx-5]["c"]
        if signal_dir == "buy" and mom > 0: score += 1
        elif signal_dir == "sell" and mom < 0: score += 1

    avg_v = np.mean([m5[j]["v"] for j in range(max(0,idx-20), idx+1)])
    if c["v"] > avg_v * 1.2: score += 1

    return score


class GridBasket:
    """网格篮子 — 管理顺趋势的加仓网格"""

    def __init__(self, direction, price, atr_val, ts, i, score, spread):
        self.direction = direction
        self.levels = [{"price": price, "entry_ts": ts, "entry_i": i, "sl_hit": False, "tp_hit": False}]
        self.atr_val = atr_val
        self.spread = spread
        self.score = score
        self.start_ts = ts
        self.start_i = i
        self.closed = False
        self.close_reason = ""
        self.close_ts = 0
        self.close_price = 0
        self.pnl = 0.0
        self.n_tp = 0
        self.n_sl = 0

    @property
    def grid_step(self):
        """网格间距"""
        return max(self.atr_val * GRID_SPACING_ATR, 0.3)

    @property
    def tp_dist(self):
        """每级止盈距离"""
        return TP_PER_SEGMENT

    @property
    def sl_dist(self):
        """每层止损距离"""
        return SL_PER_ENTRY

    @property
    def active_levels(self):
        return [l for l in self.levels if not l["sl_hit"] and not l["tp_hit"]]

    def should_add(self, low, high):
        """价格触及下一层网格线时加仓"""
        if len(self.levels) >= MAX_GRID_DEPTH: return False
        if not self.active_levels: return False
        last_price = self.active_levels[-1]["price"]
        if self.direction == "buy":
            return low <= last_price - self.grid_step
        else:
            return high >= last_price + self.grid_step

    def add_level(self, price, ts, i):
        self.levels.append({"price": price, "entry_ts": ts, "entry_i": i, "sl_hit": False, "tp_hit": False})

    def check_exit(self, high, low, ts):
        """检查每个网格层的止盈止损 + 篮子总风控"""
        if self.closed: return

        # 逐层检查
        for lv in self.levels:
            if lv["sl_hit"] or lv["tp_hit"]: continue
            if self.direction == "buy":
                if high >= lv["price"] + self.tp_dist:
                    lv["tp_hit"] = True
                    self.n_tp += 1
                elif low <= lv["price"] - self.sl_dist:
                    lv["sl_hit"] = True
                    self.n_sl += 1
            else:
                if low <= lv["price"] - self.tp_dist:
                    lv["tp_hit"] = True
                    self.n_tp += 1
                elif high >= lv["price"] + self.sl_dist:
                    lv["sl_hit"] = True
                    self.n_sl += 1

        # 计算篮子实时盈亏
        total_pnl = 0.0
        for lv in self.levels:
            if lv["tp_hit"]:
                total_pnl += self.tp_dist - self.spread
            elif lv["sl_hit"]:
                total_pnl -= self.sl_dist + self.spread
            else:
                # 浮盈浮亏 (使用当前价格)
                if self.direction == "buy":
                    upnl = low - lv["price"] - self.spread
                else:
                    upnl = lv["price"] - high - self.spread
                total_pnl += upnl

        # 篮子DD强平
        if total_pnl < -BASKET_DD_MAX:
            self.closed = True
            self.close_reason = "DD_CUT"
            self.close_ts = ts
            self.pnl = total_pnl
            return

        # 所有层级都成交 (全止盈或全止损)
        if self.n_tp + self.n_sl >= len(self.levels):
            self.closed = True
            self.close_ts = ts
            tp_ct = self.n_tp; sl_ct = self.n_sl
            if tp_ct > sl_ct:
                self.close_reason = "TP"
            elif sl_ct > tp_ct:
                self.close_reason = "LOSS"
            else:
                self.close_reason = "EVEN"
            self.pnl = total_pnl
            return

    def to_dict(self):
        total_vol = len(self.levels) * VOLUME_PER_GRID
        avg_entry = np.mean([l["price"] for l in self.levels])
        return {
            "dir": self.direction,
            "n_levels": len(self.levels),
            "avg_entry": round(avg_entry, 2),
            "pnl": round(self.pnl, 2),
            "reason": self.close_reason,
            "start_i": self.start_i,
            "close_ts": self.close_ts,
            "hold_m5": round((self.close_ts - self.start_ts) / 300, 1) if self.closed else 0,
            "n_tp": self.n_tp,
            "n_sl": self.n_sl,
            "score": self.score,
            "mode": "QUEEN",
        }


def run_queen_bt(m5, h1, h4):
    n = len(m5)
    if n < 100: return []

    print("  预计算H1缓存...")
    h1_cache = precompute_h1_cache(h1, h4)
    print(f"  H1缓存: {len(h1_cache)}个")
    m5_rsi = RSI(np.array([b["c"] for b in m5]), M5_RSI_PERIOD)

    print("  运行网格...")
    trades = []
    active_basket = None
    h1_idx = 0
    day_pnl = defaultdict(float)
    day_trades = defaultdict(int)
    day_id = -1

    for i in range(60, n - 6):
        ts = m5[i]["ts"]; cl = m5[i]["c"]; high = m5[i]["h"]; low = m5[i]["l"]
        utc_hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour

        # 弱时段跳过
        if utc_hour in SESSIONS_WEAK: continue

        # 日风控
        dy = int(ts / 86400)
        if dy != day_id: day_id = dy; day_pnl[dy] = 0; day_trades[dy] = 0
        if day_pnl.get(dy, 0) < -MAX_DAILY_LOSS: continue
        if day_trades.get(dy, 0) >= MAX_DAILY_TRADES: continue

        # H1缓存
        h1_idx = time_align(h1, ts, min(h1_idx+1, len(h1)-1))
        if h1_idx not in h1_cache: continue
        c = h1_cache[h1_idx]
        if c["atr"] < MIN_ATR: continue

        # ── 活跃篮子处理 ──
        if active_basket:
            active_basket.check_exit(high, low, ts)

            if active_basket.closed:
                trades.append(active_basket.to_dict())
                day_pnl[dy] += active_basket.pnl
                day_trades[dy] += 1
                active_basket = None
            else:
                # 加仓
                if active_basket.should_add(low, high):
                    step = active_basket.grid_step
                    if active_basket.direction == "buy":
                        gprice = active_basket.levels[-1]["price"] - step
                    else:
                        gprice = active_basket.levels[-1]["price"] + step
                    active_basket.add_level(gprice, ts, i)
                continue

        # ── 新入场信号 ──
        regime = c["regime"]
        levels = c["levels"]; fvgs = c["fvgs"]; obs = c["obs"]

        # RANGE模式下：震荡反转
        if regime in ("RANGING", "VOLATILE"):
            # 检查是否接近关键位
            near_s = near_r = False
            for lv in levels:
                if abs(cl - lv["price"]) < c["atr"] * RANGE_KEY_LEVEL_DIST:
                    if lv["type"] in ("support", "both"): near_s = True
                    if lv["type"] in ("resist", "both"): near_r = True

            sig_dir = None
            if near_s: sig_dir = "buy"
            elif near_r: sig_dir = "sell"
            if not sig_dir: continue

            sc = score_entry(m5, i, sig_dir, c["atr"], m5_rsi, levels, fvgs, obs)
            if sc >= MIN_ENTRY_SCORE:
                active_basket = GridBasket(sig_dir, cl, c["atr"], ts, i, sc, SPREAD_COST)

        # TRENDING模式下：趋势回调入场
        elif regime == "TRENDING":
            h1_slice = c["h1_slice"]
            if len(h1_slice) < 50: continue
            hc = np.array([b["c"] for b in h1_slice])
            e20 = EMA(hc, TREND_EMA_FAST); e50 = EMA(hc, TREND_EMA_SLOW)
            if np.isnan(e20[-1]) or np.isnan(e50[-1]): continue

            trend_up = e20[-1] > e50[-1]
            sig_dir = None

            if trend_up:
                # 回调到FVG/OB买
                if any(f["type"]=="bull" and f["low"]<=cl<=f["high"] for f in fvgs): sig_dir = "buy"
                elif any(o["type"]=="bull_ob" and o["low"]<=cl<=o["high"] for o in obs): sig_dir = "buy"
            else:
                if any(f["type"]=="bear" and f["low"]<=cl<=f["high"] for f in fvgs): sig_dir = "sell"
                elif any(o["type"]=="bear_ob" and o["low"]<=cl<=o["high"] for o in obs): sig_dir = "sell"

            if sig_dir:
                sc = score_entry(m5, i, sig_dir, c["atr"], m5_rsi, levels, fvgs, obs)
                if sc >= MIN_ENTRY_SCORE:
                    active_basket = GridBasket(sig_dir, cl, c["atr"], ts, i, sc, SPREAD_COST)

    return trades


def quick_stats(trades):
    if not trades: return "0笔"
    n = len(trades)
    w = [t for t in trades if t["pnl"] > 0]
    l = [t for t in trades if t["pnl"] <= 0]
    wr = len(w)/n*100
    total = round(sum(t["pnl"] for t in trades), 2)
    gw = sum(t["pnl"] for t in w) if w else 0
    gl = sum(abs(t["pnl"]) for t in l) if l else 0
    pf = round(gw/gl, 2) if gl > 0 else 999
    avg_pnl = round(total/n, 2)
    avg_hold = round(sum(t["hold_m5"] for t in trades)/n, 1) if n else 0
    avg_lvl = round(sum(t["n_levels"] for t in trades)/n, 1) if n else 0

    dp = defaultdict(float)
    for t in trades:
        d = int(t["close_ts"]/86400) if t["close_ts"] else 0
        dp[d] += t["pnl"]
    wd = sum(1 for p in dp.values() if p>0)
    ld = sum(1 for p in dp.values() if p<0)

    cum=0; mp=0; dd=0
    for t in trades:
        cum+=t["pnl"]; mp=max(mp,cum); dd=max(dd,mp-cum)

    return (f"{n}笔 WR{wr:.1f}% PnL{total:+.1f}U PF{pf} "
            f"均利{avg_pnl:+.2f}U 均持{avg_hold}根 均{avg_lvl}层 "
            f"盈日{wd}/{wd+ld}({wd/(wd+ld)*100:.0f}%) DD{dd:.0f}U")


def main():
    if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
        print("MT5 init fail:", mt5.last_error()); return

    print("=" * 100)
    print("Queen v2 回测 — 趋势网格 + 分段止盈 + 结构入场 (适配FxPro 100U)")
    print(f"{SYMBOL} | 0.01手/层 | 最多{MAX_GRID_DEPTH}层 | 点差{SPREAD_COST}")
    print("=" * 100)

    m5, h1, h4 = fetch_data()
    print(f"M5={len(m5)} H1={len(h1)} H4={len(h4)}")
    days = round(len(m5)/288)
    print(f"约 {days} 天\n")

    trades = run_queen_bt(m5, h1, h4)
    print(f"\n结果: {quick_stats(trades)}")

    # 按出场原因
    for reason in ["TP", "LOSS", "EVEN", "DD_CUT"]:
        rt = [t for t in trades if t["reason"] == reason]
        if rt:
            print(f"  {reason}: {quick_stats(rt)}")

    # 按网格层数
    for depth in range(1, MAX_GRID_DEPTH+1):
        dt = [t for t in trades if t["n_levels"] == depth]
        if dt:
            print(f"  {depth}层: {quick_stats(dt)}")

    # 按月
    print("\n  月收益:")
    monthly = defaultdict(float)
    for t in trades:
        if t["close_ts"]:
            dt = datetime.fromtimestamp(t["close_ts"], tz=timezone.utc)
            monthly[f"{dt.year}-{dt.month:02d}"] += t["pnl"]
    for mk in sorted(monthly.keys()):
        print(f"    {mk}: {monthly[mk]:+.1f}U")

    mt5.shutdown()


if __name__ == "__main__":
    main()
