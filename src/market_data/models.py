import dataclasses
from datetime import datetime


@dataclasses.dataclass
class Tick:
    """实时 Tick 数据结构"""
    symbol: str
    bid: float
    ask: float
    last: float | None = None
    volume: int = 0
    timestamp: datetime | None = None

    @property
    def spread(self) -> float:
        return round(self.ask - self.bid, 5)


@dataclasses.dataclass
class KlineBar:
    """K 线数据结构"""
    symbol: str
    timeframe: str          # e.g. "1m", "5m", "1h", "1d"
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime

    @property
    def range(self) -> float:
        return round(self.high - self.low, 5)
