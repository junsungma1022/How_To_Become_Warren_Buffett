import asyncio
import json

import httpx
import pytest

from hbwb.broker.toss import OrdersNotAllowed, TossClient, TossError, fmt_price, sessions_from_calendar_day


async def _nosleep(_s):
    return None


def make(handler, allow_orders=False):
    http = httpx.AsyncClient(base_url="https://openapi.tossinvest.com", transport=httpx.MockTransport(handler))
    return TossClient("cid-SECRET1", "sec-SECRET2", allow_orders=allow_orders, http=http, sleep=_nosleep)


def base_handler(calls, extra=None):
    def h(req: httpx.Request):
        calls.append(req)
        if req.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "eyJTOKENTOKENTOKEN", "token_type": "Bearer", "expires_in": 86400})
        if req.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"result": [{"accountNo": "123-45", "accountSeq": 7, "accountType": "BROKERAGE"},
                                                        {"accountNo": "999", "accountSeq": 9, "accountType": "PENSION_SAVINGS"}]})
        if extra:
            r = extra(req)
            if r is not None:
                return r
        return httpx.Response(404, json={"error": {"requestId": "r1", "code": "not-mocked", "message": req.url.path}})
    return h


def run(coro):
    return asyncio.run(coro)


def test_fmt_price_truncates():
    assert fmt_price(1.23999) == "1.23"
    assert fmt_price(0.123456) == "0.1234"
    assert fmt_price(12) == "12.00"
    with pytest.raises(ValueError):
        fmt_price(0.00001)


def test_order_body_and_account_header():
    calls = []

    def extra(req):
        if req.url.path == "/api/v1/orders" and req.method == "POST":
            return httpx.Response(200, json={"result": {"orderId": "o-1", "clientOrderId": "c"}})
    c = make(base_handler(calls, extra), allow_orders=True)
    oid = run(c.place_limit("ABCD", "BUY", 12, 1.23456, "coid1"))
    assert oid == "o-1"
    post = [r for r in calls if r.url.path == "/api/v1/orders"][0]
    body = json.loads(post.content)
    assert body == {"clientOrderId": "coid1", "symbol": "ABCD", "side": "BUY", "orderType": "LIMIT",
                    "timeInForce": "DAY", "quantity": "12", "price": "1.23"}
    assert post.headers["X-Tossinvest-Account"] == "7"           # BROKERAGE 계좌의 accountSeq
    assert post.headers["Authorization"] == "Bearer eyJTOKENTOKENTOKEN"


def test_shadow_client_cannot_order():
    c = make(base_handler([]), allow_orders=False)
    with pytest.raises(OrdersNotAllowed):
        run(c.place_limit("ABCD", "BUY", 1, 1.0))
    with pytest.raises(OrdersNotAllowed):
        run(c.cancel("o-1"))


def test_token_revoked_retries_once():
    calls, n = [], {"k": 0}

    def extra(req):
        if req.url.path == "/api/v1/prices":
            n["k"] += 1
            if n["k"] == 1:
                return httpx.Response(401, json={"error": {"requestId": "r", "code": "token-revoked", "message": "x"}})
            return httpx.Response(200, json={"result": [{"symbol": "ABCD", "lastPrice": "1.5", "currency": "USD"}]})
    c = make(base_handler(calls, extra))
    assert run(c.last_prices(["ABCD"])) == {"ABCD": 1.5}
    assert sum(r.url.path == "/oauth2/token" for r in calls) == 2


def test_error_message_is_scrubbed():
    def extra(req):
        if req.url.path == "/api/v1/prices":
            return httpx.Response(403, json={"error": {"requestId": "r", "code": "ip-not-allowed",
                                                       "message": "bad sec-SECRET2 eyJTOKENTOKENTOKEN"}})
    c = make(base_handler([], extra))
    with pytest.raises(TossError) as e:
        run(c.last_prices(["ABCD"]))
    assert e.value.code == "ip-not-allowed"
    assert "SECRET2" not in str(e.value) and "TOKENTOKEN" not in str(e.value)


def test_candles_end_time_to_start():
    def extra(req):
        if req.url.path == "/api/v1/candles":
            return httpx.Response(200, json={"result": {"candles": [
                {"timestamp": "2026-11-18T09:32:00-05:00", "openPrice": "2", "highPrice": "3", "lowPrice": "1", "closePrice": "2.5", "volume": "10", "currency": "USD"},
                {"timestamp": "2026-11-18T09:31:00-05:00", "openPrice": "1", "highPrice": "2", "lowPrice": "1", "closePrice": "2", "volume": "5", "currency": "USD"}],
                "nextBefore": None}})
    c = make(base_handler([], extra))
    bars = run(c.candles("ABCD", "1m", 2))
    assert [b["c"] for b in bars] == [2.0, 2.5]                      # 시간순
    assert bars[1]["start_ms"] - bars[0]["start_ms"] == 60_000


def test_calendar_sessions():
    day = {"date": "2026-11-18", "dayMarket": None,
           "preMarket": {"startTime": "2026-11-18T18:00:00+09:00", "endTime": "2026-11-18T23:30:00+09:00"},
           "regularMarket": {"startTime": "2026-11-18T23:30:00+09:00", "endTime": "2026-11-19T06:00:00+09:00"},
           "afterMarket": {"startTime": "2026-11-19T06:00:00+09:00", "endTime": "2026-11-19T10:00:00+09:00"}}
    s = sessions_from_calendar_day(day)
    assert set(s) == {"pre", "regular", "after"}
