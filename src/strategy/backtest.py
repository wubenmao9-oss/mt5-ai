"""Strategy backtest engine — runs strategy against historical K-line data."""

import logging
from dataclasses import dataclass, field
from typing import Type
from datetime import datetime, timezone

from .base import BaseStrategy

logger = logging.getLogger(__name__)

@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    equity_curve: list = field(default_factory=list)
    passed: bool = False
    reason: str = ""

    # Thresholds
    MIN_WIN_RATE = 45.0
    MIN_PROFIT_FACTOR = 1.0

    def evaluate(self):
        if self.total_trades == 0:
            self.passed = False
            self.reason = "无交易记录"
            return
        self.win_rate = (self.wins / self.total_trades) * 100 if self.total_trades else 0
        self.avg_win = (self.total_pnl_of_wins / self.wins) if self.wins else 0
        self.avg_loss = (self.total_pnl_of_losses / self.losses) if self.losses else 0
        self.profit_factor = abs(self.total_pnl_of_wins / self.total_pnl_of_losses) if self.total_pnl_of_losses != 0 else 999

        checks = []
        if self.win_rate < self.MIN_WIN_RATE:
            checks.append(f"胜率{self.win_rate:.1f}%<{self.MIN_WIN_RATE}%")
        if self.profit_factor < self.MIN_PROFIT_FACTOR:
            checks.append(f"盈亏比{self.profit_factor:.2f}<{self.MIN_PROFIT_FACTOR}")
        if self.total_pnl <= 0:
            checks.append(f"总盈亏{self.total_pnl:.2f}<=0")

        if checks:
            self.passed = False
            self.reason = "; ".join(checks)
        else:
            self.passed = True
            self.reason = "通过"

class BacktestRunner:
    """Run a strategy against historical data for soft testing."""

    def __init__(self, symbol: str = "XAUUSD", initial_balance: float = 10000.0):
        self.symbol = symbol
        self.initial_balance = initial_balance

    def _get_historical_bars(self, days: int = 30):
        """Fetch historical H1 bars from MT5."""
        import MetaTrader5 as mt5
        from datetime import timedelta
        to = datetime.now(timezone.utc)
        fr = to - timedelta(days=days)
        rates = mt5.copy_rates_from(self.symbol, mt5.TIMEFRAME_H1, fr, days * 24)
        return rates

    def run(self, strategy_cls: Type[BaseStrategy], days: int = 30) -> BacktestResult:
        """Run backtest of strategy_cls over `days` of historical data.

        Simulates a minimal trading environment: feeds historical bars
        as ticks to the strategy, tracks virtual trades.
        """
        result = BacktestResult()
        result.total_pnl_of_wins = 0.0
        result.total_pnl_of_losses = 0.0

        try:
            bars = self._get_historical_bars(days)
        except Exception as e:
            logger.error("Backtest: failed to get historical data: %s", e)
            result.passed = False
            result.reason = f"获取历史数据失败: {e}"
            return result

        if bars is None or len(bars) == 0:
            result.passed = False
            result.reason = "无历史数据"
            return result

        # Create a minimal mock environment for the strategy
        balance = self.initial_balance
        equity = balance
        position = None  # {side, open_price, open_time, volume}
        max_equity = balance

        class MockExecutor:
            async def get_account_info(self):
                return {"balance": balance, "equity": equity, "margin_free": equity}

            async def place_order(self, side, price, volume, sl, tp, comment=""):
                nonlocal position
                if position is not None:
                    return None
                position = {"side": side, "open_price": price, "volume": volume, "sl": sl, "tp": tp}
                return {"ticket": 1}

            async def close_position(self, ticket, price, comment=""):
                nonlocal position, balance, equity
                if position is None:
                    return None
                pnl = (price - position["open_price"]) * position["volume"]
                if position["side"] == "sell":
                    pnl = -pnl
                balance += pnl
                equity = balance
                closed = position.copy()
                closed["close_price"] = price
                closed["pnl"] = pnl
                position = None
                return closed

            def get_positions(self):
                if position:
                    return [type("Pos", (), {
                        "ticket": 1, "type": 0 if position["side"] == "buy" else 1,
                        "price_open": position["open_price"], "volume_current": position["volume"],
                    })()]
                return []

        class MockRecorder:
            def record_trade(self, **kw): pass
            def record_decision(self, **kw): pass

        class MockConfig:
            symbol = self.symbol
            volume = 0.01

        # Instantiate the strategy with mocks
        try:
            strategy = strategy_cls(
                executor=MockExecutor(),
                config=MockConfig(),
                recorder=MockRecorder(),
                emailer=None,
            )
        except Exception as e:
            result.passed = False
            result.reason = f"策略实例化失败: {e}"
            return result

        # Feed bars as ticks
        import asyncio
        for bar in bars:
            tick = type("Tick", (), {
                "symbol": self.symbol,
                "bid": bar["close"],
                "ask": bar["close"] + 0.3,
                "time": datetime.fromtimestamp(bar["time"], tz=timezone.utc),
            })()
            try:
                asyncio.get_event_loop().run_until_complete(strategy.on_tick(tick))
            except Exception:
                pass

            # Check if position was closed by TP/SL
            if position is not None:
                close_price = None
                if position["side"] == "buy":
                    if position["sl"] and bar["low"] <= position["sl"]:
                        close_price = position["sl"]
                    elif position["tp"] and bar["high"] >= position["tp"]:
                        close_price = position["tp"]
                else:
                    if position["sl"] and bar["high"] >= position["sl"]:
                        close_price = position["sl"]
                    elif position["tp"] and bar["low"] <= position["tp"]:
                        close_price = position["tp"]

                if close_price:
                    try:
                        closed = asyncio.get_event_loop().run_until_complete(
                            MockExecutor().close_position(1, close_price)
                        )
                    except Exception:
                        closed = None
                    if closed:
                        pnl = closed["pnl"]
                        result.total_trades += 1
                        result.total_pnl += pnl
                        if pnl > 0:
                            result.wins += 1
                            result.total_pnl_of_wins += pnl
                        else:
                            result.losses += 1
                            result.total_pnl_of_losses += abs(pnl)
                        equity = balance
                        max_equity = max(max_equity, equity)
                        dd = (max_equity - equity) / max_equity * 100 if max_equity else 0
                        result.max_drawdown = max(result.max_drawdown, dd)
                        result.equity_curve.append(equity)

        result.evaluate()
        try:
            asyncio.get_event_loop().run_until_complete(strategy.stop())
        except Exception:
            pass
        return result

    def run_from_source(self, source_code: str, module_name: str, days: int = 30) -> BacktestResult:
        """Compile source code, extract strategy class, and run backtest."""
        from .crypto import compile_to_pyc, decrypt_to_module, extract_strategy_class
        from .crypto import encrypt_pyc, decrypt_pyc

        # Compile source → pyc → enc → dec → module (tests the full pipeline)
        pyc = compile_to_pyc(source_code, module_name)
        enc = encrypt_pyc(pyc)
        # Verify round-trip
        dec = decrypt_pyc(enc)
        assert dec == pyc, "Encryption round-trip failed"

        mod = decrypt_to_module(enc, module_name)
        _, cls = extract_strategy_class(mod)
        return self.run(cls, days=days)
