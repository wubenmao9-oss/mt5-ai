
"""Strategy parameter configuration"""
from dataclasses import dataclass

MT5_TF_MAP = {0: "H1", 1: "M5", 2: "M15", 3: "M30", 4: "H4", 5: "D1"}

@dataclass
class StrategyConfig:
    symbol: str = "XAUUSD"
    volume: float = 0.01
    trend_tf: int = 0
    trend_sma_fast: int = 20
    trend_sma_slow: int = 50
    trend_rsi_period: int = 14
    entry_tf: int = 1
    entry_bb_period: int = 20
    entry_bb_std: float = 2.0
    entry_rsi_period: int = 7
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    trend_strength_threshold: float = 50.0
    bb_touch_buffer: float = 0.0002
    cooldown_after_trade_minutes: int = 60
    cooldown_no_signal_minutes: int = 60
    cooldown_error_minutes: int = 15
    cleanup_hour: int = 23
    cleanup_minute: int = 55
    sl_atr_multiplier: float = 1.5
    tp_atr_multiplier: float = 3.0
    # B2 Risk Management
    sl_pct: float = 0.0065
    tp_pct: float = 0.0115
    max_trades: int = 3
    max_loss: float = 30.0
    session_start: int = 7
    session_end: int = 20
    strategy_name: str = "TrendMeanReversion"
    strategy_version: str = "1.0.0"

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
