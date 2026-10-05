
"""Backtester ?  M5/H1 """

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

import MetaTrader5 as mt5
import numpy as np

from ..trading.mt5_executor import MT5Executor
from .config import StrategyConfig, MT5_TF_MAP
from .indicators import SMA, RSI, BollingerBands, ATR

logger = logging.getLogger(__name__)

TF_MT5_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


# ======================================================================
# ?
# ======================================================================

@dataclass
class BacktestTrade:
    entry_time: str
    exit_time: str
    direction: str          # buy / sell
    entry_price: float
    exit_price: float
    sl: float
    tp: float
    profit: float
    profit_pips: float
    exit_reason: str        # tp_hit / sl_hit / still_open
    entry_bar: int
    exit_bar: int


@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_profit: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    max_consecutive_losses: int = 0
    sharpe: float = 0.0
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

    def pprint(self) -> None:
        print("=" * 55)
        print("  BACKTEST RESULT")
        print("=" * 55)
        print(f"  Total Trades:      {self.total_trades}")
        print(f"  Wins:              {self.wins}")
        print(f"  Losses:            {self.losses}")
        print(f"  Win Rate:          {self.win_rate:.1f}%")
        print(f"  Total Profit:      {self.total_profit:+.2f} USD")
        print(f"  Profit Factor:     {self.profit_factor:.2f}")
        print(f"  Avg Win:           {self.avg_win:+.2f}")
        print(f"  Avg Loss:          {self.avg_loss:+.2f}")
        print(f"  Max Drawdown:      {self.max_drawdown:.2f} USD")
        print(f"  Max Consec Losses: {self.max_consecutive_losses}")
        print(f"  Sharpe (approx):   {self.sharpe:.2f}")
        print("-" * 55)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 1),
            "total_profit": round(self.total_profit, 2),
            "profit_factor": round(self.profit_factor, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "max_consecutive_losses": self.max_consecutive_losses,
            "sharpe": round(self.sharpe, 2),
        }


# ======================================================================
# ======================================================================

