"""MT4 API 直连客户端 — ctypes 封装 mt4api.dll

直连 MT4 服务器，不需要本地 MT4 终端。
同时提供 行情订阅 + 交易执行。

使用前需要:
  1. 下载 mt4api.dll / LoginId.dll / LoginX.dll 放在项目目录
  2. 申请试用 License（7天）放入程序目录
  3. 准备好 MT4 账号（模拟/实盘）
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import queue
import threading
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable

from ..market_data.models import Tick

logger = logging.getLogger(__name__)

# ======================================================================
# ctypes 类型别名
# ======================================================================

_WINAPI = ctypes.WINFUNCTYPE
_HANDLE = ctypes.c_void_p

# ======================================================================
# C 数据结构（对应 mt4api.h）
# ======================================================================

class Mt4_stQuoteEventInfo(ctypes.Structure):
    _fields_ = [
        ("szSymbol", ctypes.c_char * 12),
        ("nCount", ctypes.c_int),
        ("nDigits", ctypes.c_int),
        ("nTime", ctypes.c_int),
        ("fAsk", ctypes.c_double),
        ("fBid", ctypes.c_double),
    ]


class Mt4_stTradeRecord(ctypes.Structure):
    _fields_ = [
        ("order", ctypes.c_int),
        ("login", ctypes.c_int),
        ("symbol", ctypes.c_char * 12),
        ("digits", ctypes.c_int),
        ("cmd", ctypes.c_int),
        ("volume", ctypes.c_int),
        ("open_time", ctypes.c_int),
        ("state", ctypes.c_int),
        ("open_price", ctypes.c_double),
        ("sl", ctypes.c_double),
        ("tp", ctypes.c_double),
        ("close_time", ctypes.c_int),
        ("gw_volume", ctypes.c_int),
        ("expiration", ctypes.c_int),
        ("reason", ctypes.c_char),
        ("conv_reserv", ctypes.c_char * 3),
        ("conv_rates", ctypes.c_double * 2),
        ("commission", ctypes.c_double),
        ("commission_agent", ctypes.c_double),
        ("storage", ctypes.c_double),
        ("close_price", ctypes.c_double),
        ("profit", ctypes.c_double),
        ("taxes", ctypes.c_double),
        ("magic", ctypes.c_int),
        ("comment", ctypes.c_char * 32),
        ("gw_order", ctypes.c_int),
        ("activation", ctypes.c_int),
        ("gw_open_price", ctypes.c_short),
        ("gw_close_price", ctypes.c_short),
        ("margin_rate", ctypes.c_double),
        ("timestamp", ctypes.c_int),
        ("api_data", ctypes.c_int * 4),
        ("next", ctypes.c_void_p),
    ]


class Mt4_stOrderNotifyEventInfo(ctypes.Structure):
    _fields_ = [
        ("nReqId", ctypes.c_int),
        ("nStatus", ctypes.c_ubyte),
        ("emType", ctypes.c_int),
        ("tr", Mt4_stTradeRecord),
        ("bid", ctypes.c_double),
        ("ask", ctypes.c_double),
    ]


class Mt4_stOrderUpdateEventInfo(ctypes.Structure):
    _fields_ = [
        ("emUpdateAction", ctypes.c_int),
        ("fBalance", ctypes.c_double),
        ("fCredit", ctypes.c_double),
        ("tr", Mt4_stTradeRecord),
    ]


# 交易指令常量
CMD_BUY = 0
CMD_SELL = 1
CMD_BUY_LIMIT = 2
CMD_SELL_LIMIT = 3
CMD_BUY_STOP = 4
CMD_SELL_STOP = 5

CMD_STR_MAP = {
    "buy": CMD_BUY,
    "sell": CMD_SELL,
    "buy_limit": CMD_BUY_LIMIT,
    "sell_limit": CMD_SELL_LIMIT,
}

# ======================================================================
# 回调函数类型（假设的签名，需跟文档确认）
# ======================================================================

_QuoteCallback = _WINAPI(
    None, ctypes.POINTER(Mt4_stQuoteEventInfo), ctypes.c_void_p
)
_DisconnectCallback = _WINAPI(None, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p)
_OrderNotifyCallback = _WINAPI(
    None, ctypes.POINTER(Mt4_stOrderNotifyEventInfo), ctypes.c_void_p
)
_OrderUpdateCallback = _WINAPI(
    None, ctypes.POINTER(Mt4_stOrderUpdateEventInfo), ctypes.c_void_p
)

# ======================================================================
# MT4 API 客户端
# ======================================================================

class MT4APIClient:
    """mt4api.dll 的 Python 封装。

    提供直连 MT4 服务器的行情订阅和交易执行。
    所有回调通过 asyncio 调度，与异步代码兼容。
    """

    def __init__(
        self,
        dll_dir: str | None = None,
        callback: Callable[[Tick], Awaitable[None] | None] | None = None,
    ) -> None:
        self._callback = callback
        self._nid = -1
        self._connected = False
        self._dll: ctypes.WinDLL | None = None

        # 线程安全的事件队列（DLL 回调 → asyncio 调度）
        self._event_queue: queue.Queue = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

        # 注册的 ctypes 回调（必须保持引用，防止 GC）
        self._cb_quote = None
        self._cb_disconnect = None
        self._cb_order_notify = None

        if dll_dir is None:
            dll_dir = str(Path(__file__).resolve().parent.parent.parent / "lib")
        self._load_dll(dll_dir)

    # ------------------------------------------------------------------
    # DLL 加载
    # ------------------------------------------------------------------

    def _load_dll(self, dll_dir: str) -> None:
        path = Path(dll_dir)
        dll_path = path / "mt4api.dll"
        login_id_path = path / "LoginId.dll"
        login_x_path = path / "LoginX.dll"

        missing = []
        for fp in [dll_path, login_id_path, login_x_path]:
            if not fp.exists():
                missing.append(fp.name)

        if missing:
            logger.warning(
                "MT4 API DLL 缺失: %s。请从 https://github.com/gzhuichou/mt4api "
                "下载 mt4api开发包[1.0.0.2] 放到 %s",
                missing, path.resolve(),
            )
            return

        # 先加载依赖（LoginX.dll 需要先于 mt4api.dll 加载）
        try:
            if login_id_path.exists():
                ctypes.WinDLL(str(login_id_path))
            if login_x_path.exists():
                ctypes.WinDLL(str(login_x_path))
        except Exception as e:
            logger.warning("加载依赖 DLL 失败: %s", e)

        try:
            self._dll = ctypes.WinDLL(str(dll_path))
            self._bind_functions()
            logger.info("mt4api.dll 加载成功")
        except Exception as e:
            logger.error("加载 mt4api.dll 失败: %s", e)

    def _bind_functions(self) -> None:
        dll = self._dll
        if dll is None:
            return

        dll.MT4API_Create.argtypes = []
        dll.MT4API_Create.restype = ctypes.c_int

        dll.MT4API_Destory.argtypes = [ctypes.c_int]
        dll.MT4API_Destory.restype = None

        dll.MT4API_Init.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,        # pszBroker
            ctypes.c_uint,          # nUser
            ctypes.c_char_p,        # pszPassword
            ctypes.c_char_p,        # pszHost
            ctypes.c_int,           # nPort
            ctypes.c_int,           # nTradeHisTimeFrom
            ctypes.c_int,           # nTradeHisTimeTo
        ]
        dll.MT4API_Init.restype = ctypes.c_bool

        dll.MT4API_Connect.argtypes = [ctypes.c_int]
        dll.MT4API_Connect.restype = ctypes.c_bool

        dll.MT4API_IsConnect.argtypes = [ctypes.c_int]
        dll.MT4API_IsConnect.restype = ctypes.c_bool

        dll.MT4API_DisConnect.argtypes = [ctypes.c_int]
        dll.MT4API_DisConnect.restype = ctypes.c_bool

        dll.MT4API_Subscribe.argtypes = [ctypes.c_int, ctypes.c_char_p]
        dll.MT4API_Subscribe.restype = ctypes.c_bool

        dll.MT4API_UnSubscribe.argtypes = [ctypes.c_int, ctypes.c_char_p]
        dll.MT4API_UnSubscribe.restype = ctypes.c_bool

        dll.MT4API_SetQuoteEventHandler.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ]
        dll.MT4API_SetQuoteEventHandler.restype = ctypes.c_bool

        dll.MT4API_SetDisconnectEventHandler.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ]
        dll.MT4API_SetDisconnectEventHandler.restype = ctypes.c_bool

        dll.MT4API_SetOrderNotifyEventHandler.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ]
        dll.MT4API_SetOrderNotifyEventHandler.restype = ctypes.c_bool

        dll.MT4API_GetQuote.argtypes = [
            ctypes.c_int, ctypes.c_char_p,
            ctypes.POINTER(Mt4_stQuoteEventInfo),
        ]
        dll.MT4API_GetQuote.restype = ctypes.c_bool

        dll.MT4API_GetMoneyInfo.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),  # fBalance
            ctypes.POINTER(ctypes.c_double),  # fCredit
            ctypes.POINTER(ctypes.c_double),  # fMargin
            ctypes.POINTER(ctypes.c_double),  # fFreeMargin
            ctypes.POINTER(ctypes.c_double),  # fEquity
            ctypes.POINTER(ctypes.c_double),  # fProfit
        ]
        dll.MT4API_GetMoneyInfo.restype = ctypes.c_bool

        dll.MT4API_OrderSend.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,        # pszSymbol
            ctypes.c_ubyte,         # nTradeCmd
            ctypes.c_double,        # volume
            ctypes.c_double,        # price
            ctypes.c_int,           # ie_deviation
            ctypes.c_double,        # stoploss
            ctypes.c_double,        # takeprofit
            ctypes.c_char_p,        # pszComment
            ctypes.c_int,           # magic
            ctypes.c_int,           # expiration
            ctypes.POINTER(Mt4_stTradeRecord),
        ]
        dll.MT4API_OrderSend.restype = ctypes.c_bool

        dll.MT4API_OrderSendAsync.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,        # pszSymbol
            ctypes.c_ubyte,         # nTradeCmd
            ctypes.c_double,        # volume
            ctypes.c_double,        # price
            ctypes.c_int,           # ie_deviation
            ctypes.c_double,        # stoploss
            ctypes.c_double,        # takeprofit
            ctypes.c_char_p,        # pszComment
            ctypes.c_int,           # magic
            ctypes.c_int,           # expiration
        ]
        dll.MT4API_OrderSendAsync.restype = ctypes.c_int

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return self._dll is not None

    async def connect(
        self,
        broker: str,
        user: int,
        password: str,
        host: str,
        port: int,
        *,
        trade_history_from: int = 0,
        trade_history_to: int = 0,
    ) -> None:
        """连接 MT4 服务器。

        Parameters
        ----------
        broker : str
            券商名称（随意，仅用于日志）
        user : int
            MT4 账号
        password : str
            MT4 密码
        host : str
            服务器地址（IP 或域名）
        port : int
            服务器端口（通常是 443）
        """
        if self._dll is None:
            raise RuntimeError("mt4api.dll 未加载，请先下载 DLL 文件")

        self._nid = self._dll.MT4API_Create()
        if self._nid < 0:
            raise RuntimeError("MT4API_Create 失败")

        self._loop = asyncio.get_running_loop()

        # 注册回调
        self._register_callbacks()

        # 初始化
        ok = self._dll.MT4API_Init(
            self._nid,
            broker.encode(),
            user,
            password.encode(),
            host.encode(),
            port,
            trade_history_from,
            trade_history_to,
        )
        if not ok:
            raise RuntimeError(f"MT4API_Init 失败: {self._get_last_error()}")

        # 连接
        ok = self._dll.MT4API_Connect(self._nid)
        if not ok:
            raise RuntimeError(f"MT4API_Connect 失败: {self._get_last_error()}")

        self._connected = True
        logger.info("MT4 直连成功: %s@%s:%d (账号 %d)", broker, host, port, user)

        # 启动事件处理协程
        asyncio.create_task(self._event_loop())

    async def disconnect(self) -> None:
        if self._dll and self._nid >= 0:
            self._dll.MT4API_DisConnect(self._nid)
            self._dll.MT4API_Destory(self._nid)
        self._connected = False

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def _register_callbacks(self) -> None:
        dll = self._dll
        nid = self._nid
        if dll is None:
            return

        # 报价回调
        self._cb_quote = _QuoteCallback(self._on_quote_event)
        dll.MT4API_SetQuoteEventHandler(nid, self._cb_quote, None)

        # 断线回调
        self._cb_disconnect = _DisconnectCallback(self._on_disconnect_event)
        dll.MT4API_SetDisconnectEventHandler(nid, self._cb_disconnect, None)

    def _on_quote_event(self, p_info: ctypes.POINTER(Mt4_stQuoteEventInfo), param) -> None:
        """DLL 线程调用的报价回调。"""
        info = p_info.contents
        tick = Tick(
            symbol=info.szSymbol.decode("utf-8", errors="replace"),
            bid=info.fBid,
            ask=info.fAsk,
            last=None,
            volume=0,
            timestamp=datetime.fromtimestamp(info.nTime, tz=timezone.utc)
            if info.nTime else None,
        )
        self._event_queue.put(("quote", tick))

    def _on_disconnect_event(self, p_code, param) -> None:
        self._event_queue.put(("disconnect", None))

    # ------------------------------------------------------------------
    # 事件调度（asyncio 事件循环）
    # ------------------------------------------------------------------

    async def _event_loop(self) -> None:
        while self._connected:
            try:
                event_type, data = self._event_queue.get(timeout=1)
            except queue.Empty:
                continue

            if event_type == "quote":
                if self._callback:
                    result = self._callback(data)
                    if result is not None:
                        await result
            elif event_type == "disconnect":
                logger.warning("MT4 服务器断开连接")
                self._connected = False
                break

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------

    async def subscribe(self, symbol: str) -> None:
        """订阅实时报价。"""
        if self._dll is None:
            return
        ok = self._dll.MT4API_Subscribe(self._nid, symbol.encode())
        if not ok:
            raise RuntimeError(f"订阅 {symbol} 失败: {self._get_last_error()}")
        logger.info("订阅成功: %s", symbol)

    async def get_quote(self, symbol: str) -> Tick | None:
        """主动获取当前报价。"""
        if self._dll is None:
            return None
        info = Mt4_stQuoteEventInfo()
        ok = self._dll.MT4API_GetQuote(self._nid, symbol.encode(), ctypes.byref(info))
        if not ok:
            return None
        return Tick(
            symbol=info.szSymbol.decode("utf-8", errors="replace"),
            bid=info.fBid,
            ask=info.fAsk,
            timestamp=datetime.fromtimestamp(info.nTime, tz=timezone.utc)
            if info.nTime else None,
        )

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------

    async def buy_market(
        self,
        symbol: str,
        volume: float = 0.01,
        *,
        deviation: int = 10,
        comment: str = "",
        magic: int = 0,
    ) -> dict:
        """市价买入。"""
        return await self._order_send(symbol, CMD_BUY, volume, 0.0, deviation, comment, magic)

    async def sell_market(
        self,
        symbol: str,
        volume: float = 0.01,
        *,
        deviation: int = 10,
        comment: str = "",
        magic: int = 0,
    ) -> dict:
        """市价卖出。"""
        return await self._order_send(symbol, CMD_SELL, volume, 0.0, deviation, comment, magic)

    async def place_limit_order(
        self,
        symbol: str,
        price: float,
        volume: float = 0.01,
        *,
        order_type: str = "buy_limit",
        deviation: int = 10,
        comment: str = "",
        magic: int = 0,
    ) -> dict:
        """挂单交易。"""
        cmd = CMD_STR_MAP.get(order_type)
        if cmd is None:
            raise ValueError(f"不支持的挂单类型: {order_type}")
        return await self._order_send(symbol, cmd, volume, price, deviation, comment, magic)

    async def _order_send(
        self,
        symbol: str,
        cmd: int,
        volume: float,
        price: float,
        deviation: int,
        comment: str,
        magic: int,
    ) -> dict:
        if self._dll is None:
            raise RuntimeError("mt4api.dll 未加载")

        tr = Mt4_stTradeRecord()
        ok = self._dll.MT4API_OrderSend(
            self._nid,
            symbol.encode(),
            ctypes.c_ubyte(cmd),
            volume,
            price,
            deviation,
            0.0,    # stoploss
            0.0,    # takeprofit
            (comment or "python order").encode(),
            magic,
            0,      # expiration
            ctypes.byref(tr),
        )

        result = {
            "success": bool(ok),
            "cmd": cmd,
            "symbol": symbol,
            "volume": volume,
            "price": price,
        }

        if ok:
            result.update({
                "order": tr.order,
                "open_price": tr.open_price,
                "open_time": tr.open_time,
            })
        else:
            result["error"] = self._get_last_error()

        return result

    # ------------------------------------------------------------------
    # 账户
    # ------------------------------------------------------------------

    async def get_account_info(self) -> dict:
        """获取账户信息。"""
        if self._dll is None:
            return {}
        balance = ctypes.c_double()
        credit = ctypes.c_double()
        margin = ctypes.c_double()
        free_margin = ctypes.c_double()
        equity = ctypes.c_double()
        profit = ctypes.c_double()

        ok = self._dll.MT4API_GetMoneyInfo(
            self._nid,
            ctypes.byref(balance),
            ctypes.byref(credit),
            ctypes.byref(margin),
            ctypes.byref(free_margin),
            ctypes.byref(equity),
            ctypes.byref(profit),
        )
        if not ok:
            return {}
        return {
            "balance": balance.value,
            "credit": credit.value,
            "margin": margin.value,
            "free_margin": free_margin.value,
            "equity": equity.value,
            "profit": profit.value,
        }

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _get_last_error(self) -> str:
        if self._dll is None:
            return "dll not loaded"
        if not hasattr(self._dll, "MT4API_GetLastError"):
            return "unknown"
        err = self._dll.MT4API_GetLastError(self._nid, 0)
        return f"error_code={err}"

    def __del__(self) -> None:
        if self._connected:
            try:
                self._dll.MT4API_DisConnect(self._nid)
                self._dll.MT4API_Destory(self._nid)
            except Exception:
                pass
