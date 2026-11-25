"""대시보드 · 실시간 보기가 쓰는 '지금 상태' 묶음 (읽기만 · D-17).

원천: data/state/heartbeat.json · positions.json · data/ledger/articles.csv · trades.csv · logs/events-*.jsonl.
엔진과 같은 파일을 읽기만 한다 — 주문 · 설정을 바꾸는 길은 없다.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path

from ..clock import KST, ms_to, parse_iso_ms
from ..config import mode_cfg, normalize_mode
from .daily import read_csv
from .labels import JEV, exits_ko, reason_ko, universe_ko

HB_STALE_MS = 15 * 60_000


def window_start_ms(now_ms: int, hour: int) -> int:
    """지금이 속한 보고 하루의 시작 (KST hour:00)."""
    t = ms_to(now_ms, KST)
    start = t.replace(hour=hour, minute=0, second=0, microsecond=0)
    if t < start:
        start -= dt.timedelta(days=1)
    return int(start.timestamp() * 1000)


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _kst(ms: int | None, fmt: str = "%m-%d %H:%M:%S") -> str:
    return ms_to(ms, KST).strftime(fmt) if ms else ""


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def recent_events(log_dir: Path, since_ms: int, limit: int = 4000) -> list[dict]:
    """since_ms 뒤 이벤트 (오늘 · 어제 파일만 읽는다)."""
    out: list[dict] = []
    files = sorted(Path(log_dir).glob("events-*.jsonl"))[-2:]
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if int(e.get("t") or 0) >= since_ms:
                        out.append(e)
        except OSError:
            continue
    return out[-limit:]


def build_state(root: Path, cfg: dict, now_ms: int, key_info: dict | None = None) -> dict:
    data = root / cfg["paths"]["data_dir"]
    logs = root / cfg["paths"]["log_dir"]
    hour = int(str(cfg["report"]["daily_time_kst"]).split(":")[0])
    w0 = window_start_ms(now_ms, hour)

    hb = _read_json(data / "state" / "heartbeat.json") or {}
    mode = normalize_mode(hb.get("mode") or ("live" if cfg["mode"]["live_enabled"] else "paper"))
    start_usd = float(mode_cfg(cfg, mode)["capital"]["budget_usd"])
    hb_age = now_ms - int(hb["t"]) if hb.get("t") else None
    engine = {
        "mode": mode,
        "alive": hb_age is not None and hb_age < HB_STALE_MS,
        "heartbeat_age_s": round(hb_age / 1000) if hb_age is not None else None,
        "started": _kst(hb.get("started_ms")),
        "last_alert": _kst(hb.get("last_alert_ms")),
        "last_alert_age_s": round((now_ms - hb["last_alert_ms"]) / 1000) if hb.get("last_alert_ms") else None,
        "halted": hb.get("halted"),
        "window_start": _kst(w0, "%m-%d %H:%M"),
    }

    trades = read_csv(data / "ledger" / "trades.csv")
    mine = [t for t in trades if normalize_mode(t.get("mode") or "") == mode]
    realized = sum(_f(t.get("pnl_usd")) for t in mine)

    # 보유
    marks = hb.get("marks") or {}
    positions = []
    st = _read_json(data / "state" / "positions.json") or {}
    cost_open = 0.0
    for sym, rec in st.items():
        p = rec.get("pos") or {}
        fills = p.get("fills") or []
        buys = [f for f in fills if f.get("side") == "BUY"]
        sells = [f for f in fills if f.get("side") == "SELL"]
        bq = sum(_f(f.get("qty")) for f in buys)
        qty = bq - sum(_f(f.get("qty")) for f in sells)
        if qty <= 0:
            continue
        avg = sum(_f(f.get("qty")) * _f(f.get("price")) for f in buys) / bq if bq else 0.0
        cost_open += sum(_f(f.get("qty")) * _f(f.get("price")) + _f(f.get("fee")) for f in buys) \
            - sum(_f(f.get("qty")) * _f(f.get("price")) - _f(f.get("fee")) for f in sells)
        mark = _f(marks.get(sym)) or None
        positions.append({
            "symbol": sym, "slot": int(p.get("slot", 0)) + 1, "qty": qty, "avg": round(avg, 4),
            "mark": mark, "pnl_pct": round(100 * (mark / avg - 1), 2) if mark and avg else None,
            "peak_pct": round(100 * (_f(p.get("peak")) / avg - 1), 2) if avg and p.get("peak") else None,
            "since": _kst(p.get("entry_ms"), "%m-%d %H:%M"),
            "tp_done": len(p.get("tp_done") or []), "trailing": bool(p.get("trailing")),
        })

    # 계좌
    snap = hb.get("paper") if mode == "paper" else None
    if snap:
        cash, equity = _f(snap.get("cash_usd")), _f(snap.get("equity_usd"))
    else:
        cash = start_usd + realized - cost_open if mode == "paper" else None
        equity = (cash + sum((p["mark"] or p["avg"]) * p["qty"] for p in positions)) if cash is not None else None
    account = {
        "start": start_usd, "cash": cash, "equity": equity, "realized": round(realized, 4),
        "ret_pct": round(100 * (equity / start_usd - 1), 2) if equity is not None else None,
        "trades": len(mine), "wins": sum(_f(t.get("pnl_usd")) > 0 for t in mine),
        "source": "엔진 5분 기록" if snap else ("거래 기록으로 계산" if mode == "paper" else "실계좌 (토스 앱 확인)"),
    }

    # 오늘(보고 하루) 깔때기 · 이유
    arts = read_csv(data / "ledger" / "articles.csv")
    today = [a for a in arts if (parse_iso_ms(a.get("received_utc")) or 0) >= w0]
    ev = recent_events(logs, min(w0, now_ms - 24 * 3_600_000))
    alerts_today = sum(1 for e in ev if e.get("kind") == "alert" and int(e["t"]) >= w0)
    funnel = [
        ["알림", alerts_today],
        ["기사 처리", len(today)],
        ["종목 범위 통과", sum(a.get("universe_ok") == "true" for a in today)],
        ["Jev 호재", sum(a.get("jev_pass") == "true" for a in today)],
        ["Claude 판단", sum(a.get("claude_called") == "true" for a in today)],
        ["Claude 매수", sum((a.get("claude_decision") or "").upper() == "BUY" for a in today)],
        ["체결", sum(a.get("action") == "buy" for a in today)],
    ]
    reasons = Counter(reason_ko(a.get("action_reason")) for a in today if a.get("action") != "buy")
    uni = Counter(universe_ko(a.get("universe_reason")) for a in today if a.get("universe_ok") != "true")

    # AI 비용
    def cost_since(ms: int) -> float:
        return sum(_f(a.get("ai_cost_usd")) for a in arts if (parse_iso_ms(a.get("received_utc")) or 0) >= ms)
    first = min((parse_iso_ms(a.get("received_utc")) or now_ms for a in arts), default=now_ms)
    span_h = max((now_ms - max(first, now_ms - 24 * 3_600_000)) / 3_600_000, 0.0)
    last24 = cost_since(now_ms - 24 * 3_600_000)
    per_day = last24 * 24 / span_h if span_h >= 1 else None
    ai = {
        "today": round(cost_since(w0), 4), "last24h": round(last24, 4),
        "total": round(sum(_f(a.get("ai_cost_usd")) for a in arts), 4),
        "per_day_est": round(per_day, 3) if per_day is not None else None,
        "basis_h": round(min(span_h, 24), 1),
        "claude_calls_today": funnel[4][1],
        "key": key_info,
    }

    feed = []
    for a in sorted(arts, key=lambda r: parse_iso_ms(r.get("received_utc")) or 0, reverse=True)[:40]:
        feed.append({
            "time": _kst(parse_iso_ms(a.get("received_utc")), "%H:%M:%S"), "ticker": a.get("ticker"),
            "title": (a.get("title") or "")[:140],
            "jev": JEV.get(a.get("jev_choice") or "", a.get("jev_choice") or ""),
            "claude": a.get("claude_decision") or "",
            "rise": a.get("signal_rise_pct") or a.get("over_cap_max_pct") or "",
            "result": "매수" if a.get("action") == "buy" else (
                universe_ko(a.get("universe_reason")) if a.get("action_reason") == "universe" else reason_ko(a.get("action_reason"))),
            "buy": a.get("action") == "buy",
        })
    trade_rows = [{
        "ticker": t.get("ticker"), "slot": t.get("slot"),
        "entry": _kst(parse_iso_ms(t.get("entry_utc")), "%m-%d %H:%M"),
        "exit": _kst(parse_iso_ms(t.get("exit_utc")), "%m-%d %H:%M"),
        "ret_pct": _f(t.get("ret_pct")), "pnl": _f(t.get("pnl_usd")),
        "why": exits_ko(t.get("exit_reasons")), "hold_min": t.get("hold_min"),
    } for t in reversed(mine[-30:])]

    return {"now": _kst(now_ms), "engine": engine, "account": account, "positions": positions,
            "funnel": funnel, "reasons": reasons.most_common(8), "universe": uni.most_common(6),
            "ai": ai, "articles": feed, "trades": trade_rows}