class Backtester:
    """?? M5 / H1 ?"""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self._cfg = config or StrategyConfig()

    async def run(
        self,
        symbol: str = "XAUUSD",
        lookback_days: int = 30,
        min_trades: int = 5,
    ) -> BacktestResult:
        logger.info("?: %s  %d ?", symbol, lookback_days)

        # 1. ? K ?
        m5_data = await self._fetch_bars(symbol, "M5", lookback_days)
        h1_data = await self._fetch_bars(symbol, "H1", lookback_days * 2)

        if m5_data is None or h1_data is None or len(m5_data) < 200 or len(h1_data) < 100:
            return BacktestResult(total_trades=0, win_rate=0.0)

        logger.info("??: M5=%d ?  H1=%d ?", len(m5_data), len(h1_data))

        # ?? numpy ??
        m5_close = np.array([b["close"] for b in m5_data], dtype=float)
        m5_high = np.array([b["high"] for b in m5_data], dtype=float)
        m5_low = np.array([b["low"] for b in m5_data], dtype=float)
        m5_times = [b["time"] for b in m5_data]

        h1_close = np.array([b["close"] for b in h1_data], dtype=float)
        h1_high = np.array([b["high"] for b in h1_data], dtype=float)
        h1_low = np.array([b["low"] for b in h1_data], dtype=float)
        h1_times = [b["time"] for b in h1_data]

        # 2.
        h1_sma_fast = SMA(h1_close, self._cfg.trend_sma_fast)
        h1_sma_slow = SMA(h1_close, self._cfg.trend_sma_slow)
        h1_rsi_arr = RSI(h1_close, self._cfg.trend_rsi_period)
        h1_atr_arr = ATR(h1_high, h1_low, h1_close, 14)

        m5_bb_mid, m5_bb_up, m5_bb_low = BollingerBands(
            m5_close, self._cfg.entry_bb_period, self._cfg.entry_bb_std
        )
        m5_rsi_arr = RSI(m5_close, self._cfg.entry_rsi_period)

        # 3. ? M5 Bar ??
        warmup = max(self._cfg.trend_sma_slow, self._cfg.entry_bb_period) + 10
        trades: list[BacktestTrade] = []
        in_position: BacktestTrade | None = None
        cooldown_until: float = 0.0
        equity_curve: list[float] = []
        balance = 500.0  #

        for i in range(warmup, len(m5_data)):
            t = m5_times[i]

            # ? H1  H1 bar?
            h1_idx = None
            for j in range(len(h1_times) - 1, -1, -1):
                if h1_times[j] <= t:
                    h1_idx = j
                    break
            if h1_idx is None or h1_idx < self._cfg.trend_sma_slow:
                continue

            # ?? H1
            cur_sma_f = float(h1_sma_fast[h1_idx])
            cur_sma_s = float(h1_sma_slow[h1_idx])
            cur_rsi_h1 = float(h1_rsi_arr[h1_idx])
            cur_atr = float(h1_atr_arr[h1_idx])

            # ?? M5
            cur_bb_u = float(m5_bb_up[i])
            cur_bb_l = float(m5_bb_low[i])
            cur_rsi_m5 = float(m5_rsi_arr[i])
            cur_price = float(m5_close[i])

            if math.isnan(cur_sma_f) or math.isnan(cur_bb_u):
                continue

            # --- ?? SL/TP ---
            if in_position is not None:
                info = self._check_sl_tp(in_position, m5_high[i], m5_low[i], i, t)
                if info is not None:
                    in_position.exit_time = info["time"]
                    in_position.exit_price = info["price"]
                    in_position.exit_reason = info["reason"]
                    in_position.profit = info["profit"]
                    in_position.profit_pips = info["pips"]
                    in_position.exit_bar = i
                    balance += in_position.profit
                    trades.append(in_position)
                    equity_curve.append(balance)
                    in_position = None
                    cooldown_until = t + self._cfg.cooldown_after_trade_minutes * 60
                    logger.debug(
                        "?? %s  %s  profit=%.2f",
                        info["reason"], "buy" if "buy" in info.get("dir", "") else "sell",
                        info["profit"],
                    )
                    continue
                # ?
                if in_position.direction == "buy":
                    unrealized = (cur_price - in_position.entry_price) / in_position.entry_price * balance if balance > 0 else 0
                else:
                    unrealized = (in_position.entry_price - cur_price) / in_position.entry_price * balance if balance > 0 else 0
                equity_curve.append(balance + unrealized)
                continue

            # ---  ---
            if t < cooldown_until:
                equity_curve.append(balance if equity_curve else balance)
                continue

            # --- ? ---
            threshold = self._cfg.trend_strength_threshold
            if cur_sma_f > cur_sma_s and cur_rsi_h1 > threshold:
                trend = "bullish"
            elif cur_sma_f < cur_sma_s and cur_rsi_h1 < (100 - threshold):
                trend = "bearish"
            else:
                #  ? ?? cooldown?
                cooldown_until = t + self._cfg.cooldown_no_signal_minutes * 60
                equity_curve.append(balance if equity_curve else balance)
                continue

            # --- ? ---
            buffer = self._cfg.bb_touch_buffer * cur_price
            sl_dist = cur_atr * self._cfg.sl_atr_multiplier if not math.isnan(cur_atr) and cur_atr > 0 else 50.0
            tp_dist = cur_atr * self._cfg.tp_atr_multiplier if not math.isnan(cur_atr) and cur_atr > 0 else 100.0

            signal = None
            if trend == "bullish":
                if cur_bb_l > 0 and cur_price <= cur_bb_l + buffer:
                    if cur_rsi_m5 < self._cfg.rsi_oversold:
                        signal = ("buy", cur_price)
            else:
                if cur_bb_u > 0 and cur_price >= cur_bb_u - buffer:
                    if cur_rsi_m5 > self._cfg.rsi_overbought:
                        signal = ("sell", cur_price)

            if signal is not None:
                direction, entry_price = signal
                sl = entry_price - sl_dist if direction == "buy" else entry_price + sl_dist
                tp = entry_price + tp_dist if direction == "buy" else entry_price - tp_dist
                in_position = BacktestTrade(
                    entry_time=datetime.fromtimestamp(t, tz=timezone.utc).isoformat(),
                    exit_time="",
                    direction=direction,
                    entry_price=round(entry_price, 2),
                    exit_price=0.0,
                    sl=round(sl, 2),
                    tp=round(tp, 2),
                    profit=0.0,
                    profit_pips=0.0,
                    exit_reason="",
                    entry_bar=i,
                    exit_bar=0,
                )
                logger.debug(
                    "?? %s @ %.2f  sl=%.2f  tp=%.2f  (bar %d)",
                    direction, entry_price, sl, tp, i,
                )

            equity_curve.append(balance if equity_curve else balance)

        # 4. ?
        result = self._calc_stats(trades, equity_curve)
        logger.info("?: %d   ?? %.1f%%  ?? %.2f",
                     result.total_trades, result.win_rate, result.total_profit)
        return result

    # ------------------------------------------------------------------
    # ?
    # ------------------------------------------------------------------

    @staticmethod
    def _check_sl_tp(
        trade: BacktestTrade, bar_high: float, bar_low: float, bar_idx: int, bar_time: float
    ) -> dict | None:
        if trade.direction == "buy":
            if bar_low <= trade.sl:
                profit_pips = trade.sl - trade.entry_price
                return {"time": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                        "price": trade.sl, "reason": "sl_hit",
                        "profit": round(min(profit_pips, -0.01), 2),
                        "pips": round(profit_pips, 2), "dir": "buy"}
            if bar_high >= trade.tp:
                profit_pips = trade.tp - trade.entry_price
                return {"time": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                        "price": trade.tp, "reason": "tp_hit",
                        "profit": round(max(profit_pips, 0.01), 2),
                        "pips": round(profit_pips, 2), "dir": "buy"}
        else:
            if bar_high >= trade.sl:
                profit_pips = trade.entry_price - trade.sl
                return {"time": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                        "price": trade.sl, "reason": "sl_hit",
                        "profit": round(min(profit_pips, -0.01), 2),
                        "pips": round(profit_pips, 2), "dir": "sell"}
            if bar_low <= trade.tp:
                profit_pips = trade.entry_price - trade.tp
                return {"time": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                        "price": trade.tp, "reason": "tp_hit",
                        "profit": round(max(profit_pips, 0.01), 2),
                        "pips": round(profit_pips, 2), "dir": "sell"}
        return None

    @staticmethod
    def _calc_stats(trades: list[BacktestTrade], equity: list[float]) -> BacktestResult:
        closed = [t for t in trades if t.exit_reason in ("tp_hit", "sl_hit")]
        total = len(closed)
        if total == 0:
            return BacktestResult()

        wins = [t for t in closed if t.profit > 0]
        losses = [t for t in closed if t.profit <= 0]
        profits = [t.profit for t in closed]

        total_profit = sum(profits)
        avg_win = sum(t.profit for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.profit for t in losses) / len(losses) if losses else -1.0
        win_rate = len(wins) / total * 100
        profit_factor = (
            sum(t.profit for t in wins) / sum(abs(t.profit) for t in losses)
            if losses and sum(abs(t.profit) for t in losses) > 0
            else 999.0
        )

        # Max consecutive losses
        max_cl = 0
        cur_cl = 0
        for t in closed:
            if t.profit <= 0:
                cur_cl += 1
                max_cl = max(max_cl, cur_cl)
            else:
                cur_cl = 0

        # Max drawdown
        peak = equity[0] if equity else 500.0
        dd = 0.0
        for v in equity:
            if v > peak:
                peak = v
            dd = max(dd, peak - v)

        # Sharp (??)
        returns = []
        for j in range(1, len(equity)):
            if equity[j - 1] != 0:
                returns.append((equity[j] - equity[j - 1]) / equity[j - 1])
        sharpe = 0.0
        if len(returns) > 1 and np.std(returns) > 0:
            sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(288))  # 288 M5 per day

        return BacktestResult(
            total_trades=total,
            wins=len(wins),
            losses=len(losses),
            win_rate=round(win_rate, 1),
            total_profit=round(total_profit, 2),
            profit_factor=round(profit_factor, 2),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            max_drawdown=round(dd, 2),
            max_consecutive_losses=max_cl,
            sharpe=round(sharpe, 2),
            trades=trades,
            equity_curve=equity,
        )

    @staticmethod
    async def _fetch_bars(symbol: str, tf_name: str, days: int) -> list[dict] | None:
        mt5_tf = TF_MT5_MAP.get(tf_name, mt5.TIMEFRAME_M5)
        seconds = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
        count = int(days * 86400 / seconds.get(tf_name, 300)) + 200

        def _fetch():
            rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, count)
            if rates is None:
                return None
            return [
                {"time": r[0], "open": float(r[1]), "high": float(r[2]),
                 "low": float(r[3]), "close": float(r[4]), "volume": int(r[5])}
                for r in rates
            ]

        return await MT5Executor._run_in_executor(_fetch)
