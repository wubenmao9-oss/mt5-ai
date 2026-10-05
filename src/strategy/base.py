"""? ? ??"""
from abc import ABC, abstractmethod

class BaseStrategy(ABC):
    @abstractmethod
    async def start(self):
        ...

    @abstractmethod
    async def stop(self):
        ...

    @abstractmethod
    async def on_tick(self, tick):
        ...

    @abstractmethod
    def get_status(self) -> dict:
        ...
