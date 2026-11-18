"""1분봉 위에서 같은 규칙(rules.py)으로 한 거래를 흉내 낸다 — KNIME 정답 라벨 계산용.

가정 (보수적):
- 진입: 기사 발행 + entry_delay_s 뒤 처음 시작하는 1분봉의 시가에 1차 매수 (지정가 슬리피지만큼 비싸게)
- 한 봉 안의 순서는 시가 → 저가 → 고가 → 종가 로 본다 (손절이 익절보다 먼저 닿았다고 본다)
- 체결가는 규칙이 발동한 가격 그대로 (손절은 손절선, 익절은 익절선, 시간 청산은 그 봉 시가)
- 수수료는 매수 · 매도 모두 fee_rate
진입 후 10분 안에 봉이 없으면 '살 수 없었음'(tradeable=False)으로 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import Fill, Position, entry2_qty, entry2_triggered, exit_decision, plan_entry1, update_peak


@dataclass
class SimResult:
    tradeable: bool
    entry_ms: int | None = None
    entry_price: float | None = None
    exit_ms: int | None = None
    pnl_usd: float = 0.0
    ret_pct: float = 0.0
    max_up_pct: float = 0.0           # 진입가 대비 보유 구간 최고가 (%)
    max_down_pct: float = 0.0
    exit_reasons: str = ""
    did_entry2: bool = False
    win: bool = False


def _rule_price(reason: str, avg: float, peak: float, cfg: dict, fallback: float) -> float:
    x = cfg["exit"]
    if reason == "stop_loss":
        return avg * (1 + x["stop_loss_pct"] / 100)
    if reason == "tp2":
        return avg * (1 + x["tp2_pct"] / 100)
    if reason in ("tp1", "tp1_all_1share"):
        return avg * (1 + x["tp1_pct"] / 100)
    if reason == "trailing_stop":
        return peak * (1 - x["trail_pct"] / 100)
    return fallback


def simulate(bars: list[dict], t0_ms: int, cfg: dict, budget: float, fee_rate: float,
             entry_delay_s: int = 60, session_end_ms: int | None = None) -> SimResult:
    """bars: 시간순 [{start_ms, o, h, l, c, v}] (1분)."""
    start = t0_ms + entry_delay_s * 1000
    idx = next((i for i, b in enumerate(bars) if b["start_ms"] >= start), None)
    if idx is None or bars[idx]["start_ms"] - start > 10 * 60_000:
        return SimResult(False)
    b0 = bars[idx]
    q1, _total, limit = plan_entry1(budget, b0["o"], cfg, fee_rate)
    if q1 <= 0:
        return SimResult(False)
    pos = Position("SIM", budget)
    pos.add(Fill("BUY", q1, limit, b0["start_ms"], "entry1", q1 * limit * fee_rate))
    pos.peak = limit
    cash = budget - q1 * limit * (1 + fee_rate)
    hi, lo = limit, limit
    reasons = []
    for b in bars[idx:]:
        for k, p in (("o", b["o"]), ("l", b["l"]), ("h", b["h"]), ("c", b["c"])):
            # 시간 · 장마감 판단은 봉 시작 시각, 가격 판단은 봉 안의 각 점
            now = b["start_ms"] if k == "o" else b["start_ms"] + 59_000
            if pos.qty <= 0:
                break
            hi, lo = max(hi, p), min(lo, p)
            if entry2_triggered(pos, p, now, cfg):
                q2, lim2 = entry2_qty(pos, p, cash, cfg, fee_rate)
                pos.entry2_done = True
                if q2 > 0:
                    pos.add(Fill("BUY", q2, lim2, now, "entry2", q2 * lim2 * fee_rate))
                    cash -= q2 * lim2 * (1 + fee_rate)
            update_peak(pos, p)
            while pos.qty > 0:
                ex = exit_decision(pos, p, now, session_end_ms, cfg)
                if ex is None:
                    break
                px = _rule_price(ex.reason, pos.avg_price or p, pos.peak, cfg, p)
                pos.add(Fill("SELL", ex.qty, px, now, ex.reason, ex.qty * px * fee_rate))
                reasons.append(ex.reason)
        if pos.qty <= 0:
            break
    if pos.qty > 0:  # 자료 끝 — 마지막 종가로 정리
        last = bars[-1]
        pos.add(Fill("SELL", pos.qty, last["c"], last["start_ms"] + 59_000, "data_end", pos.qty * last["c"] * fee_rate))
        reasons.append("data_end")
    pnl = pos.realized_pnl()
    entry_px = pos.avg_price or limit
    return SimResult(True, b0["start_ms"], round(entry_px, 6), pos.fills[-1].t_ms, round(pnl, 4),
                     round(100 * pnl / pos.cost, 4) if pos.cost else 0.0,
                     round(100 * (hi / limit - 1), 4), round(100 * (lo / limit - 1), 4),
                     "|".join(reasons), pos.entry2_done and any(f.reason == "entry2" for f in pos.fills), pnl > 0)
