"""주문 실행.

- LiveExecutor  : 토스 지정가 주문 (장외에서는 지정가만 받으므로 늘 지정가). 매수는 시간 안에 못 채운 잔량을 취소,
                  매도는 가격을 고쳐 다시 내기를 reprice_max 번까지 하고, 그래도 남으면 critical 알림.
- ShadowExecutor: 주문을 보내지 않는다. 지금 호가에 바로 체결됐다고 기록만 한다 (shadow 모드 점검용).

반환값은 이 주문 묶음 전체의 체결 요약 하나(`Fill`) — 체결이 없으면 None.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Awaitable, Callable

from ..broker.toss import OrderState, Quote, TossClient, TossError
from ..clock import now_ms
from ..strategy.rules import Fill

LogFn = Callable[..., dict]
AlertFn = Callable[[str, str], Awaitable[None]]


def _agg(states: list[OrderState], side: str, reason: str, fee_rate: float) -> Fill | None:
    qty = sum(s.filled_qty for s in states)
    if qty <= 0:
        return None
    notional = sum(s.filled_qty * (s.avg_price or 0.0) for s in states)
    fee = sum(s.commission for s in states if s.commission is not None)
    if not any(s.commission is not None for s in states):
        fee = notional * fee_rate
    return Fill(side, int(round(qty)), notional / qty, now_ms(), reason, fee)


class LiveExecutor:
    mode = "live"

    def __init__(self, toss: TossClient, cfg: dict, log: LogFn, alert: AlertFn, fee_rate: float, sleep=asyncio.sleep):
        self.toss, self.cfg, self.log, self.alert, self.fee_rate, self._sleep = toss, cfg, log, alert, fee_rate, sleep

    async def _wait(self, oid: str, timeout_s: float) -> OrderState:
        st = await self.toss.get_order(oid)
        waited = 0.0
        while not st.done and waited < timeout_s:
            await self._sleep(0.7)
            waited += 0.7
            st = await self.toss.get_order(oid)
        return st

    async def buy(self, symbol: str, qty: int, limit: float, reason: str) -> Fill | None:
        e = self.cfg["entry"]
        coid = uuid.uuid4().hex[:32]
        try:
            oid = await self.toss.place_limit(symbol, "BUY", qty, limit, coid)
        except TossError as err:
            self.log("order_error", side="BUY", symbol=symbol, qty=qty, limit=limit, code=err.code, status=err.status, msg=err.message)
            if err.code == "transport":
                await self.alert("critical", f"🔴 {symbol} 매수 주문 전송 결과를 모른다 — 토스 앱에서 미체결 주문을 확인할 것")
            return None
        self.log("order_sent", side="BUY", symbol=symbol, qty=qty, limit=limit, order_id=oid, reason=reason)
        st = await self._wait(oid, e["fill_timeout_s"])
        if not st.done:
            try:
                await self.toss.cancel(oid)
            except TossError as err:
                self.log("order_error", side="CANCEL", symbol=symbol, order_id=oid, code=err.code, msg=err.message)
            st = await self._wait(oid, 5)
        self.log("order_final", side="BUY", symbol=symbol, order_id=oid, status=st.status, filled=st.filled_qty, avg=st.avg_price)
        return _agg([st], "BUY", reason, self.fee_rate)

    async def sell(self, symbol: str, qty: int, reason: str, quote: Callable[[], Awaitable[Quote]],
                   last: Callable[[], Awaitable[float | None]]) -> Fill | None:
        x = self.cfg["exit"]
        states: list[OrderState] = []
        remaining = qty
        oid = None
        for attempt in range(x["reprice_max"] + 1):
            q = await quote()
            ref = q.bid or await last()
            if not ref:
                await self.alert("critical", f"🔴 {symbol} 매도 기준 가격이 없다 (호가 · 체결 없음) — 수동 확인")
                break
            limit = ref * (1 - x["limit_slippage_pct"] / 100)
            try:
                if oid is None:
                    oid = await self.toss.place_limit(symbol, "SELL", remaining, limit, uuid.uuid4().hex[:32])
                    self.log("order_sent", side="SELL", symbol=symbol, qty=remaining, limit=limit, order_id=oid, reason=reason)
                else:
                    old = oid
                    oid = await self.toss.modify_price(oid, limit)
                    # 앞 주문은 REPLACED 로 끝나고 그때까지의 체결은 앞 주문에 남는다 → 따로 모은다
                    states.append(await self._wait(old, 3))
                    self.log("order_reprice", symbol=symbol, old_order_id=old, order_id=oid, limit=limit, attempt=attempt)
            except TossError as err:
                self.log("order_error", side="SELL", symbol=symbol, qty=remaining, limit=limit, code=err.code, msg=err.message)
                if err.code in {"already-filled"}:
                    break
                if oid is None:
                    await self._sleep(1.0)
                    continue
            st = await self._wait(oid, x["fill_timeout_s"])
            if st.status == "FILLED" or (st.done and st.status != "REPLACED"):
                states.append(st)
                oid = None
                remaining = qty - int(round(sum(s.filled_qty for s in states)))
                if remaining <= 0:
                    break
            # 미체결이면 다음 바퀴에서 가격을 고친다 (정정은 새 orderId · 앞 주문의 체결분은 그 주문에 남는다)
        if oid is not None:
            try:
                await self.toss.cancel(oid)
            except TossError as err:
                self.log("order_error", side="CANCEL", symbol=symbol, order_id=oid, code=err.code, msg=err.message)
            states.append(await self._wait(oid, 5))
        fill = _agg(states, "SELL", reason, self.fee_rate)
        sold = fill.qty if fill else 0
        if sold < qty:
            await self.alert("critical", f"🔴 {symbol} {reason} 매도 {qty}주 중 {sold}주만 체결 — 남은 {qty - sold}주 수동 확인")
        return fill


class ShadowExecutor:
    """주문 없음. 지금 호가에 바로 체결됐다고 가정해 기록만 한다."""

    mode = "shadow"

    def __init__(self, cfg: dict, log: LogFn, fee_rate: float, quote_fn: Callable[[str], Awaitable[Quote]]):
        self.cfg, self.log, self.fee_rate, self.quote_fn = cfg, log, fee_rate, quote_fn

    async def buy(self, symbol: str, qty: int, limit: float, reason: str) -> Fill | None:
        q = await self.quote_fn(symbol)
        if q.ask is None or q.ask > limit:
            self.log("shadow_no_fill", side="BUY", symbol=symbol, ask=q.ask, limit=limit)
            return None
        f = Fill("BUY", qty, q.ask, now_ms(), reason, qty * q.ask * self.fee_rate)
        self.log("shadow_fill", side="BUY", symbol=symbol, qty=qty, price=q.ask, reason=reason)
        return f

    async def sell(self, symbol: str, qty: int, reason: str, quote, last) -> Fill | None:
        q = await quote()
        px = q.bid or await last()
        if not px:
            self.log("shadow_no_fill", side="SELL", symbol=symbol)
            return None
        self.log("shadow_fill", side="SELL", symbol=symbol, qty=qty, price=px, reason=reason)
        return Fill("SELL", qty, px, now_ms(), reason, qty * px * self.fee_rate)
