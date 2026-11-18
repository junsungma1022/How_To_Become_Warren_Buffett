"""그룹별 초당 호출 한도 (토스 문서의 그룹 TPS 를 넘지 않게)."""

from __future__ import annotations

import asyncio
import time


class Limiter:
    def __init__(self, per_second: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.gap = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._clock, self._sleep = clock, sleep

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if now < self._next:
                await self._sleep(self._next - now)
                now = self._clock()
            self._next = now + self.gap
