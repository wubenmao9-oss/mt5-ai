"""MT4 EA Bridge — 通过文件 IPC 与 MT4 终端通信。

EA 读取 cmd.txt 执行交易，写入 rsp.txt 返回结果。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Awaitable

from ..market_data.models import Tick

logger = logging.getLogger(__name__)


class MT4Bridge:
    """与 MT4 PythonBridgeEA 通过文件通信的桥接层。"""

    def __init__(self, data_dir: str, callback: Callable[[Tick], Awaitable[None] | None] | None = None) -> None:
        self._files_dir = Path(data_dir) / "Files"
        self._cmd_file = self._files_dir / "cmd.txt"
        self._rsp_file = self._files_dir / "rsp.txt"
        self._callback = callback
        self._connected = False

    @property
    def files_dir(self) -> str:
        return str(self._files_dir)

    async def connect(self, timeout: float = 5.0) -> bool:
        """检查 EA 是否运行（写入一个 ping 命令，等回复）。"""
        self._files_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await self._send_command("quote", timeout=timeout)
            if result.get("STATUS") == "ok":
                self._connected = True
                logger.info("MT4 EA Bridge 连接成功")
                return True
        except (FileNotFoundError, TimeoutError, ValueError):
            pass
        logger.warning("MT4 EA Bridge 未响应 — 请在 MT4 图表上加载 PythonBridgeEA")
        return False

    async def disconnect(self) -> None:
        self._connected = False

    # ------------------------------------------------------------------
    # 命令发送
    # ------------------------------------------------------------------

    async def _send_command(self, action: str, *, timeout: float = 10.0, **kwargs) -> dict:
        """发送命令到 EA 并等待回复。

        Parameters
        ----------
        action : str
            命令名: quote, account, buy, sell, close_all, orders
        timeout : float
            等待超时（秒）
        """
        # 清理旧文件
        self._rsp_file.unlink(missing_ok=True)

        # 写命令文件
        lines = [f"ACTION:{action}"]
        for k, v in kwargs.items():
            lines.append(f"{k.upper()}:{v}")
        self._cmd_file.write_text("\n".join(lines), encoding="ascii")

        # 等 EA 处理（轮询 rsp.txt）
        for _ in range(int(timeout * 10)):
            await asyncio.sleep(0.1)
            if self._rsp_file.exists():
                text = self._rsp_file.read_text(encoding="utf-8", errors="replace")
                self._rsp_file.unlink(missing_ok=True)

                # 解析 key:value 格式
                result = {}
                for line in text.strip().split("\n"):
                    line = line.strip()
                    if ":" in line:
                        k, v = line.split(":", 1)
                        result[k.strip()] = v.strip()

                if result.get("STATUS") == "error":
                    raise RuntimeError(
                        f"MT4 命令失败 [{result.get('ERRCODE', '?')}]: {action}"
                    )
                return result

        raise TimeoutError(f"MT4 EA 未在 {timeout} 秒内响应命令: {action}")

    async def get_quote(self) -> dict:
        """获取当前报价。"""
        return await self._send_command("quote")

    async def get_account_info(self) -> dict:
        """获取账户信息。"""
        return await self._send_command("account")

    async def buy(self, symbol: str, volume: float = 0.01, **kwargs) -> dict:
        """市价买入。"""
        return await self._send_command("buy", symbol=symbol, volume=volume, **kwargs)

    async def sell(self, symbol: str, volume: float = 0.01, **kwargs) -> dict:
        """市价卖出。"""
        return await self._send_command("sell", symbol=symbol, volume=volume, **kwargs)

    async def close_all(self, symbol: str = "") -> dict:
        """平掉所有订单（可选指定品种）。"""
        return await self._send_command("close_all", symbol=symbol)

    async def get_orders(self) -> list[dict]:
        """获取当前持仓列表。"""
        raw = await self._send_command("orders")
        orders = []
        total = int(raw.get("TOTAL", 0))
        for i in range(total):
            key = f"ORDER:{i+1}"
            if key in raw:
                fields = raw[key].split("|")
                order = {}
                for f in fields:
                    if ":" in f:
                        k, v = f.split(":", 1)
                        order[k.lower()] = v
                orders.append(order)
        return orders

    async def start_polling(self, interval: float = 1.0) -> None:
        """不断轮询报价，触发回调。"""
        if not self._connected:
            logger.warning("MT4 未连接，无法启动轮询")
            return

        logger.info("MT4 报价轮询已启动（间隔 %.1f 秒）", interval)
        while self._connected:
            try:
                quote = await self.get_quote()
                tick = Tick(
                    symbol=quote.get("SYMBOL", ""),
                    bid=float(quote.get("BID", 0)),
                    ask=float(quote.get("ASK", 0)),
                    last=None,
                    volume=0,
                    timestamp=None,
                )
                if self._callback:
                    result = self._callback(tick)
                    if result is not None:
                        await result
            except Exception as e:
                logger.debug("报价轮询异常: %s", e)
            await asyncio.sleep(interval)
