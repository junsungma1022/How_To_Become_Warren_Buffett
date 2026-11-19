"""Discord 웹훅 알림 — 채널 다섯 (#entries #exits #warnings #critical #daily_summary).

- 웹훅 주소는 비밀값이다 (.env). 없는 채널은 보내지 않고 기록에만 남긴다.
- 보내기 실패는 이벤트 기록에 남긴다 (알림 실패를 알림으로 알릴 수는 없다 · 침묵 실패 금지).
- 같은 `key` 는 `min_interval_s` 안에 다시 보내지 않는다 (경고 폭주 방지).
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import httpx

from ..config import Secrets, scrub

CHANNELS = ("entries", "exits", "warnings", "critical", "daily_summary")
LIMIT = 1900          # Discord content 2000자 한도


class Discord:
    def __init__(self, secrets: Secrets, log: Callable[..., dict], http: httpx.AsyncClient | None = None,
                 prefix: str = "HBWB", clock=time.time, sleep=asyncio.sleep):
        self.urls = {ch: secrets.get("discord_" + ch, required=False) for ch in CHANNELS}
        self._secret_vals = [u for u in self.urls.values() if u]
        self.log = log
        self.http = http or httpx.AsyncClient(timeout=10.0)
        self.prefix, self.clock, self._sleep = prefix, clock, sleep
        self._last: dict[str, float] = {}

    async def send(self, channel: str, text: str, key: str | None = None, min_interval_s: float = 0.0) -> bool:
        if channel not in CHANNELS:
            raise ValueError(channel)
        now = self.clock()
        if key and min_interval_s and now - self._last.get(key, -1e18) < min_interval_s:
            return False
        if key:
            self._last[key] = now
        msg = f"[{self.prefix}] {text}"
        chunks = [msg[i:i + LIMIT] for i in range(0, len(msg), LIMIT)] or [msg]
        url = self.urls.get(channel)
        if not url:
            self.log("notify_skip", channel=channel, why="웹훅 없음", text=msg[:300])
            return False
        ok = True
        for ch in chunks:
            try:
                body = {"content": ch, "allowed_mentions": {"parse": []}}
                r = await self.http.post(url, json=body)
                for _ in range(3):                      # 429 (너무 자주) 면 Discord 가 알려 준 시간만큼 쉬고 다시
                    if r.status_code != 429:
                        break
                    try:
                        wait = float((r.json() or {}).get("retry_after") or 1.0)
                    except ValueError:
                        wait = float(r.headers.get("Retry-After") or 1.0)
                    await self._sleep(min(wait, 10.0))
                    r = await self.http.post(url, json=body)
                if r.status_code >= 300:
                    ok = False
                    self.log("notify_fail", channel=channel, status=r.status_code, body=scrub(r.text[:200], self._secret_vals))
            except httpx.HTTPError as e:
                ok = False
                self.log("notify_fail", channel=channel, err=scrub(f"{type(e).__name__}: {e}", self._secret_vals)[:200])
        return ok

    async def aclose(self) -> None:
        await self.http.aclose()
