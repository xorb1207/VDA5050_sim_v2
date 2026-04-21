from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Callable, Awaitable

from src.interfaces.bus import IMessageBus


class LocalMemoryBus(IMessageBus):
    """
    테스트 / CI용 인메모리 버스.
    MQTT 브로커 없이 동작. asyncio.Queue 기반.
    """

    def __init__(self) -> None:
        # topic → callback 리스트 (와일드카드 미지원, 완전 일치)
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)

    async def connect(self) -> None:
        pass  # no-op

    async def disconnect(self) -> None:
        pass  # no-op

    async def publish(self, topic: str, payload: dict) -> None:
        """구독자 전체에 즉시 전달. '#' 와일드카드 지원."""
        callbacks = [
            cb
            for pattern, cbs in self._subscribers.items()
            for cb in cbs
            if self._matches(pattern, topic)
        ]
        if callbacks:
            await asyncio.gather(*(cb(payload) for cb in callbacks))

    async def subscribe(
        self, topic: str, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        self._subscribers[topic].append(callback)

    @staticmethod
    def _matches(pattern: str, topic: str) -> bool:
        """MQTT '#' 와일드카드 단순 구현."""
        if pattern == topic:
            return True
        if pattern.endswith("/#"):
            return topic.startswith(pattern[:-2] + "/")
        if pattern.endswith("#"):
            return topic.startswith(pattern[:-1])
        return False


# ---------------------------------------------------------------------------


class MQTTAdapter(IMessageBus):
    """
    단일 MQTT 클라이언트로 전체 AGV 토픽 처리.
    Wildcard 구독: uagv/v2/NEXT/#
    내부 Dispatcher가 topic에서 agv_id 추출 후 콜백 라우팅.

    의존성: aiomqtt (pip install aiomqtt)
    """

    TOPIC_PREFIX = "uagv/v2/NEXT"

    def __init__(self, host: str, port: int = 1883) -> None:
        self._host = host
        self._port = port
        self._client = None              # aiomqtt.Client (연결 후 설정)
        self._dispatch_table: dict[str, list[Callable]] = defaultdict(list)
        self._dispatcher_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """
        aiomqtt 클라이언트 생성 + 와일드카드 구독 시작.
        LWT 설정: topic=uagv/v2/NEXT/master/lwt, payload=CONNECTIONBROKEN
        """
        raise NotImplementedError

    async def disconnect(self) -> None:
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
        raise NotImplementedError

    async def publish(self, topic: str, payload: dict) -> None:
        raise NotImplementedError

    async def subscribe(
        self, topic: str, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        """
        dispatch_table에 등록. 실제 MQTT 구독은 connect() 시 와일드카드로 일괄 처리.
        """
        self._dispatch_table[topic].append(callback)

    async def _dispatch_loop(self) -> None:
        """
        수신 메시지를 topic 기반으로 콜백 라우팅.
        topic 예: uagv/v2/NEXT/AGV_001/order → agv_id = AGV_001
        """
        raise NotImplementedError
