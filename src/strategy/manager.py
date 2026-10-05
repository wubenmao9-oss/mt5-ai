"""Strategy?? ? StrategyStrategyStrategy?"""

import logging
from typing import Type

from .base import BaseStrategy

logger = logging.getLogger(__name__)


class StrategyManager:
    """StrategyStrategyStrategyStrategyStrategyStrategyStrategyStrategyStrategy?"""

    def __init__(self, executor, config=None, recorder=None, emailer=None):
        self._executor = executor
        self._config = config
        self._recorder = recorder
        self._emailer = emailer
        self._registry: dict[str, Type[BaseStrategy]] = {}
        self._active: BaseStrategy | None = None
        self._active_name: str | None = None

    # ------------------------------------------------------------------
    # ??
    # ------------------------------------------------------------------

    def register(self, name: str, strategy_cls: Type[BaseStrategy]) -> None:
        """StrategyStrategy??name ? .env ? ACTIVE_STRATEGY StrategyStrategy"""
        self._registry[name] = strategy_cls
        logger.info("Registered: %s (%s)", name, strategy_cls.__name__)

    def list_strategies(self) -> list[str]:
        return list(self._registry)

    # ------------------------------------------------------------------
    # ??
    # ------------------------------------------------------------------

    async def activate(self, name: str) -> None:
        """StrategyStrategyStrategyStrategy??"""
        if name not in self._registry:
            raise ValueError(f"Strategy?: {name}, ??: {self.list_strategies()}")

        if self._active:
            logger.info("?Activated: %s", self._active_name)
            await self._active.stop()

        cls = self._registry[name]
        try:
            self._active = cls(
                executor=self._executor,
                config=self._config,
                recorder=self._recorder,
                emailer=self._emailer,
            )
            await self._active.start()
            self._active_name = name
            # Sync strategy name to executor for email notifications
            if hasattr(self._executor, '_strategy_name'):
                self._executor._strategy_name = name
            logger.info("Activated: %s", name)
        except Exception as e:
            logger.exception("?? %s Strategy?: %s", name, e)
            self._active = None
            self._active_name = None
            raise

    # ------------------------------------------------------------------
    # StrategyStrategyStrategyStrategy?
    # ------------------------------------------------------------------

    async def on_tick(self, tick) -> None:
        if self._active:
            await self._active.on_tick(tick)

    async def stop(self) -> None:
        if self._active:
            await self._active.stop()
            self._active_name = None

    def get_status(self) -> dict:
        if self._active:
            return self._active.get_status()
        return {"active": False, "active_strategy": None}
