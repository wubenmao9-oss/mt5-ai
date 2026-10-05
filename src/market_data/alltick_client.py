"""AllTick 行情客户端"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from typing import Callable, Awaitable

import aiohttp
import websockets

from .models import Tick, KlineBar

logger = logging.getLogger(__name__)

WS_BASE = os.getenv("ALLTICK_WS_BASE", "wss://quote.alltick.co/quote-b-ws-api")
REST_BASE = os.getenv("ALLTICK_REST_BASE", "https://quote.alltick.co/quote-b-api")

KLINE_TYPE_MAP = {
    "1m": 1, "5m": 2, "15m": 3, "30m": 4,
    "1h": 5, "2h": 6, "4h": 7,
    "1d": 8, "1w": 9, "1M": 10,
}
KLINE_TYPE_REVERSE = {v: k for k, v in KLINE_TYPE_MAP.items()}


# ======================================================================
# HTTP 轮询模式（免费计划可用）
# ======================================================================

class AllTickDepthPoller:
    """每 10 秒轮询 HTTP /depth-tick 接口，回调 Tick 对象。

    遇到 429 限流时自动扩大间隔（10s → 15s → 22s ... 最大 120s）。
    """

    def __init__(
        self,
        token: str,
        callback: Callable[[Tick], Awaitable[None] | None],
        rest_base: str = REST_BASE,
    ) -> None:
        self._token = token
        self._callback = callback
        self._rest_base = rest_base
        self._running = False

    async def run(self, symbols: list[str]) -> None:
        self._running = True
        interval = 10.0
        max_interval = 120.0

        await asyncio.sleep(interval)  # 首次等够 10s，不撞 K 线

        while self._running:
            ok = await self._poll(symbols)
            if ok:
                interval = 10.0
            else:
                interval = min(interval * 1.5, max_interval)

            if self._running:
                await asyncio.sleep(interval)

    async def close(self) -> None:
        self._running = False

    async def _poll(self, symbols: list[str]) -> bool:
        """返回 True 成功，False 被限流或失败。"""
        try:
            trace = f"dt-{uuid.uuid4().hex[:12]}"
            query_data = {
                "trace": trace,
                "data": {
                    "symbol_list": [{"code": s} for s in symbols],
                },
            }

            url = (
                f"{self._rest_base}/depth-tick"
                f"?token={self._token}"
                f"&query={_url_encode_json(query_data)}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 429:
                        logger.warning("depth-tick 被限流 (429)，扩大轮询间隔")
                        return False
                    if resp.status != 200:
                        logger.warning("depth-tick (HTTP %d)", resp.status)
                        return False
                    body = await resp.json()

            if body.get("ret") != 200:
                logger.warning("depth-tick 业务错误: %s", body.get("msg", body))
                return False

            tick_list = body.get("data", {}).get("tick_list", [])
            for raw in tick_list:
                tick = self._parse_tick(raw)
                if tick is not None:
                    result = self._callback(tick)
                    if result is not None:
                        await result

            return True

        except Exception:
            logger.exception("depth-tick 轮询异常")
            return False

    @staticmethod
    def _parse_tick(data: dict) -> Tick | None:
        try:
            code = data.get("code", "")
            tick_time = data.get("tick_time", 0)
            ts_int = int(str(tick_time)) if tick_time else 0
            timestamp = (
                datetime.fromtimestamp(ts_int / 1000, tz=timezone.utc)
                if ts_int
                else None
            )

            bids = data.get("bids") or []
            asks = data.get("asks") or []

            bid = float(bids[0]["price"]) if bids else 0.0
            ask = float(asks[0]["price"]) if asks else 0.0
            bid_vol = (
                int(float(bids[0].get("volume", 0)))
                if bids and bids[0].get("volume")
                else 0
            )
            ask_vol = (
                int(float(asks[0].get("volume", 0)))
                if asks and asks[0].get("volume")
                else 0
            )

            return Tick(
                symbol=code,
                bid=bid,
                ask=ask,
                last=None,
                volume=max(bid_vol, ask_vol),
                timestamp=timestamp,
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("解析 depth-tick 失败: %s — %s", exc, data)
            return None


# ======================================================================
# WebSocket 订阅模式（需付费计划）
# ======================================================================

class AllTickWebSocket:
    """AllTick 实时盘口 WebSocket（需基础计划及以上）。"""

    def __init__(
        self,
        token: str,
        callback: Callable[[Tick], Awaitable[None] | None],
        ws_base: str = WS_BASE,
    ) -> None:
        self._ws_url = f"{ws_base}?token={token}"
        self._callback = callback
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._seq_id = 0

    async def run(self, symbols: list[str]) -> None:
        self._running = True
        delay = 1.0
        max_delay = 60.0

        while self._running:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    self._ws = ws
                    delay = 1.0
                    await self._subscribe(symbols)
                    await self._send_heartbeat()
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop()
                    )
                    try:
                        await self._read_loop()
                    finally:
                        heartbeat_task.cancel()
            except websockets.exceptions.InvalidStatus as exc:
                status = exc.response.status_code if exc.response else 0
                if status == 429:
                    cool = 30.0 + random.uniform(0, 5)
                    logger.warning("服务器限流 (HTTP 429)，%.0f 秒后重试...", cool)
                    await asyncio.sleep(cool)
                else:
                    logger.warning(
                        "服务器拒绝连接 (HTTP %d)，%.1f 秒后重试...", status, delay
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2 + random.uniform(0, 1), max_delay)
            except websockets.ConnectionClosed as exc:
                logger.warning(
                    "WS 断开  code=%s  reason=%s  %.1f 秒后重连...",
                    exc.code, exc.reason, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2 + random.uniform(0, 1), max_delay)
            except Exception:
                logger.exception("WS 异常，%.1f 秒后重连...", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2 + random.uniform(0, 1), max_delay)

    async def close(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _subscribe(self, symbols: list[str]) -> None:
        payload = {
            "cmd_id": 22002,
            "seq_id": self._next_seq(),
            "trace": f"sub-{uuid.uuid4().hex[:12]}",
            "data": {
                "symbol_list": [{"code": s} for s in symbols],
            },
        }
        await self._ws.send(json.dumps(payload))
        resp = json.loads(await self._ws.recv())
        if resp.get("ret") != 200:
            raise ConnectionRefusedError(
                f"订阅失败 (cmd_id=22002): {resp.get('msg', resp)}"
            )
        logger.info("订阅成功: %s", symbols)

    async def _send_heartbeat(self) -> None:
        payload = {
            "cmd_id": 22000,
            "seq_id": self._next_seq(),
            "trace": f"hb-{uuid.uuid4().hex[:8]}",
            "data": {},
        }
        await self._ws.send(json.dumps(payload))

    async def _heartbeat_loop(self) -> None:
        while self._running and self._ws and not self._ws.closed:
            await asyncio.sleep(10)
            try:
                await self._send_heartbeat()
            except Exception:
                logger.warning("心跳发送失败，退出心跳循环")
                break

    async def _read_loop(self) -> None:
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            cmd_id = msg.get("cmd_id")
            if cmd_id == 22999:
                tick = self._parse_tick(msg.get("data", {}))
                if tick is not None:
                    result = self._callback(tick)
                    if result is not None:
                        await result
                continue
            if cmd_id == 22001:
                continue
            logger.debug("忽略非推送消息: cmd_id=%s", cmd_id)

    @staticmethod
    def _parse_tick(data: dict) -> Tick | None:
        try:
            code = data.get("code", "")
            tick_time = data.get("tick_time", 0)
            ts_int = int(str(tick_time)) if tick_time else 0
            timestamp = (
                datetime.fromtimestamp(ts_int / 1000, tz=timezone.utc)
                if ts_int
                else None
            )
            bids = data.get("bids") or []
            asks = data.get("asks") or []
            bid = float(bids[0]["price"]) if bids else 0.0
            ask = float(asks[0]["price"]) if asks else 0.0
            bid_vol = (
                int(float(bids[0].get("volume", 0)))
                if bids and bids[0].get("volume")
                else 0
            )
            ask_vol = (
                int(float(asks[0].get("volume", 0)))
                if asks and asks[0].get("volume")
                else 0
            )
            return Tick(
                symbol=code,
                bid=bid,
                ask=ask,
                last=None,
                volume=max(bid_vol, ask_vol),
                timestamp=timestamp,
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("解析推送失败: %s — %s", exc, data)
            return None

    def _next_seq(self) -> int:
        self._seq_id += 1
        return self._seq_id


# ======================================================================
# REST K 线
# ======================================================================

class AllTickRestClient:
    """HTTP 拉取历史 K 线。"""

    def __init__(self, token: str, rest_base: str = REST_BASE) -> None:
        self._token = token
        self._rest_base = rest_base

    async def fetch_kline(
        self,
        symbol: str,
        timeframe: str,
        *,
        limit: int = 100,
        kline_timestamp_end: int = 0,
        adjust_type: int = 0,
    ) -> list[KlineBar]:
        kline_type = KLINE_TYPE_MAP.get(timeframe)
        if kline_type is None:
            raise ValueError(f"不支持的 K 线周期: {timeframe}")

        trace = f"kline-{uuid.uuid4().hex[:12]}"

        query_data = {
            "trace": trace,
            "data": {
                "code": symbol,
                "kline_type": kline_type,
                "kline_timestamp_end": kline_timestamp_end,
                "query_kline_num": limit,
                "adjust_type": adjust_type,
            },
        }

        url = (
            f"{self._rest_base}/kline"
            f"?token={self._token}"
            f"&query={_url_encode_json(query_data)}"
        )

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"K 线请求失败 (HTTP {resp.status})"
                    )
                body = await resp.json()

        if body.get("ret") != 200:
            raise RuntimeError(f"K 线请求失败: {body.get('msg', body)}")

        raw_list = body.get("data", {}).get("kline_list", [])
        tf_label = KLINE_TYPE_REVERSE.get(kline_type, timeframe)

        return [self._parse_bar(symbol, tf_label, b) for b in raw_list]

    @staticmethod
    def _parse_bar(symbol: str, tf: str, raw: dict) -> KlineBar:
        ts = int(raw.get("timestamp", 0))
        timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
        return KlineBar(
            symbol=symbol,
            timeframe=tf,
            open=float(raw.get("open_price", 0)),
            high=float(raw.get("high_price", 0)),
            low=float(raw.get("low_price", 0)),
            close=float(raw.get("close_price", 0)),
            volume=int(float(raw.get("volume", 0))),
            timestamp=timestamp,
        )


def _url_encode_json(data: dict) -> str:
    import urllib.parse
    return urllib.parse.quote(json.dumps(data, separators=(",", ":")))

