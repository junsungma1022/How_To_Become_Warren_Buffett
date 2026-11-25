"""매일 KST 10:00 하루 손익 보고서 (전날 10:00 ~ 오늘 10:00 · 미국 프리~애프터 한 바퀴가 이 안에 들어간다).

경계 시각은 settings.yaml report.daily_time_kst (2026-11-23 09:00 → 10:00: 겨울엔 토스 애프터가 KST 09:50 에 끝난다).
"""

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


def window_ending(day_kst: dt.date, hour: int = 10) -> Window:
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


def cumulative(trades: list[dict]) -> dict:
    """시작부터 지금까지 (끝난 거래 전부)."""
    done = [t for t in trades if t.get("exit_utc")]
    pnl = [_f(t.get("pnl_usd")) for t in done]
    days = {str(t.get("exit_utc"))[:10] for t in done}
    return {"trades": len(done), "realized": sum(pnl), "wins": sum(p > 0 for p in pnl), "days": len(days),
            "fees": sum(_f(t.get("fees_usd")) for t in done)}


def heartbeat_status(hb: dict | None, now_ms: int, stale_min: float = 15) -> tuple[bool, str]:
    """(정상인가, 한 줄). 엔진은 5분마다 heartbeat.json 을 쓴다."""
    if not hb or not hb.get("t"):
        return False, "⚠️ 엔진 상태: 기록 없음 (한 번도 안 켜졌거나 data/state 가 없다)"
    age = (now_ms - int(hb["t"])) / 60000
    pos = hb.get("positions") or {}
    held = ", ".join(f"{k} {v}주" for k, v in pos.items()) or "없음"
    up = (now_ms - int(hb.get("started_ms") or hb["t"])) / 3_600_000
    if age > stale_min:
        return False, f"🔴 엔진 멈춤 의심: 마지막 신호 {age:.0f}분 전 · 보유 {held}"
    halted = f" · 🔴 정지 상태: {hb['halted']}" if hb.get("halted") else ""
    return True, f"엔진 정상 ({hb.get('mode')}) · 마지막 신호 {age:.0f}분 전 · 이번 실행 {up:.1f}시간째 · 보유 {held}{halted}"


def render(s: dict, mode: str, equity: dict | None, start_capital: float,
           cum: dict | None = None, engine_line: str | None = None) -> str:
    lines = [f"📊 **일일 손익 보고서** ({s['window']}) · 모드 {mode}"]
    if engine_line:
        lines.append(engine_line)
    if equity:
        tot = equity["cash"] + equity["holdings"]
        lines.append(f"{'가상 계좌' if mode == 'paper' else '계좌'}(USD): 현금 ${equity['cash']:.2f} · 보유 평가 ${equity['holdings']:.2f} · 합계 **${tot:.2f}** "
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
    if cum and cum["trades"]:
        rate = 100 * cum["wins"] / cum["trades"]
        lines.append(f"누적 (시작부터): 실현 ${cum['realized']:+.2f} · 거래 {cum['trades']}건 · 승률 {rate:.0f}% · "
                     f"거래한 날 {cum['days']}일 · 수수료 ${cum['fees']:.2f}")
    at = s["window"][-9:-4] if str(s["window"]).endswith(" KST") else ""
    lines.append(f"하루 손실 한도 · 연패 카운터는 지금{f'({at})' if at else ''} 리셋됐다.")
    return "\n".join(lines)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def history(trades: list[dict], day_key_of) -> list[dict]:
    """보고 기준 하루(KST 10:00 시작)마다 [날, 거래 수, 승, 실현, 수수료] — 마지막 결과 보고용."""
    days: dict[str, dict] = {}
    for t in trades:
        ms = parse_iso_ms(t.get("exit_utc")) if t.get("exit_utc") else None
        if ms is None:
            continue
        d = days.setdefault(day_key_of(ms), {"day": day_key_of(ms), "trades": 0, "wins": 0, "realized": 0.0, "fees": 0.0})
        p = _f(t.get("pnl_usd"))
        d["trades"] += 1
        d["wins"] += p > 0
        d["realized"] += p
        d["fees"] += _f(t.get("fees_usd"))
    out, run = [], 0.0
    for k in sorted(days):
        run += days[k]["realized"]
        out.append({**days[k], "cumulative": run})
    return out


def render_history(rows: list[dict], start_capital: float) -> str:
    lines = ["📈 **전체 기간 결과** (하루 = KST 10:00 시작)", "날짜 | 거래 | 승 | 실현 | 누적 | 누적 %"]
    for r in rows:
        lines.append(f"{r['day']} | {r['trades']} | {r['wins']} | ${r['realized']:+.2f} | ${r['cumulative']:+.2f} | "
                     f"{100 * r['cumulative'] / start_capital:+.1f}%")
    if not rows:
        lines.append("(끝난 거래 없음)")
    return "\n".join(lines)
