"""OpenRouter 하나로 두 모델을 부른다.

- Jev (TypeSafe 결정 모델): `POST /api/alpha/decisions` — 글을 생성하지 않고 선택지별 확률을 돌려준다
- Claude Opus 5.5: `POST /api/v1/chat/completions` (`anthropic/claude-opus-5.5`)

호출마다 비용(`usage.cost`, OpenRouter 크레딧 = 달러)을 돌려줘서 일일 보고서에 AI 비용을 적는다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import scrub

BASE = "https://openrouter.ai"
APP_HEADERS = {"X-Title": "How_To_Become_Warren_Buffett", "HTTP-Referer": "https://github.com/junsungma1022/How_To_Become_Warren_Buffett"}


class OpenRouterError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"openrouter {status}: {message}")
        self.status = status


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def _usage(doc: dict) -> Usage:
    u = doc.get("usage") or {}
    return Usage(int(u.get("input_tokens") or u.get("prompt_tokens") or 0),
                 int(u.get("output_tokens") or u.get("completion_tokens") or 0),
                 float(u.get("cost") or 0.0))


class OpenRouter:
    def __init__(self, key: str, http: httpx.AsyncClient | None = None, sleep=asyncio.sleep):
        self._key = key
        self.http = http or httpx.AsyncClient(base_url=BASE, timeout=90.0)
        self._sleep = sleep

    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self._key, "Content-Type": "application/json", **APP_HEADERS}

    async def _post(self, path: str, body: dict, timeout: float, retry: int = 2) -> dict:
        last = None
        for attempt in range(retry + 1):
            try:
                r = await self.http.post(path, json=body, headers=self._headers(), timeout=timeout)
            except httpx.TransportError as e:
                last = OpenRouterError(0, scrub(f"{type(e).__name__}: {e}", [self._key]))
                await self._sleep(1.0 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r.json()
            msg = scrub(r.text[:300], [self._key])
            last = OpenRouterError(r.status_code, msg)
            if r.status_code in (429, 500, 502, 503, 524, 529) and attempt < retry:
                await self._sleep(1.5 * (attempt + 1))
                continue
            raise last
        raise last  # type: ignore[misc]

    async def decide(self, model: str, state: Any, questions: dict, timeout: float = 20.0) -> tuple[dict, Usage]:
        doc = await self._post("/api/alpha/decisions", {"model": model, "state": state, "questions": questions}, timeout)
        return doc.get("answers") or {}, _usage(doc)

    async def chat(self, model: str, messages: list[dict], *, max_tokens: int, reasoning_effort: str | None = None,
                   timeout: float = 60.0) -> tuple[str, Usage]:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True}}
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        doc = await self._post("/api/v1/chat/completions", body, timeout)
        try:
            text = doc["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise OpenRouterError(200, "응답에 choices 가 없다")
        if isinstance(text, list):  # 일부 제공자는 content 를 조각 목록으로 준다
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return str(text), _usage(doc)

    async def aclose(self) -> None:
        await self.http.aclose()
