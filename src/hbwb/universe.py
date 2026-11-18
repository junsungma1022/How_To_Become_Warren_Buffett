"""종목 범위: 시가총액 $5M ~ $300M (사용자 결정 2026-11-18 · 이전 U1 의 시총 기준 · 거래량 조건 없음).

시가총액 = 토스 `/api/v1/stocks` 의 sharesOutstanding × 전일 종가 (토스 일봉).
모르는 것은 추측하지 않는다 — 주식 수 · 전일 종가 · 시장 · 종목 유형 중 하나라도 없으면 'unclassified' 로 빼고 기록한다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .broker.toss import TossClient, TossError
from .clock import ET, ms_to


@dataclass
class UniverseCheck:
    ok: bool
    reason: str
    market_cap: float | None = None
    prev_close: float | None = None
    shares: float | None = None
    market: str | None = None
    name: str | None = None


def judge(info: dict | None, prev_close: float | None, cfg: dict) -> UniverseCheck:
    u = cfg["universe"]
    if not info:
        return UniverseCheck(False, "unclassified:no_stock_info")
    market, stype = info.get("market"), info.get("securityType")
    name = info.get("englishName") or info.get("name")
    try:
        shares = float(info.get("sharesOutstanding"))
    except (TypeError, ValueError):
        shares = None
    if info.get("status") != "ACTIVE":
        return UniverseCheck(False, f"status:{info.get('status')}", market=market, name=name)
    if market not in u["markets"]:
        return UniverseCheck(False, f"market:{market}", market=market, name=name)
    if stype not in u["security_types"]:
        return UniverseCheck(False, f"security_type:{stype}", market=market, name=name)
    if u.get("common_share_only") and info.get("isCommonShare") is False:
        return UniverseCheck(False, "not_common_share", market=market, name=name)
    if not shares or shares <= 0:
        return UniverseCheck(False, "unclassified:no_shares", market=market, name=name)
    if not prev_close or prev_close <= 0:
        return UniverseCheck(False, "unclassified:no_prev_close", shares=shares, market=market, name=name)
    mcap = shares * prev_close
    if not u["min_market_cap_usd"] <= mcap <= u["max_market_cap_usd"]:
        return UniverseCheck(False, "market_cap_out", mcap, prev_close, shares, market, name)
    return UniverseCheck(True, "ok", mcap, prev_close, shares, market, name)


def prev_close_from_daily(daily: list[dict], ref_ms: int) -> float | None:
    """`ref_ms` 의 미국 현지 날짜보다 **앞선** 마지막 일봉 종가 (일봉 timestamp = 그 거래일 현지 자정)."""
    ref_day = ms_to(ref_ms, ET).date()
    best = None
    for c in daily:
        d = ms_to(c["start_ms"], ET).date()
        if d < ref_day and (best is None or c["start_ms"] > best["start_ms"]):
            best = c
    return best["c"] if best else None


class Universe:
    """종목 정보 · 전일 종가를 (종목, 날짜) 단위로 기억해 토스 호출을 줄인다."""

    def __init__(self, toss: TossClient, cfg: dict):
        self.toss, self.cfg = toss, cfg
        self._info: dict[str, dict | None] = {}
        self._prev: dict[tuple[str, dt.date], float | None] = {}

    async def check(self, symbol: str, ref_ms: int) -> UniverseCheck:
        try:
            if symbol not in self._info:
                rows = await self.toss.stocks([symbol])
                self._info[symbol] = next((r for r in rows if str(r.get("symbol")).upper() == symbol), None)
            key = (symbol, ms_to(ref_ms, ET).date())
            if key not in self._prev and self._info[symbol]:
                daily = await self.toss.candles(symbol, "1d", count=10)
                self._prev[key] = prev_close_from_daily(daily, ref_ms)
        except TossError as e:
            if e.status in (400, 404):  # unsupported-symbol · stock-not-found
                self._info[symbol] = None
                return UniverseCheck(False, f"unclassified:{e.code}")
            raise
        return judge(self._info.get(symbol), self._prev.get((symbol, ms_to(ref_ms, ET).date())), self.cfg)
