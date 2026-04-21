from abc import ABC, abstractmethod
from typing import Callable, Awaitable


class IMessageBus(ABC):
    """
    메시지 버스 추상 인터페이스.
    AGV 인스턴스는 이 인터페이스만 바라봄 — MQTT인지 메모리인지 모름.
    """

    @abstractmethod
    async def connect(self) -> None:
        """버스 연결. memory 버스는 no-op."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """버스 연결 해제."""
        ...

    @abstractmethod
    async def publish(self, topic: str, payload: dict) -> None:
        """토픽에 메시지 발행."""
        ...

    @abstractmethod
    async def subscribe(
        self, topic: str, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        """
        토픽 구독. 와일드카드 지원 (예: uagv/v2/NEXT/#).
        메시지 수신 시 callback(payload) 호출.
        """
        ...
