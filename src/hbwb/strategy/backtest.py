"""1분봉 위에서 같은 규칙(rules.py)으로 한 거래를 흉내 낸다 — KNIME 정답 라벨 계산용.

가정 (보수적):
- AI 판단에 decision_delay_s(60초) 걸린다고 보고, 그 뒤부터 발행 +5분까지 신호를 찾는다
- 기준가 = 발행 전에 끝난 마지막 1분봉 종가 (없으면 발행 뒤 첫 봉 시가)
- 신호: 봉 고가 ≥ 기준가 × 1.05 → 진입가 = 기준가 × 1.05 × 1.007 (지정가 그대로 체결됐다고 봄)
  · 판단 시점에 이미 +5% 위면 그 봉 시가 × 1.007
- 한 봉 안의 순서는 시가 → 저가 → 고가 → 종가 (손절이 익절보다 먼저 닿았다고 본다)
- 매도 체결가는 규칙이 발동한 선 그대로 (손절선 · 익절선 · 트레일링선), 시간 · 장마감 청산은 그 점의 가격
- 수수료는 매수 · 매도 모두 fee_rate
신호가 없으면 triggered=False (매수 안 함).
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import Fill, Position, apply_exit, exit_decision, plan_entry, ref_from_bars, update_peak


@dataclass
class SimResult:
    triggered: bool
    ref_price: float | None = None
    entry_ms: int | None = None
    entry_price: float | None = None
    exit_ms: int | None = None
    pnl_usd: float = 0.0
    ret_pct: float = 0.0
    max_up_pct: float = 0.0
    max_down_pct: float = 0.0
    exit_reasons: str = ""
    win: bool = False
    why: str = ""


def _rule_price(reason: str, pos: Position, cfg: dict, idx: int | None, p: float, gap: bool) -> float:
    """체결가: 봉 시가에서 바로 넘은 경우(gap)는 그 시가, 봉 안에서 선을 지난 경우는 그 선."""
    if gap or pos.avg_price is None:
        return p
    x, avg = cfg["exit"], pos.avg_price
    if reason == "stop_loss":
        return avg * (1 + x["stop_loss_pct"] / 100)
    if idx is not None:
        return avg * (1 + x["take_profits"][idx][0] / 100)
    if reason == "trailing_stop":
        return pos.peak * (1 - x["trail_pct"] / 100)
    return p


def simulate(bars: list[dict], published_ms: int, cfg: dict, slot_usd: float, fee_rate: float,
             session_end_ms: int | None = None) -> SimResult:
    """bars: 시간순 [{start_ms, o, h, l, c, v}] (1분)."""
    e = cfg["entry"]
    ref = ref_from_bars(bars, published_ms, cfg)
    after = [b for b in bars if b["start_ms"] + 60_000 > published_ms]
    if ref is None:
        first = next((b for b in after if b["start_ms"] >= published_ms), None)
        ref = first["o"] if first else None
    if ref is None:
        return SimResult(False, why="no_ref")
    t_dec = published_ms + e["decision_delay_s"] * 1000
    t_end = published_ms + e["trigger_window_min"] * 60_000
    trigger = ref * (1 + e["trigger_rise_pct"] / 100)
    entry_px = entry_t = None
    idx0 = None
    for i, b in enumerate(bars):
        b_end = b["start_ms"] + 60_000
        if b_end <= t_dec or b["start_ms"] >= t_end:
            continue
        if b["start_ms"] >= t_dec and b["o"] >= trigger:      # 판단이 끝났을 때 이미 위
            entry_px, entry_t, idx0 = b["o"], b["start_ms"], i
            break
        if b["h"] >= trigger:
            entry_px, entry_t, idx0 = trigger, max(b["start_ms"], t_dec), i
            break
    if entry_px is None:
        return SimResult(False, ref, why="no_trigger")
    qty, limit = plan_entry(slot_usd, entry_px, cfg, fee_rate)
    if qty <= 0:
        return SimResult(False, ref, why="too_expensive")
    pos = Position("SIM", 0)
    pos.add(Fill("BUY", qty, limit, entry_t, "entry", qty * limit * fee_rate))
    hi = lo = limit
    reasons = []
    for b in bars[idx0:]:
        # 진입한 봉은 진입 뒤 순서를 모르므로 종가만 본다 (보수적)
        pts = [("c", b["c"])] if b is bars[idx0] else [("o", b["o"]), ("l", b["l"]), ("h", b["h"]), ("c", b["c"])]
        for k, p in pts:
            offset = {"o": 0, "l": 20_000, "h": 40_000, "c": 59_000}[k]
            now = max(entry_t, b["start_ms"] + offset)
            if pos.qty <= 0:
                break
            hi, lo = max(hi, p), min(lo, p)
            update_peak(pos, p)
            guard = 0
            while pos.qty > 0 and guard < 10:
                guard += 1
                ex = exit_decision(pos, p, now, session_end_ms, cfg)
                if ex is None:
                    break
                px = _rule_price(ex.reason, pos, cfg, ex.tp_index, p, gap=(k == "o"))
                pos.add(Fill("SELL", ex.qty, px, now, ex.reason, ex.qty * px * fee_rate))
                apply_exit(pos, ex)
                reasons.append(ex.reason)
        if pos.qty <= 0:
            break
    if pos.qty > 0:
        last = bars[-1]
        pos.add(Fill("SELL", pos.qty, last["c"], last["start_ms"] + 59_000, "data_end", pos.qty * last["c"] * fee_rate))
        reasons.append("data_end")
    pnl = pos.realized_pnl()
    return SimResult(True, ref, entry_t, round(limit, 6), pos.fills[-1].t_ms, round(pnl, 4),
                     round(100 * pnl / pos.cost, 4) if pos.cost else 0.0,
                     round(100 * (hi / limit - 1), 4), round(100 * (lo / limit - 1), 4),
                     "|".join(reasons), pnl > 0)
