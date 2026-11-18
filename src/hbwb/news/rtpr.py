"""RTPR 실시간 보도자료 (https://rtpr.io) — WebSocket 알림 + 기사 본문.

이전 프로젝트(Auto_Trader_Futures · wire_probe.py, 2026-10 실측)에서 확인한 규칙:
- WebSocket `wss://ws.rtpr.io/ws-alerts?apiKey=KEY` — 알림 `{"type":"alert", ticker, article_published_at, article_url, ...}`
  · 서버 `{"type":"ping"}` 에 `{"type":"pong"}` · 닫힘 코드 4001 = 키 오류, 4004 = 구독 끝 (다시 붙지 않는다)
  · 키당 연결 하나 (다른 곳에서 같은 키로 붙으면 앞 연결이 끊긴다)
  · 대시보드에 'All articles' 규칙이 있어야 알림이 온다
- 본문: `article_url + ?format=text` 에 머리 `X-API-Key` → 첫 줄 제목, 둘째 줄 `TICKER (EXCHANGE) · WIRE · 날짜`
- 알림의 article_published_at 과 실시간 수신 시각 차이는 중앙 0.1초 (531건 대조)

키는 URL 쿼리와 헤더로만 나가고, 로그 · 예외 문구에는 `scrub` 를 거쳐 남지 않는다.
사용자는 RTPR 측에서 이 개발 · 사용을 허락받았다고 밝혔다 (2026-11-18 · docs/DECISIONS.md). 원문 기사는 저장소에 올리지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

import httpx

from ..clock import now_ms, parse_iso_ms
from ..config import scrub

WS_URL = "wss://ws.rtpr.io/ws-alerts"
FATAL_CLOSE = {4001: "RTPR 키가 틀렸다", 4004: "RTPR 구독이 끝났다"}
IDLE_S = 120          # 서버 ping 은 30초마다 — 이만큼 아무것도 안 오면 다시 붙는다
UA = "How_To_Become_Warren_Buffett/0.1 (student project)"

_EXCH = re.compile(r"^\s*([A-Z0-9.\-]+)\s*\(([^)]+)\)")


class RtprFatal(RuntimeError):
    pass


@dataclass
class Alert:
    ticker: str
    url: str                # 서명 쿼리를 뗀 기사 URL (기사 식별자)
    raw_url: str            # 서명 쿼리가 붙은 원래 URL (본문 받기에 쓴다 · 기록에는 남기지 않는다)
    published_ms: int | None
    received_ms: int
    extra: dict = field(default_factory=dict)

    @property
    def article_id(self) -> str:
        return self.url.rstrip("/").rsplit("/", 1)[-1]

    @property
    def age_s(self) -> float | None:
        return None if self.published_ms is None else (self.received_ms - self.published_ms) / 1000


def strip_query(url: str | None) -> str:
    return str(url or "").split("?", 1)[0]


def parse_ws_text(text: str, received_ms: int) -> tuple[list[Alert], bool, list[dict]]:
    """WebSocket 글 → (알림들, ping 이었나, 그 밖)."""
    try:
        m = json.loads(text)
    except ValueError:
        return [], False, [{"type": "parse_error", "text": text[:200]}]
    alerts, ping, other = [], False, []
    for x in (m if isinstance(m, list) else [m]):
        if not isinstance(x, dict):
            other.append({"type": "?", "v": str(x)[:200]})
        elif x.get("type") == "ping":
            ping = True
        elif x.get("type") == "alert":
            t = str(x.get("ticker") or "").upper().strip()
            raw = str(x.get("article_url") or "")
            if not t or not raw:
                other.append({"type": "alert_missing_fields", "keys": sorted(x.keys())})
                continue
            extra = {k: x[k] for k in ("impact_score", "impact_tier", "event_type", "impact_direction") if k in x}
            alerts.append(Alert(t, strip_query(raw), raw, parse_iso_ms(x.get("article_published_at")), received_ms, extra))
        else:
            other.append({k: (v[:200] if isinstance(v, str) else v) for k, v in x.items()})
    return alerts, ping, other


@dataclass
class Article:
    title: str
    exchange: str | None
    body: str
    status: int


def parse_article_text(text: str) -> tuple[str, str | None]:
    """본문 글의 앞줄들 → (제목, 거래소)."""
    lines = [ln.strip().lstrip("#*>").strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    title = lines[0][:300] if lines else ""
    exch = None
    if len(lines) > 1:
        m = _EXCH.match(lines[1])
        if m:
            exch = m.group(2).strip().upper()
    return title, exch


class RtprClient:
    def __init__(self, key: str, http: httpx.AsyncClient | None = None):
        self._key = key
        self.http = http or httpx.AsyncClient(timeout=15.0, headers={"User-Agent": UA})

    def _scrub(self, s) -> str:
        return scrub(s, [self._key])

    async def article(self, raw_url: str) -> Article:
        url = raw_url + ("&" if "?" in raw_url else "?") + "format=text"
        r = await self.http.get(url, headers={"X-API-Key": self._key, "Accept": "text/plain, */*"})
        text = r.text if r.status_code == 200 else ""
        title, exch = parse_article_text(text)
        return Article(title, exch, text, r.status_code)

    async def alerts(self, on_event: Callable[[str, dict], None], connect=None) -> AsyncIterator[Alert]:
        """끊기면 2·4·…·60초 쉬고 다시 붙는다. 4001/4004 면 RtprFatal."""
        import websockets  # 함수 안에서 — 시험에서 없어도 모듈은 불러진다

        connect = connect or (lambda: websockets.connect(
            WS_URL + "?apiKey=" + urllib.parse.quote(self._key, safe=""), open_timeout=15,
            ping_interval=None, user_agent_header=UA))
        fails = 0
        while True:
            try:
                async with connect() as ws:
                    on_event("rtpr_connect", {})
                    fails = 0
                    while True:
                        try:
                            text = await asyncio.wait_for(ws.recv(), timeout=IDLE_S)
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"{IDLE_S}초 동안 받은 것이 없다")
                        r_ms = now_ms()
                        alerts, ping, other = parse_ws_text(text if isinstance(text, str) else text.decode(), r_ms)
                        if ping:
                            await ws.send('{"type":"pong"}')
                        for o in other:
                            on_event("rtpr_other", {"msg": o})
                        for a in alerts:
                            yield a
            except RtprFatal:
                raise
            except Exception as e:  # noqa: BLE001 - 연결 오류는 모두 다시 붙기
                code = getattr(getattr(e, "rcvd", None), "code", None)
                if code in FATAL_CLOSE:
                    on_event("rtpr_fatal", {"code": code, "why": FATAL_CLOSE[code]})
                    raise RtprFatal(FATAL_CLOSE[code]) from None
                fails += 1
                wait = min(60, 2 ** min(fails, 6))
                on_event("rtpr_disconnect", {"err": self._scrub(f"{type(e).__name__}: {e}")[:300], "retry_in_s": wait})
                await asyncio.sleep(wait)

    async def aclose(self) -> None:
        await self.http.aclose()
