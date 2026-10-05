
"""Technical indicator calculations using numpy"""

import numpy as np


def SMA(prices: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average"""
    if len(prices) < period:
        return np.full_like(prices, np.nan)
    result = np.full_like(prices, np.nan)
    for i in range(period - 1, len(prices)):
        result[i] = np.mean(prices[i - period + 1 : i + 1])
    return result


def EMA(prices: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average"""
    if len(prices) < period:
        return np.full_like(prices, np.nan)
    result = np.full_like(prices, np.nan)
    multiplier = 2.0 / (period + 1)
    result[period - 1] = np.mean(prices[:period])
    for i in range(period, len(prices)):
        result[i] = (prices[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def RSI(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index"""
    if len(prices) < period + 1:
        return np.full_like(prices, np.nan)
    deltas = np.diff(prices)
    result = np.full_like(prices, np.nan)
    for i in range(period, len(prices)):
        window = deltas[i - period : i]
        gains = window[window > 0].sum()
        losses = -window[window < 0].sum()
        if losses == 0:
            result[i] = 100.0
        else:
            rs = gains / losses
            result[i] = 100.0 - (100.0 / (1.0 + rs))
    return result


def BollingerBands(
    prices: np.ndarray, period: int = 20, std_dev: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (middle, upper, lower)"""
    middle = SMA(prices, period)
    std = np.full_like(prices, np.nan)
    for i in range(period - 1, len(prices)):
        std[i] = np.std(prices[i - period + 1 : i + 1])
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return middle, upper, lower


def ATR(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range"""
    if len(close) < 2:
        return np.full_like(close, np.nan)
    tr = np.full_like(close, np.nan)
    for i in range(1, len(close)):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)
    result = np.full_like(close, np.nan)
    result[period] = np.mean(tr[1 : period + 1])
    for i in range(period + 1, len(close)):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result


def MACD(
    prices: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (macd_line, signal_line, histogram)"""
    ema_fast = EMA(prices, fast)
    ema_slow = EMA(prices, slow)
    macd_line = ema_fast - ema_slow
    signal_line = EMA(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def SuperTrend(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               period: int = 10, multiplier: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    """SuperTrend 指标

    Args:
        high, low, close: 价格数组
        period: ATR周期 (默认10)
        multiplier: 倍数 (默认3.0)

    Returns:
        (trend_direction, supertrend_line)
        trend_direction: 1=多头, -1=空头, NaN=无效
        supertrend_line: SuperTrend线位置
    """
    n = len(close)
    atr_arr = ATR(high, low, close, period)

    trend = np.full(n, np.nan)
    st_line = np.full(n, np.nan)

    if n < period + 1:
        return trend, st_line

    start = period
    if np.isnan(atr_arr[start]):
        return trend, st_line

    hl2 = (high + low) / 2.0
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)

    for i in range(start, n):
        if np.isnan(atr_arr[i]):
            continue
        upper[i] = hl2[i] + multiplier * atr_arr[i]
        lower[i] = hl2[i] - multiplier * atr_arr[i]

    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)

    final_upper[start] = upper[start]
    final_lower[start] = lower[start]
    trend[start] = 1 if close[start] > final_upper[start] else -1
    st_line[start] = final_lower[start] if trend[start] == 1 else final_upper[start]

    for i in range(start + 1, n):
        if np.isnan(final_upper[i-1]) or np.isnan(upper[i]):
            final_upper[i] = upper[i]
        else:
            final_upper[i] = upper[i] if close[i-1] > final_upper[i-1] else min(upper[i], final_upper[i-1])

        if np.isnan(final_lower[i-1]) or np.isnan(lower[i]):
            final_lower[i] = lower[i]
        else:
            final_lower[i] = lower[i] if close[i-1] < final_lower[i-1] else max(lower[i], final_lower[i-1])

        if np.isnan(trend[i-1]):
            trend[i] = 1 if close[i] > final_upper[i] else -1
        else:
            if trend[i-1] == 1 and close[i] < final_lower[i]:
                trend[i] = -1
            elif trend[i-1] == -1 and close[i] > final_upper[i]:
                trend[i] = 1
            else:
                trend[i] = trend[i-1]

        st_line[i] = final_lower[i] if trend[i] == 1 else final_upper[i]

    return trend, st_line


def QQE(prices: np.ndarray, rsi_period: int = 14, qqe_factor: float = 3.0,
        wilders_period: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """QQE (Quantitative Qualitative Estimation) 指标

    基于RSI的趋势动量指标, 类似谷歌黄金策略中的用法

    Args:
        prices: 价格数组
        rsi_period: RSI周期 (默认14)
        qqe_factor: QQE倍数 (默认3.0)
        wilders_period: Wilder平滑周期 (默认5)

    Returns:
        (qqe_direction, qqe_line)
        qqe_direction: 1=多头, -1=空头, 0=中性, NaN=无效
        qqe_line: QQE线值
    """
    n = len(prices)
    if n < rsi_period + wilders_period + 2:
        return np.full(n, np.nan), np.full(n, np.nan)

    rsi_arr = RSI(prices, rsi_period)
    smoothed_rsi = EMA(rsi_arr, wilders_period)

    rsi_high = np.full(n, np.nan)
    rsi_low = np.full(n, np.nan)
    rsi_close_val = np.full(n, np.nan)

    for i in range(rsi_period, n):
        if not np.isnan(smoothed_rsi[i]):
            rsi_close_val[i] = smoothed_rsi[i]
            rsi_high[i] = rsi_close_val[i]
            rsi_low[i] = rsi_close_val[i]

    rsi_atr = ATR(rsi_high, rsi_low, rsi_close_val, wilders_period)

    qqe_line = np.full(n, np.nan)
    qqe_direction = np.full(n, np.nan)

    for i in range(rsi_period + wilders_period, n):
        if np.isnan(rsi_atr[i]) or np.isnan(smoothed_rsi[i]):
            continue
        qqe_line[i] = rsi_atr[i] * qqe_factor

        if i > rsi_period + wilders_period and not np.isnan(qqe_line[i-1]):
            if smoothed_rsi[i] > 50 + qqe_line[i]:
                qqe_direction[i] = 1
            elif smoothed_rsi[i] < 50 - qqe_line[i]:
                qqe_direction[i] = -1
            else:
                qqe_direction[i] = qqe_direction[i-1] if not np.isnan(qqe_direction[i-1]) else 0
        else:
            qqe_direction[i] = 0

    return qqe_direction, qqe_line
