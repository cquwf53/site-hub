from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class EventBroker:
    def __init__(self, queue_size: int = 128) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size
        self._lock = asyncio.Lock()

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        message = {"event": event, "data": data}
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def stream(self) -> AsyncIterator[str]:
        async with self.subscribe() as queue:
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                    payload = json.dumps(message["data"], ensure_ascii=False, default=str)
                    yield f"event: {message['event']}\ndata: {payload}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
