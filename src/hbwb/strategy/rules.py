"""매수 · 매도 규칙 — 순수 함수 (네트워크 없음). 실거래 엔진과 과거 시뮬레이션(KNIME 라벨)이 **같은 코드**를 쓴다.

출처: 이전 프로그램 `junsungma1022/Auto_Trading_Program` (`9a780e7`) 의 `position_monitor` · `entry_engine` 규칙을
Auto_Trader_Futures D-154 (5) 에 옮겨 적은 그대로. 바꾼 것은 매수 신호 하나 (사용자 2026-11-19).

자금
  3칸: 칸 하나 = 자금의 1/3 로 한 종목. 3칸 모두 보유 중이면 매수 안 함 (칸 잠금 없음).
매수 (한 번에 · 분할 없음)
  Claude BUY 뒤, 뉴스 발행 시각부터 trigger_window_min(5분) 안에 가격 ≥ 기준가 × (1 + trigger_rise_pct%)(+5%) 이면
  지정가 = 신호 가격 × (1 + limit_premium_pct%)(0.7%) 로 칸 금액만큼.
  기준가 = 발행 전에 끝난 마지막 1분봉 종가 (30분 넘게 낡았으면 발행 뒤 첫 체결가).
매도 (한 번 점검에 하나 · 위에서부터)
  ① 손익 ≤ −7% → 전량. 진입 30초 안에는 ①만 본다.
  ② 15분 지났고 손익 < +5% → 전량 · 5분 지났고 손익 < +2% → 전량
  ③ 손익 ≥ +15% 이고 TP3 전 → 원수량 30% (트레일링 켬) · ≥ +10% TP2 전 → 30% · ≥ +5% TP1 전 → 30%
  ④ 트레일링이 켜졌으면 고점 대비 ≤ −5% (ET 09:25~09:35 는 −3%) → 전량
  + 장 마감: 매매하는 마지막 세션(애프터마켓) 끝 5분 전 → 전량 (①과 ② 사이)
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from ..clock import ET, ms_to


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
    slot: int
    fills: list[Fill] = field(default_factory=list)
    entry_ms: int | None = None
    peak: float = 0.0
    tp_done: list[int] = field(default_factory=list)   # 끝낸 익절 단계의 번호 (take_profits 목록의 인덱스)
    trailing: bool = False

    @property
    def qty(self) -> int:
        return sum(f.qty if f.side == "BUY" else -f.qty for f in self.fills)

    @property
    def orig_qty(self) -> int:
        return sum(f.qty for f in self.fills if f.side == "BUY")

    @property
    def avg_price(self) -> float | None:
        q = self.orig_qty
        return None if q <= 0 else sum(f.qty * f.price for f in self.fills if f.side == "BUY") / q

    @property
    def cost(self) -> float:
        return sum(f.qty * f.price for f in self.fills if f.side == "BUY")

    @property
    def fees(self) -> float:
        return sum(f.fee for f in self.fills)

    def realized_pnl(self) -> float:
        avg = self.avg_price or 0.0
        return sum(f.qty * (f.price - avg) for f in self.fills if f.side == "SELL") - self.fees

    def pnl_pct(self, price: float) -> float:
        return 100.0 * (price / self.avg_price - 1) if self.avg_price else 0.0

    def add(self, fill: Fill) -> None:
        if fill.qty <= 0:
            return
        self.fills.append(fill)
        if fill.side == "BUY" and self.entry_ms is None:
            self.entry_ms = fill.t_ms
        self.peak = max(self.peak, fill.price)

    @property
    def closed(self) -> bool:
        return self.orig_qty > 0 and self.qty == 0


def shares_for(cash: float, price: float, fee_rate: float) -> int:
    if price <= 0 or cash <= 0:
        return 0
    return int(math.floor(cash / (price * (1 + fee_rate)) + 1e-9))


def slot_budget(equity: float, cash: float, cfg: dict) -> float:
    """칸 하나의 금액 = min(설정 예산, 지금 자산) / 칸 수 — 단 쓸 수 있는 현금보다 크지 않게."""
    base = min(float(cfg["capital"]["budget_usd"]), equity)
    return max(0.0, min(base / int(cfg["capital"]["slots"]), cash))


def plan_entry(slot_usd: float, signal_price: float, cfg: dict, fee_rate: float) -> tuple[int, float]:
    """→ (수량, 지정가). 1주도 못 사면 수량 0."""
    limit = signal_price * (1 + cfg["entry"]["limit_premium_pct"] / 100)
    return shares_for(slot_usd, limit, fee_rate), limit


def entry_signal(ref_price: float | None, price: float, now_ms: int, published_ms: int, cfg: dict) -> str | None:
    """'signal' · 'expired' · None(아직)."""
    e = cfg["entry"]
    if now_ms > published_ms + e["trigger_window_min"] * 60_000:
        return "expired"
    if ref_price and price >= ref_price * (1 + e["trigger_rise_pct"] / 100) * (1 - 1e-9):
        return "signal"
    return None


def ref_from_bars(bars: list[dict], published_ms: int, cfg: dict) -> float | None:
    """발행 전에 끝난 마지막 1분봉 종가 (ref_max_age_min 안일 때만)."""
    lim = published_ms - cfg["entry"]["ref_max_age_min"] * 60_000
    done = [b for b in bars if b["start_ms"] + 60_000 <= published_ms and b["start_ms"] + 60_000 >= lim]
    return done[-1]["c"] if done else None


@dataclass
class ExitOrder:
    qty: int
    reason: str
    tp_index: int | None = None


def _in_open_window(now_ms: int) -> bool:
    t = ms_to(now_ms, ET).time()
    return dt.time(9, 25) <= t <= dt.time(9, 35)


def exit_decision(pos: Position, price: float, now_ms: int, session_end_ms: int | None, cfg: dict) -> ExitOrder | None:
    """지금 가격에서 팔아야 하면 (수량, 이유). 호출 전에 update_peak 로 고점을 갱신해 둔다."""
    x = cfg["exit"]
    q = pos.qty
    if q <= 0 or pos.avg_price is None or pos.entry_ms is None:
        return None
    pnl = pos.pnl_pct(price)
    eps = 1e-9                     # 부동소수 오차로 정확히 −7% · +5% 를 놓치지 않게
    held_s = (now_ms - pos.entry_ms) / 1000
    if pnl <= x["stop_loss_pct"] + eps:                                                   # ①
        return ExitOrder(q, "stop_loss")
    if held_s < x["grace_s"]:
        return None
    if session_end_ms is not None and now_ms >= session_end_ms - x["session_close_buffer_min"] * 60_000:
        return ExitOrder(q, "session_close")
    for minutes, min_pnl in x["time_stops"]:                                        # ② 긴 것부터
        if held_s > minutes * 60 and pnl < min_pnl - eps:
            return ExitOrder(q, f"time_stop_{int(minutes)}m")
    n = len(x["take_profits"])
    for i, (lvl, frac) in enumerate(x["take_profits"]):                            # ③ 높은 단계부터 하나
        if pnl >= lvl - eps and i not in pos.tp_done:
            part = max(1, int(math.floor(pos.orig_qty * frac)))
            return ExitOrder(min(part, q), f"tp{n - i}", i)
    if pos.trailing and pos.peak > 0:                                               # ④
        gap = x["trail_open_pct"] if _in_open_window(now_ms) else x["trail_pct"]
        if price <= pos.peak * (1 - gap / 100) * (1 + eps):
            return ExitOrder(q, "trailing_stop")
    return None


def apply_exit(pos: Position, order: ExitOrder) -> None:
    """매도가 체결된 뒤: 그 익절 단계를 끝난 것으로 표시하고, 가장 높은 단계(인덱스 0 = TP3)면 트레일링을 켠다.
    낮은 단계는 건너뛰지 않는다 — 가격이 한 번에 +15% 로 가도 다음 점검들에서 TP2 · TP1 이 차례로 30% 씩 나간다 (이전 프로그램)."""
    if order.tp_index is not None and order.tp_index not in pos.tp_done:
        pos.tp_done.append(order.tp_index)
        if order.tp_index == 0:
            pos.trailing = True


def update_peak(pos: Position, price: float) -> None:
    if pos.qty > 0:
        pos.peak = max(pos.peak, price)
