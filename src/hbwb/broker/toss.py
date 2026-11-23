"""토스증권 Open API 클라이언트 (시세 · 계좌 · 주문).

공식 문서 (2026-11-18 확인 · https://developers.tossinvest.com/llms.txt → openapi.json):
- REST `https://openapi.tossinvest.com` · 토큰 `POST /oauth2/token` (client_credentials, form-encoded)
- 클라이언트마다 유효 토큰 하나 — 새로 받으면 앞 토큰은 `401 token-revoked`
- 계좌 · 주문 API 는 `X-Tossinvest-Account: {accountSeq}` 머리 필요 → `GET /api/v1/accounts` 로 실행 중에 받는다
  (계좌번호 · accountSeq 는 파일 · .env 어디에도 저장하지 않는다)
- 허용 IP 목록 밖에서 부르면 403 `ip-not-allowed`
- 주문: side BUY|SELL · orderType LIMIT|MARKET · timeInForce DAY|CLS|OPG · US 가격은 $1 미만 소수 4자리, 이상 2자리 (절삭)
  · clientOrderId 는 10분 멱등키 · 미국 주식 정정은 가격만 (수량 정정 불가)
- 한도 (그룹 TPS): ORDER 10 · ORDER_INFO 6 (09:00~09:10 KST 3) · ACCOUNT 1 · ASSET 5 · MARKET_DATA 15 ·
  MARKET_DATA_CHART 20 · STOCK 5 · MARKET_INFO 3 · ORDER_HISTORY 5
- 미국 시세는 일부 거래소 기준 (NBBO 아님) · 1분봉 timestamp 는 봉 **끝** 시각

주문 함수는 `allow_orders=True` 로 만든 클라이언트에서만 동작한다 (shadow 모드에서는 물리적으로 못 보낸다).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Iterable

import httpx

from ..clock import ET, ms_to, parse_iso_ms
from ..config import scrub
from ..ratelimit import Limiter

BASE = "https://openapi.tossinvest.com"

GROUP_TPS = {
    "AUTH": 5, "ACCOUNT": 1, "ASSET": 5, "ORDER": 10, "ORDER_INFO": 3, "ORDER_HISTORY": 5,
    "MARKET_DATA": 15, "MARKET_DATA_CHART": 20, "STOCK": 5, "MARKET_INFO": 3,
}
# ORDER_INFO 는 09:00~09:10 KST 에 3 으로 줄어든다 → 늘 3 으로 잡는다 (보수적)


class TossError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, request_id: str | None = None, data: Any = None):
        super().__init__(f"toss {status} {code}: {message} (requestId={request_id})")
        self.status, self.code, self.message, self.request_id, self.data = status, code, message, request_id, data


class OrdersNotAllowed(RuntimeError):
    """shadow 모드 클라이언트에서 주문 함수를 불렀다 — 코드 버그다."""


def fmt_price(p: float | Decimal) -> str:
    """미국 주문 가격: $1 미만 소수 4자리 · 이상 2자리 (문서: 그 아래 자리는 절삭)."""
    d = Decimal(str(p))
    q = Decimal("0.0001") if d < 1 else Decimal("0.01")
    out = d.quantize(q, rounding=ROUND_DOWN)
    if out <= 0:
        raise ValueError(f"가격이 0 이하: {p}")
    return format(out, "f")


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class Quote:
    bid: float | None
    ask: float | None
    t_ms: int | None


@dataclass
class OrderState:
    order_id: str
    symbol: str
    side: str
    status: str
    quantity: float
    filled_qty: float
    avg_price: float | None
    commission: float | None
    price: float | None
    raw: dict

    @property
    def done(self) -> bool:
        return self.status in {"FILLED", "CANCELED", "REJECTED", "REPLACED", "CANCEL_REJECTED", "REPLACE_REJECTED"}


def parse_order(o: dict) -> OrderState:
    ex = o.get("execution") or {}
    return OrderState(
        order_id=str(o.get("orderId")), symbol=str(o.get("symbol")), side=str(o.get("side")),
        status=str(o.get("status")), quantity=_f(o.get("quantity")) or 0.0,
        filled_qty=_f(ex.get("filledQuantity")) or 0.0, avg_price=_f(ex.get("averageFilledPrice")),
        commission=_f(ex.get("commission")), price=_f(o.get("price")), raw=o,
    )


class TossClient:
    def __init__(self, client_id: str, client_secret: str, *, allow_orders: bool = False,
                 http: httpx.AsyncClient | None = None, sleep=asyncio.sleep):
        self._cid, self._sec = client_id, client_secret
        self.allow_orders = allow_orders
        self.http = http or httpx.AsyncClient(base_url=BASE, timeout=10.0)
        self._tok: str | None = None
        self._tok_exp = 0.0
        self._tok_lock = asyncio.Lock()
        self._account_seq: int | None = None
        self._lim = {g: Limiter(tps, sleep=sleep) for g, tps in GROUP_TPS.items()}
        self._sleep = sleep
        self.calls = 0

    # ── 내부 ────────────────────────────────────────────────────────────────
    def _secrets(self) -> list[str]:
        return [x for x in (self._cid, self._sec, self._tok) if x]

    def _err(self, r: httpx.Response) -> TossError:
        try:
            e = (r.json() or {}).get("error") or {}
        except ValueError:
            e = {}
        msg = scrub(e.get("message") or r.text[:200], self._secrets())
        return TossError(r.status_code, str(e.get("code") or "unknown"), msg, e.get("requestId"), e.get("data"))

    async def token(self, force: bool = False) -> str:
        async with self._tok_lock:
            if self._tok and not force and time.time() < self._tok_exp - 300:
                return self._tok
            await self._lim["AUTH"].wait()
            r = await self.http.post("/oauth2/token", data={
                "grant_type": "client_credentials", "client_id": self._cid, "client_secret": self._sec})
            self.calls += 1
            if r.status_code != 200:
                raise TossError(r.status_code, "oauth2", scrub(r.text[:200], self._secrets()))
            doc = r.json()
            self._tok = doc["access_token"]
            self._tok_exp = time.time() + float(doc.get("expires_in") or 3600)
            return self._tok

    async def _req(self, method: str, path: str, group: str, *, account: bool = False,
                   params: dict | None = None, json: Any = None, retry: int = 2, idempotent: bool = False) -> Any:
        """재시도: 토큰 만료 · 429 는 항상, 5xx 는 GET 이거나 멱등키(clientOrderId)가 있는 주문일 때만."""
        for attempt in range(retry + 1):
            await self._lim[group].wait()
            headers = {"Authorization": "Bearer " + await self.token(), "Accept": "application/json"}
            if account:
                headers["X-Tossinvest-Account"] = str(await self.account_seq())
            if json is not None:
                headers["Content-Type"] = "application/json"
            try:
                r = await self.http.request(method, path, params=params, json=json, headers=headers)
            except httpx.TransportError as e:
                self.calls += 1
                if attempt < retry and (method == "GET" or idempotent):
                    await self._sleep(0.5 * (attempt + 1))
                    continue
                # 주문이 서버에 닿았는지 모른다 → 호출한 쪽이 open_orders 로 확인해야 한다
                raise TossError(0, "transport", scrub(f"{type(e).__name__}: {e}", self._secrets())) from None
            self.calls += 1
            if r.status_code == 200:
                doc = r.json()
                return doc.get("result", doc) if isinstance(doc, dict) else doc
            err = self._err(r)
            if r.status_code == 401 and err.code in {"expired-token", "token-revoked", "invalid-token"} and attempt < retry:
                await self.token(force=True)
                continue
            if r.status_code == 429 and attempt < retry:
                await self._sleep(float(r.headers.get("Retry-After") or 1.0))
                continue
            if r.status_code >= 500 and attempt < retry and (method == "GET" or idempotent):
                await self._sleep(0.5 * (attempt + 1))
                continue
            raise err
        raise RuntimeError("unreachable")

    # ── 계좌 ────────────────────────────────────────────────────────────────
    async def account_seq(self) -> int:
        if self._account_seq is None:
            await self._lim["ACCOUNT"].wait()
            headers = {"Authorization": "Bearer " + await self.token(), "Accept": "application/json"}
            r = await self.http.get("/api/v1/accounts", headers=headers)
            self.calls += 1
            if r.status_code != 200:
                raise self._err(r)
            accts = (r.json() or {}).get("result") or []
            brokerage = [a for a in accts if a.get("accountType") == "BROKERAGE"]
            if len(brokerage) != 1:
                raise TossError(0, "account-choice", f"종합매매(BROKERAGE) 계좌가 {len(brokerage)}개 — 하나여야 한다")
            self._account_seq = int(brokerage[0]["accountSeq"])   # 계좌번호(accountNo)는 쓰지도 저장하지도 않는다
        return self._account_seq

    async def buying_power_usd(self) -> float:
        r = await self._req("GET", "/api/v1/buying-power", "ORDER_INFO", account=True, params={"currency": "USD"})
        return float(r["cashBuyingPower"])

    async def sellable_qty(self, symbol: str) -> float:
        r = await self._req("GET", "/api/v1/sellable-quantity", "ORDER_INFO", account=True, params={"symbol": symbol})
        return float(r["sellableQuantity"])

    async def commission_rate_us(self) -> float | None:
        r = await self._req("GET", "/api/v1/commissions", "ORDER_INFO", account=True)
        for c in r or []:
            if str(c.get("marketCountry")).upper() in {"US", "USA"}:
                return _f(c.get("commissionRate"))
        return None

    async def holdings(self, symbol: str | None = None) -> dict:
        return await self._req("GET", "/api/v1/holdings", "ASSET", account=True,
                               params={"symbol": symbol} if symbol else None)

    # ── 주문 ────────────────────────────────────────────────────────────────
    def _guard(self) -> None:
        if not self.allow_orders:
            raise OrdersNotAllowed("shadow 모드 — 주문 함수 호출은 버그")

    async def place_limit(self, symbol: str, side: str, qty: int, price: float, client_order_id: str | None = None) -> str:
        self._guard()
        if side not in {"BUY", "SELL"} or int(qty) != qty or qty <= 0:
            raise ValueError(f"잘못된 주문: {side} {qty}")
        body = {"clientOrderId": client_order_id or uuid.uuid4().hex[:32], "symbol": symbol, "side": side,
                "orderType": "LIMIT", "timeInForce": "DAY", "quantity": str(int(qty)), "price": fmt_price(price)}
        # 같은 clientOrderId 로 다시 보내면 서버가 앞 주문 결과를 돌려준다 (10분 멱등) → 재시도해도 중복 주문 없음
        r = await self._req("POST", "/api/v1/orders", "ORDER", account=True, json=body, retry=2, idempotent=True)
        return str(r["orderId"])

    async def get_order(self, order_id: str) -> OrderState:
        r = await self._req("GET", f"/api/v1/orders/{order_id}", "ORDER_HISTORY", account=True)
        return parse_order(r)

    async def cancel(self, order_id: str) -> str:
        self._guard()
        r = await self._req("POST", f"/api/v1/orders/{order_id}/cancel", "ORDER", account=True, json={}, retry=0)
        return str(r.get("orderId") or order_id)

    async def modify_price(self, order_id: str, price: float) -> str:
        """미국 주식은 가격만 정정된다. 정정하면 **새 orderId** 가 나온다."""
        self._guard()
        r = await self._req("POST", f"/api/v1/orders/{order_id}/modify", "ORDER", account=True,
                            json={"orderType": "LIMIT", "price": fmt_price(price)}, retry=0)
        return str(r["orderId"])

    async def open_orders(self, symbol: str | None = None) -> list[OrderState]:
        params = {"status": "OPEN"}
        if symbol:
            params["symbol"] = symbol
        r = await self._req("GET", "/api/v1/orders", "ORDER_HISTORY", account=True, params=params)
        return [parse_order(o) for o in (r or {}).get("orders") or []]

    # ── 시세 · 종목 · 장 시간 ────────────────────────────────────────────────
    async def quote(self, symbol: str) -> Quote:
        r = await self._req("GET", "/api/v1/orderbook", "MARKET_DATA", params={"symbol": symbol})
        asks = sorted(p for p in (_f(a.get("price")) for a in r.get("asks") or []) if p and p > 0)
        bids = sorted((p for p in (_f(b.get("price")) for b in r.get("bids") or []) if p and p > 0), reverse=True)
        return Quote(bids[0] if bids else None, asks[0] if asks else None, parse_iso_ms(r.get("timestamp")))

    async def last_prices(self, symbols: Iterable[str]) -> dict[str, float]:
        syms = ",".join(sorted(set(symbols)))
        r = await self._req("GET", "/api/v1/prices", "MARKET_DATA", params={"symbols": syms})
        return {x["symbol"]: float(x["lastPrice"]) for x in r or [] if _f(x.get("lastPrice"))}

    async def recent_trades(self, symbol: str, count: int = 50) -> list[tuple[int, float]]:
        """당일 최근 체결 (최대 50) → 시간순 [(ms, 가격)]."""
        r = await self._req("GET", "/api/v1/trades", "MARKET_DATA", params={"symbol": symbol, "count": count})
        out = [(parse_iso_ms(t.get("timestamp")), _f(t.get("price"))) for t in r or []]
        return sorted((t, p) for t, p in out if t and p)

    async def stocks(self, symbols: Iterable[str]) -> list[dict]:
        r = await self._req("GET", "/api/v1/stocks", "STOCK", params={"symbols": ",".join(sorted(set(symbols)))})
        return list(r or [])

    async def candles(self, symbol: str, interval: str = "1m", count: int = 200, before: str | None = None) -> list[dict]:
        """시간순 `[{start_ms, o, h, l, c, v}]`. 1분봉 timestamp(봉 끝) → 시작 시각으로 바꾼다."""
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "count": count}
        if before:
            params["before"] = before
        r = await self._req("GET", "/api/v1/candles", "MARKET_DATA_CHART", params=params)
        out = []
        for c in (r or {}).get("candles") or []:
            t = parse_iso_ms(c.get("timestamp"))
            vals = [_f(c.get(k)) for k in ("openPrice", "highPrice", "lowPrice", "closePrice", "volume")]
            if t is None or any(v is None for v in vals):
                continue
            start = t - 60_000 if interval == "1m" else t
            out.append({"start_ms": start, "o": vals[0], "h": vals[1], "l": vals[2], "c": vals[3], "v": vals[4]})
        return sorted(out, key=lambda x: x["start_ms"])

    async def us_calendar(self, date: str | None = None) -> dict:
        return await self._req("GET", "/api/v1/market-calendar/US", "MARKET_INFO", params={"date": date} if date else None)

    async def aclose(self) -> None:
        await self.http.aclose()


# 토스에서 애프터마켓 주문이 실제로 되는 끝 = 거래소 애프터 끝 − 10분 (ET 20:00 → 19:50 · 조기마감 17:00 → 16:50).
# 근거: Auto_Trader_Futures D-171 ⑤ · FINDINGS §88 (토스 앱 세션 표). 장 캘린더가 정각(…:00)을 주면 거래소 시각으로 보고 당긴다.
TOSS_AFTER_CUT_MIN = 10


def toss_after_end(end_ms: int) -> int:
    """애프터 끝 시각이 ET 정각이면 거래소 기준으로 보고 10분 당긴다 (이미 …:50 이면 그대로)."""
    t = ms_to(end_ms, ET)
    return end_ms - TOSS_AFTER_CUT_MIN * 60_000 if t.minute == 0 and t.second == 0 else end_ms


def sessions_from_calendar_day(day: dict | None) -> dict[str, tuple[int, int]]:
    """`UsMarketDay` → {세션: (시작 ms, 끝 ms)} (휴장 세션은 빠진다 · 애프터 끝은 `toss_after_end` 로 보정)."""
    out = {}
    for key, name in (("dayMarket", "day"), ("preMarket", "pre"), ("regularMarket", "regular"), ("afterMarket", "after")):
        s = (day or {}).get(key)
        if s:
            a, b = parse_iso_ms(s.get("startTime")), parse_iso_ms(s.get("endTime"))
            if a and b:
                out[name] = (a, toss_after_end(b) if name == "after" else b)
    return out


def raw_after_end(day: dict | None) -> int | None:
    """보정 전 장 캘린더의 애프터 끝 (확인 · 기록용)."""
    s = (day or {}).get("afterMarket") or {}
    return parse_iso_ms(s.get("endTime"))
