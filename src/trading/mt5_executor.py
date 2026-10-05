"""MT5 ??

?
  - buy_market(symbol, volume=0.01)      ?
  - place_limit_order(symbol, price, volume=0.01)  ? BUY/SELL_LIMIT?

?start_quote_stream(symbols, callback) ? ? Tick ?
?? data/memory.json??
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Awaitable, ClassVar

import MetaTrader5 as mt5

from ..market_data.models import Tick

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MEMORY_FILE = MEMORY_DIR / "memory.json"


# ======================================================================
#  ? ?
# ======================================================================

@dataclass
class TradeRecord:
    """?"""
    time: str
    action: str          # buy, sell, buy_limit, sell_limit
    symbol: str
    volume: float
    price: float
    order: int
    deal: int
    balance_before: float
    balance_after: float
    profit: float = 0.0

@dataclass
class Memory:
    """ JSON"""

    trades: list[dict] = field(default_factory=list)
    balance_snapshots: list[dict] = field(default_factory=list)
    total_trades: int = 0
    total_profit: float = 0.0

    def record_trade(self, trade: TradeRecord) -> None:
        d = asdict(trade)
        self.trades.append(d)
        self.total_trades += 1
        self.total_profit = round(self.total_profit + trade.profit, 2)

    def snapshot_balance(self, balance: float, equity: float) -> None:
        self.balance_snapshots.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "balance": balance,
            "equity": equity,
        })

    def save(self) -> None:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "total_trades": self.total_trades,
            "total_profit": self.total_profit,
            "trades": self.trades[-200:],          # ?? 200 ?
            "balance_snapshots": self.balance_snapshots[-500:],
        }
        MEMORY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls) -> Memory:
        if not MEMORY_FILE.exists():
            return cls()
        try:
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            m = cls(
                trades=data.get("trades", []),
                balance_snapshots=data.get("balance_snapshots", []),
                total_trades=data.get("total_trades", 0),
                total_profit=data.get("total_profit", 0.0),
            )
            logger.info("Mem: %d trades, PnL: %.2f", m.total_trades, m.total_profit)
            return m
        except Exception as e:
            logger.warning(": %s", e)
            return cls()


# ======================================================================
# MT5
# ======================================================================

class MT5Executor:
    _instance: ClassVar[MT5Executor | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _executor: ClassVar[ThreadPoolExecutor] = ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="mt5"
    )

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._connected = False
        self._memory: Memory = Memory.load()
        self._stream_task: asyncio.Task | None = None
        self._emailer = None
        self._strategy_name = "MT5"
        # Order rate limiter: exponential backoff on consecutive failures
        self._last_order_time = 0.0
        self._min_order_interval = 5.0  # seconds (base)
        self._consecutive_failures = 0
        # Cached filling mode per symbol
        self._filling_cache: dict[str, int] = {}

    # ----------------------------------------------------------------
    #  Filling mode auto-detect
    # ----------------------------------------------------------------

    def _get_filling(self, symbol: str) -> int:
        """Auto-detect the correct filling mode for a symbol.

        Different brokers support different filling types:
        - ORDER_FILLING_FOK (0): Fill or Kill
        - ORDER_FILLING_IOC (1): Immediate or Cancel
        - ORDER_FILLING_RETURN (2): Return (guaranteed for pending orders)

        We check symbol_info.filling_mode flags to pick the right one.
        """
        if symbol in self._filling_cache:
            return self._filling_cache[symbol]
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC  # fallback
        fm = info.filling_mode
        # filling_mode is a bitmask: bit0=FOK, bit1=IOC, bit2=RETURN
        if fm & 2:  # IOC supported
            filling = mt5.ORDER_FILLING_IOC
        elif fm & 1:  # FOK supported
            filling = mt5.ORDER_FILLING_FOK
        else:
            filling = mt5.ORDER_FILLING_RETURN  # always available
        self._filling_cache[symbol] = filling
        logger.info("Filling mode for %s: %d (filling_mode=%d)", symbol, filling, fm)
        return filling

    # ----------------------------------------------------------------
    # ?? & ??
    # ----------------------------------------------------------------

    @classmethod
    def get_instance(
        cls, login: int | None = None, password: str | None = None, server: str | None = None
    ) -> MT5Executor:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = cls()
                    instance._connect(login, password, server)
                    cls._instance = instance
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            if cls._instance is not None:
                cls._executor.submit(mt5.shutdown).result()
                cls._instance = None

    def _connect(self, login: int | None, password: str | None, server: str | None) -> None:
        if self._connected:
            return
        if login and password and server:
            initialized = mt5.initialize(login=login, password=password, server=server)
        else:
            initialized = mt5.initialize()
        if not initialized:
            raise RuntimeError(f"MT5 connect failed: {mt5.last_error()}")
        self._connected = True
        logger.info("MT5 [Mem: %d trades]", self._memory.total_trades)

    def shutdown(self) -> None:
        self._connected = False
        self._memory.save()
        mt5.shutdown()
        logger.info("MT5 ")

    # ----------------------------------------------------------------
    # ?
    # ----------------------------------------------------------------

    async def buy_market(
        self, symbol: str, volume: float = 0.01, sl: float = 0.0, tp: float = 0.0,
    ) -> dict:
        """

        Returns
        -------
        dict
            {"success": bool, "order": int, "price": float, "comment": str, ...}
        """
        return await self._run_in_executor(self._buy_market, symbol, volume, sl, tp)

    def _check_rate_limit(self) -> str | None:
        """Check order rate limit. Returns error string if limited, None if OK."""
        now = time.time()
        # Exponential backoff: 5s, 10s, 20s, 40s... up to 300s (5min)
        interval = min(self._min_order_interval * (2 ** self._consecutive_failures), 300.0)
        if now - self._last_order_time < interval:
            wait = interval - (now - self._last_order_time)
            return f"Rate limited, retry after {wait:.0f}s"
        self._last_order_time = now
        return None

    def _buy_market(self, symbol: str, volume: float, sl: float = 0.0, tp: float = 0.0) -> dict:
        # Rate limit with exponential backoff
        limit_err = self._check_rate_limit()
        if limit_err:
            return {"success": False, "error": limit_err}
        self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"success": False, "error": f"Symbol {symbol} not available"}

        # ?
        before = self._get_balance()

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 0,
            "comment": "python buy",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling(symbol),
        }
        if sl > 0:
            request["sl"] = sl
        if tp > 0:
            request["tp"] = tp
        result = mt5.order_send(request)
        formatted = self._format_result(result, request)

        # ?
        after = self._get_balance()
        if formatted["success"]:
            self._memory.record_trade(TradeRecord(
                time=datetime.now(timezone.utc).isoformat(),
                action="buy",
                symbol=symbol,
                volume=volume,
                price=formatted.get("price", tick.ask),
                order=formatted.get("order", 0),
                deal=formatted.get("deal", 0),
                balance_before=before,
                balance_after=after,
                profit=round(after - before, 2),
            ))
            self._memory.save()
            self._notify_order("BUY", symbol, volume, formatted.get("price", tick.ask))
            self._consecutive_failures = 0  # reset backoff on success
        else:
            self._consecutive_failures += 1

        return formatted

    def _notify_order(self, direction: str, symbol: str, volume: float, price: float) -> None:
        if not self._emailer:
            return
        try:
            now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
            self._emailer.send_order(
                time_str=now_str,
                direction=direction,
                strategy=self._strategy_name,
                volume=volume,
                price=price,
            )
        except Exception as e:
            logger.warning("Email notify failed: %s", e)

    def _notify_close(self, direction: str, pnl: float, hold_seconds: float) -> None:
        if not self._emailer:
            return
        try:
            now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
            hold_str = ""
            if hold_seconds >= 0:
                m = int(hold_seconds // 60)
                if m >= 60:
                    hold_str = f"{m // 60}h {m % 60}m"
                else:
                    hold_str = f"{m}m"
            self._emailer.send_close(
                time_str=now_str,
                direction=direction,
                reason="Strategy Close",
                pnl=round(pnl, 2),
                hold=hold_str,
            )
        except Exception as e:
            logger.warning("Email close notify failed: %s", e)

    # ----------------------------------------------------------------
    # ?
    # ----------------------------------------------------------------

    async def place_limit_order(
        self, symbol: str, price: float, volume: float = 0.01,
    ) -> dict:
        """??

        ?
          - price < ?? ask  ? ORDER_TYPE_BUY_LIMIT  (??)
          - price > ?? bid  ? ORDER_TYPE_SELL_LIMIT (??)

        Returns
        -------
        dict
        """
        return await self._run_in_executor(self._place_limit_order, symbol, price, volume)

    def _sell_market(self, symbol: str, volume: float, sl: float = 0.0, tp: float = 0.0) -> dict:
        # Rate limit with exponential backoff
        limit_err = self._check_rate_limit()
        if limit_err:
            return {"success": False, "error": limit_err}
        self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"success": False, "error": f"Symbol {symbol} not available"}
        before = self._get_balance()
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_SELL,
            "price": tick.bid,
            "deviation": 20,
            "magic": 0,
            "comment": "python sell",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling(symbol),
        }
        if sl > 0:
            request["sl"] = sl
        if tp > 0:
            request["tp"] = tp
        result = mt5.order_send(request)
        formatted = self._format_result(result, request)
        after = self._get_balance()
        if formatted["success"]:
            self._memory.record_trade(TradeRecord(
                time=datetime.now(timezone.utc).isoformat(),
                action="sell",
                symbol=symbol,
                volume=volume,
                price=formatted.get("price", tick.bid),
                order=formatted.get("order", 0),
                deal=formatted.get("deal", 0),
                balance_before=before,
                balance_after=after,
                profit=round(after - before, 2),
            ))
            self._memory.save()
            self._notify_order("SELL", symbol, volume, formatted.get("price", tick.bid))
            self._consecutive_failures = 0  # reset backoff on success
        else:
            self._consecutive_failures += 1
        return formatted

    async def sell_market(self, symbol: str, volume: float = 0.01, sl: float = 0.0, tp: float = 0.0) -> dict:
        return await self._run_in_executor(self._sell_market, symbol, volume, sl, tp)

    def _place_limit_order(self, symbol: str, price: float, volume: float) -> dict:
        self._ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"success": False, "error": f"?? {symbol} ?"}

        # ?
        if price < tick.ask and price < tick.bid:
            order_type = mt5.ORDER_TYPE_BUY_LIMIT   # ??
            label = "buy_limit"
        elif price > tick.bid and price > tick.ask:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT  # ??
            label = "sell_limit"
        else:
            return {
                "success": False,
                "error": f"?? {price} ? bid={tick.bid}~ask={tick.ask} "
                         f"?",
            }

        before = self._get_balance()

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "deviation": 10,
            "magic": 0,
            "comment": f"python {label}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        result = mt5.order_send(request)
        formatted = self._format_result(result, request)

        if formatted["success"]:
            self._memory.record_trade(TradeRecord(
                time=datetime.now(timezone.utc).isoformat(),
                action=label,
                symbol=symbol,
                volume=volume,
                price=price,
                order=formatted.get("order", 0),
                deal=formatted.get("deal", 0),
                balance_before=before,
                balance_after=before,
            ))
            self._memory.save()

        return formatted

    # ----------------------------------------------------------------
    #  ? ? Tick ?
    # ----------------------------------------------------------------

    async def start_quote_stream(
        self,
        symbols: list[str],
        callback: Callable[[Tick], Awaitable[None]],
        *,
        interval: float = 1.0,
    ) -> None:
        """ Tick

        Parameters
        ----------
        symbols : list[str]
            ?? ["XAUUSD", "EURUSD"]
        callback : Callable[[Tick], Awaitable[None]]
            ?? Tick  Tick ??
        interval : float
            ? 1 ?
        """
        if self._stream_task is not None:
            logger.warning("")
            await self.stop_quote_stream()

        async def _stream_loop():
            logger.info("Stream: %s interval=%.1fs", symbols, interval)
            while self._connected:
                try:
                    for sym in symbols:
                        quote = await self.get_quote(sym)
                        if quote:
                            tick = Tick(
                                symbol=quote["symbol"],
                                bid=quote["bid"],
                                ask=quote["ask"],
                                timestamp=datetime.fromtimestamp(
                                    quote["time"], tz=timezone.utc
                                ) if quote.get("time") else None,
                            )
                            await callback(tick)
                except Exception as e:
                    logger.debug("Error: %s", e)
                await asyncio.sleep(interval)

        self._stream_task = asyncio.create_task(_stream_loop())

    async def stop_quote_stream(self) -> None:
        """"""
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
            logger.info("")

    # ----------------------------------------------------------------
    # ?
    # ----------------------------------------------------------------

    def get_memory(self) -> Memory:
        """??"""
        return self._memory

    # ----------------------------------------------------------------
    # ?
    # ----------------------------------------------------------------

    async def get_quote(self, symbol: str) -> dict | None:
        """"""
        return await self._run_in_executor(self._get_quote, symbol)

    async def get_account_info(self) -> dict | None:
        """?"""
        return await self._run_in_executor(self._get_account_info)

    async def get_positions(self, symbol: str = "") -> list[dict]:
        """"""
        return await self._run_in_executor(self._get_positions, symbol)

    async def close_all_positions(self, symbol: str = "") -> list[dict]:
        """"""
        return await self._run_in_executor(self._close_all, symbol)

    async def cancel_all_pending(self, symbol: str = "") -> list[dict]:
        """"""
        return await self._run_in_executor(self._cancel_pending, symbol)

    # ----------------------------------------------------------------
    # ----------------------------------------------------------------


    async def modify_sl(self, symbol: str, position_ticket: int, sl: float) -> dict:
        def _modify():
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": position_ticket,
                "sl": sl,
                "tp": 0,
            }
            result = mt5.order_send(request)
            return {"success": result.retcode == mt5.TRADE_RETCODE_DONE,
                    "comment": result.comment, "retcode": result.retcode}
        return await self._run_in_executor(_modify)

    @classmethod
    async def _run_in_executor(cls, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(cls._executor, func, *args)

    @staticmethod
    def _ensure_symbol(symbol: str) -> None:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f" {symbol}: {mt5.last_error()}")

    @staticmethod
    def _get_balance() -> float:
        acc = mt5.account_info()
        return acc.balance if acc else 0.0

    @staticmethod
    def _get_quote(symbol: str) -> dict | None:
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            return None
        return {
            "symbol": symbol,
            "bid": tick.bid,
            "ask": tick.ask,
            "spread": info.spread,
            "digits": info.digits,
            "time": tick.time,
        }

    @staticmethod
    def _get_account_info() -> dict | None:
        acc = mt5.account_info()
        if acc is None:
            return None
        return {
            "login": acc.login,
            "name": acc.name,
            "balance": acc.balance,
            "equity": acc.equity,
            "margin": acc.margin,
            "free_margin": acc.margin_free,
            "profit": acc.profit,
            "leverage": acc.leverage,
            "server": acc.server,
            "currency": acc.currency,
        }

    @staticmethod
    def _get_positions(symbol: str) -> list[dict]:
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if not positions:
            return []
        return [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "buy" if p.type == mt5.ORDER_TYPE_BUY else "sell",
                "volume": p.volume,
                "open_price": p.price_open,
                "current_price": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": round(p.profit, 2),
                "swap": round(p.swap, 2),
                "magic": p.magic,
                "comment": p.comment,
                "open_time": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            }
            for p in positions
        ]

    def _close_all(self, symbol: str) -> list[dict]:
        positions = mt5.positions_get()
        if not positions:
            return []
        results = []
        for p in positions:
            if symbol and p.symbol != symbol:
                continue
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None:
                continue
            price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
            close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": close_type,
                "position": p.ticket,
                "price": price,
                "deviation": 20,
                "magic": 0,
                "comment": "python close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._get_filling(p.symbol),
            }
            before = self._get_balance()
            result = mt5.order_send(request)
            r = self._format_result(result)
            if r["success"]:
                after = self._get_balance()
                profit = round(after - before, 2)
                r["profit"] = profit
                self._memory.record_trade(TradeRecord(
                    time=datetime.now(timezone.utc).isoformat(),
                    action="close",
                    symbol=p.symbol,
                    volume=p.volume,
                    price=price,
                    order=r.get("order", 0),
                    deal=r.get("deal", 0),
                    balance_before=before,
                    balance_after=after,
                    profit=profit,
                ))
                direction = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
                self._notify_close(direction, profit, tick.time - p.time)
            results.append(r)
        self._memory.save()
        return results

    @staticmethod
    def _cancel_pending(symbol: str) -> list[dict]:
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if not orders:
            return []
        results = []
        for o in orders:
            if symbol and o.symbol != symbol:
                continue
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            results.append({
                "success": result is not None and result.retcode == mt5.TRADE_RETCODE_DONE,
                "order": o.ticket,
                "symbol": o.symbol,
            })
        return results

    @staticmethod
    def _format_result(result, request: dict | None = None) -> dict:
        if result is None:
            return {"success": False, "error": str(mt5.last_error())}
        success = result.retcode == mt5.TRADE_RETCODE_DONE
        d = {
            "success": success,
            "retcode": result.retcode,
            "order": result.order,
            "deal": result.deal,
            "volume": result.volume,
            "price": request.get("price", result.price) if request else result.price,
            "comment": result.comment,
        }
        if not success:
            d["error"] = f"retcode={result.retcode} comment={result.comment}"
        return d
