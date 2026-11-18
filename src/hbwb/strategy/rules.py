"""분할매수 · 분할매도 규칙 — 순수 함수 (네트워크 없음). 실거래 엔진과 과거 시뮬레이션(KNIME 라벨)이 **같은 코드**를 쓴다.

분할매수 (2회)
  1차: 예산 × first_fraction 만큼 (주식 수는 내림)
  2차: 1차 체결 뒤 second_window_min 안에 가격 ≥ 1차 평균가 × (1 + second_trigger_pct%) 이면 남은 예산으로.
       1차 익절이 이미 났으면 하지 않는다. 내려갈 때 더 사는 물타기는 없다.
분할매도
  손절: 가격 ≤ 평균가 × (1 + stop_loss_pct%) → 전량
  1차 익절: 가격 ≥ 평균가 × (1 + tp1_pct%) → 보유량 × tp1_fraction (내림 · 1주뿐이면 전량)
  남은 물량: 가격 ≥ 평균가 × (1 + tp2_pct%) → 전량 · 또는 (1차 익절 뒤) 가격 ≤ 최고가 × (1 − trail_pct%) → 전량
  시간: 첫 체결 뒤 max_hold_min → 전량
  장 마감: 마지막 거래 세션 끝 session_close_buffer_min 전 → 전량
우선순위: 손절 > 장 마감 > 시간 > 2차 익절 > 추적 손절 > 1차 익절 (손실을 막는 쪽이 먼저)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Fill:
    side: str          # BUY | SELL
    qty: int
    price: float
    t_ms: int
    reason: str
    fee: float = 0.0


@dataclass
class Position:
    symbol: str
    budget: float
    fills: list[Fill] = field(default_factory=list)
    first_fill_ms: int | None = None
    peak: float = 0.0
    tp1_done: bool = False
    entry2_done: bool = False

    @property
    def qty(self) -> int:
        return sum(f.qty if f.side == "BUY" else -f.qty for f in self.fills)

    @property
    def bought_qty(self) -> int:
        return sum(f.qty for f in self.fills if f.side == "BUY")

    @property
    def avg_price(self) -> float | None:
        """매수 체결의 가중 평균 (매도로 바뀌지 않는다 — 익절 · 손절 기준가)."""
        q = self.bought_qty
        if q <= 0:
            return None
        return sum(f.qty * f.price for f in self.fills if f.side == "BUY") / q

    @property
    def cost(self) -> float:
        return sum(f.qty * f.price for f in self.fills if f.side == "BUY")

    @property
    def fees(self) -> float:
        return sum(f.fee for f in self.fills)

    def realized_pnl(self) -> float:
        """매도된 수량만큼의 실현 손익 (수수료 포함)."""
        avg = self.avg_price or 0.0
        sold = [f for f in self.fills if f.side == "SELL"]
        return sum(f.qty * (f.price - avg) for f in sold) - self.fees

    def add(self, fill: Fill) -> None:
        if fill.qty <= 0:
            return
        self.fills.append(fill)
        if fill.side == "BUY":
            if self.first_fill_ms is None:
                self.first_fill_ms = fill.t_ms
            if fill.reason == "entry2":
                self.entry2_done = True
        if fill.reason.startswith("tp1"):
            self.tp1_done = True
        self.peak = max(self.peak, fill.price)

    @property
    def closed(self) -> bool:
        return self.bought_qty > 0 and self.qty == 0


def shares_for(cash: float, price: float, fee_rate: float) -> int:
    if price <= 0 or cash <= 0:
        return 0
    return int(math.floor(cash / (price * (1 + fee_rate)) + 1e-9))


def plan_entry1(budget: float, ask: float, cfg: dict, fee_rate: float) -> tuple[int, int, float]:
    """→ (1차 수량, 전체 계획 수량, 지정가). 1주도 못 사면 (0, 0, 가격)."""
    e = cfg["entry"]
    limit = ask * (1 + e["limit_slippage_pct"] / 100)
    total = shares_for(budget, limit, fee_rate)
    if total <= 0:
        return 0, 0, limit
    if total == 1:
        return 1, 1, limit          # 1주는 나눌 수 없다 → 분할 없이
    q1 = max(1, int(math.floor(total * e["first_fraction"])))
    return min(q1, total), total, limit


def entry2_qty(pos: Position, ask: float, cash_left: float, cfg: dict, fee_rate: float) -> tuple[int, float]:
    limit = ask * (1 + cfg["entry"]["limit_slippage_pct"] / 100)
    return shares_for(cash_left, limit, fee_rate), limit


def entry2_triggered(pos: Position, price: float, now_ms: int, cfg: dict) -> bool:
    e = cfg["entry"]
    if pos.entry2_done or pos.tp1_done or pos.qty <= 0 or pos.first_fill_ms is None or pos.avg_price is None:
        return False
    if now_ms - pos.first_fill_ms > e["second_window_min"] * 60_000:
        return False
    return price >= pos.avg_price * (1 + e["second_trigger_pct"] / 100)


@dataclass
class ExitOrder:
    qty: int
    reason: str


def exit_decision(pos: Position, price: float, now_ms: int, session_end_ms: int | None, cfg: dict) -> ExitOrder | None:
    """지금 가격에서 팔아야 하면 (수량, 이유). `pos.peak` 는 호출 전에 갱신해 둔다 (update_peak)."""
    x = cfg["exit"]
    q, avg = pos.qty, pos.avg_price
    if q <= 0 or avg is None:
        return None
    if price <= avg * (1 + x["stop_loss_pct"] / 100):
        return ExitOrder(q, "stop_loss")
    if session_end_ms is not None and now_ms >= session_end_ms - x["session_close_buffer_min"] * 60_000:
        return ExitOrder(q, "session_close")
    if pos.first_fill_ms is not None and now_ms - pos.first_fill_ms >= x["max_hold_min"] * 60_000:
        return ExitOrder(q, "time_limit")
    if price >= avg * (1 + x["tp2_pct"] / 100):
        return ExitOrder(q, "tp2")
    if pos.tp1_done and price <= pos.peak * (1 - x["trail_pct"] / 100):
        return ExitOrder(q, "trailing_stop")
    if not pos.tp1_done and price >= avg * (1 + x["tp1_pct"] / 100):
        part = int(math.floor(q * x["tp1_fraction"]))
        return ExitOrder(q, "tp1_all_1share") if part <= 0 else ExitOrder(part, "tp1")
    return None


def update_peak(pos: Position, price: float) -> None:
    if pos.qty > 0:
        pos.peak = max(pos.peak, price)
