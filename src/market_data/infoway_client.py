"""Infoway 行情客户端 — 盘口 + K 线轮询"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from infoway import InfowayClient, KlineType

from .models import Tick

logger = logging.getLogger(__name__)


class InfowayTicker:
    """Infoway 行情客户端。"""

    def __init__(
        self,
        api_key: str,
        symbol: str = "XAUUSD",
        callback: Callable[[Tick], Awaitable[None] | None] | None = None,
    ) -> None:
        self._client = InfowayClient(api_key=api_key)
        self._symbol = symbol
        self._callback = callback
        self._count = 0

    async def get_kline(self, kline_type: int, count: int = 1):
        """异步获取 K 线。"""
        return await asyncio.to_thread(
            self._client.common.get_kline, self._symbol,
            kline_type=kline_type, count=count,
        )

    async def get_depth(self):
        """异步获取盘口。"""
        return await asyncio.to_thread(self._client.common.get_depth, self._symbol)

    async def run_kline_polling(self, interval: float = 2.0) -> None:
        """轮询最新 1 根 K 线，打印 OHLCV。

        免费计划建议 2 秒以上间隔避免 429。
        """
        logger.info("开始轮询 K 线: %s (间隔 %.1f 秒)", self._symbol, interval)
        bar_type_names = {
            1: "1分钟", 2: "5分钟", 3: "15分钟", 4: "30分钟",
            5: "1小时", 6: "2小时", 7: "4小时",
            8: "日", 9: "周", 10: "月", 11: "季", 12: "年",
        }
        kt = KlineType.HOUR_1  # 1小时线

        while True:
            try:
                raw = await self.get_kline(kt, count=1)
                for item in raw:
                    s = item.get("s", self._symbol)
                    for bar in item.get("respList", []):
                        self._count += 1
                        logger.info(
                            "[%d] %s  O=%-10s H=%-10s L=%-10s C=%-10s "
                            "V=%-10s %s",
                            self._count, s,
                            bar.get("o", "?"), bar.get("h", "?"),
                            bar.get("l", "?"), bar.get("c", "?"),
                            bar.get("v", "?"), bar.get("pc", "?"),
                        )

                        # 同时也传给 callback
                        if self._callback:
                            tick = Tick(
                                symbol=s,
                                bid=float(bar.get("c", 0)),
                                ask=float(bar.get("c", 0)),
                                last=float(bar.get("c", 0)),
                                volume=float(bar.get("v", 0)),
                                timestamp=datetime.fromtimestamp(
                                    float(bar["t"]), tz=timezone.utc
                                ) if bar.get("t") else None,
                            )
                            result = self._callback(tick)
                            if result is not None:
                                await result
            except Exception as e:
                logger.warning("K 线查询异常: %s", e)
            await asyncio.sleep(interval)
