"""매일 KST 09:00 하루 손익 보고서 (전날 09:00 ~ 오늘 09:00 · 미국 프리~애프터 한 바퀴가 이 안에 들어간다)."""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from ..clock import KST, parse_iso_ms


@dataclass
class Window:
    start_ms: int
    end_ms: int
    label: str


def window_ending(day_kst: dt.date, hour: int = 9) -> Window:
    end = dt.datetime.combine(day_kst, dt.time(hour, 0), KST)
    start = end - dt.timedelta(days=1)
    return Window(int(start.timestamp() * 1000), int(end.timestamp() * 1000),
                  f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M} KST")


def _in(w: Window, iso: str | None) -> bool:
    t = parse_iso_ms(iso) if iso else None
    return t is not None and w.start_ms <= t < w.end_ms


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def summarize(w: Window, articles: list[dict], trades: list[dict], events: list[dict]) -> dict:
    arts = [a for a in articles if _in(w, a.get("received_utc"))]
    trs = [t for t in trades if _in(w, t.get("exit_utc"))]
    pnl = [_f(t.get("pnl_usd")) for t in trs]
    ev = [e for e in events if w.start_ms <= int(e.get("t") or 0) < w.end_ms]
    return {
        "window": w.label,
        "alerts": sum(1 for e in ev if e.get("kind") == "alert"),
        "articles": len(arts),
        "universe_ok": sum(a.get("universe_ok") == "true" for a in arts),
        "jev_pass": sum(a.get("jev_pass") == "true" for a in arts),
        "claude_called": sum(a.get("claude_called") == "true" for a in arts),
        "claude_buy": sum(a.get("claude_decision") == "BUY" for a in arts),
        "filled": sum(a.get("action") == "buy" for a in arts),
        "ai_cost": sum(_f(a.get("ai_cost_usd")) for a in arts),
        "trades": trs,
        "realized": sum(pnl),
        "wins": sum(p > 0 for p in pnl),
        "losses": sum(p <= 0 for p in pnl),
        "fees": sum(_f(t.get("fees_usd")) for t in trs),
        "warnings": sum(e.get("kind") in {"pipeline_error", "manage_error", "order_error", "jev_error", "claude_error",
                                          "rtpr_disconnect", "notify_fail"} for e in ev),
    }


def render(s: dict, mode: str, equity: dict | None, start_capital: float) -> str:
    lines = [f"📊 **일일 손익 보고서** ({s['window']}) · 모드 {mode}"]
    if equity:
        tot = equity["cash"] + equity["holdings"]
        lines.append(f"계좌(USD): 현금 ${equity['cash']:.2f} · 보유 평가 ${equity['holdings']:.2f} · 합계 **${tot:.2f}** "
                     f"(시작 ${start_capital:.0f} 대비 {tot - start_capital:+.2f}, {100 * (tot / start_capital - 1):+.1f}%)")
    n = len(s["trades"])
    rate = f"{100 * s['wins'] / n:.0f}%" if n else "-"
    lines.append(f"실현 손익: **${s['realized']:+.2f}** · 거래 {n}건 (승 {s['wins']} / 패 {s['losses']} · 승률 {rate}) · 수수료 ${s['fees']:.2f}")
    for t in s["trades"]:
        lines.append(f"  · {t.get('ticker')}: ${_f(t.get('pnl_usd')):+.2f} ({_f(t.get('ret_pct')):+.1f}%) · "
                     f"{t.get('exit_reasons')} · {_f(t.get('hold_min')):.0f}분 · Jev {t.get('jev_choice')} · Claude {t.get('claude_grade')}/5")
    lines.append(f"기사 깔때기: 알림 {s['alerts']} → 기록 {s['articles']} → 종목범위 통과 {s['universe_ok']} → Jev 통과 {s['jev_pass']} "
                 f"→ Claude 판단 {s['claude_called']} → 매수 결정 {s['claude_buy']} → 체결 {s['filled']}")
    lines.append(f"AI 비용(OpenRouter): ${s['ai_cost']:.3f} · 경고·오류 기록 {s['warnings']}건")
    return "\n".join(lines)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
