#!/usr/bin/env python3
"""엔진이 하는 일을 터미널에서 한 줄씩 본다 (D-17). 날짜가 바뀌면 새 로그 파일로 저절로 넘어간다.

    .venv/bin/python scripts/watch.py          # 최근 20줄부터 이어서 · Ctrl+C 로 끈다 (엔진에는 영향 없음)
    .venv/bin/python scripts/watch.py --all    # 하트비트 같은 잡다한 줄까지
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.clock import KST, ms_to  # noqa: E402
from hbwb.config import ROOT, load_settings  # noqa: E402
from hbwb.report.labels import JEV, exits_ko, reason_ko, universe_ko  # noqa: E402

QUIET = {"heartbeat", "rtpr_other", "paper_equity_error"}


def fmt(e: dict) -> str:
    """이벤트 한 줄 → 사람이 읽는 한 줄."""
    t = ms_to(int(e.get("t") or 0), KST).strftime("%H:%M:%S")
    k = e.get("kind", "")
    tk = e.get("ticker") or e.get("symbol") or ""
    if k == "alert":
        return f"{t} 📰 기사 {tk} (발행 {e.get('age_s', '?')}초 전)"
    if k == "article":
        r = e.get("action_reason")
        why = universe_ko(e.get("universe_reason")) if r == "universe" else reason_ko(r)
        extra = " · ".join(x for x in (JEV.get(e.get("jev_choice") or "", ""), e.get("claude_decision") or "") if x)
        mark = "🟢 매수" if e.get("action") == "buy" else "⚪ 넘김"
        return f"{t} {mark} {tk} — {why}" + (f" ({extra})" if extra else "")
    if k == "shadow_fill":
        return f"{t} 💵 가상 체결 {e.get('side')} {tk} {e.get('qty')}주 @ ${e.get('price')} ({exits_ko(e.get('reason')) or e.get('reason')})"
    if k == "claude":
        return f"{t} 🤖 Claude {tk}: {e.get('decision')} (등급 {e.get('grade')} · 확신 {e.get('conf')}) {str(e.get('catalyst') or '')}"
    if k == "trade_closed":
        pnl = float(e.get("pnl_usd") or 0)
        return (f"{t} {'✅' if pnl > 0 else '❌'} 거래 종료 {tk} 손익 ${pnl:+.2f} ({float(e.get('ret_pct') or 0):+.2f}%) · "
                f"{exits_ko(e.get('exit_reasons'))} · 보유 {e.get('hold_min')}분")
    if k == "shadow_no_fill":
        return f"{t} ⚠️ 가상 미체결 {e.get('side')} {tk}"
    if k == "over_cap":
        return f"{t} ⏸️ {tk} +{e.get('rise_pct')}% — 상한 +{e.get('cap_pct')}% 위라 대기"
    if k in {"rtpr_connect", "rtpr_disconnect", "rtpr_fatal"}:
        return f"{t} 🔌 RTPR {'연결' if k == 'rtpr_connect' else '끊김' if k == 'rtpr_disconnect' else '중단'} {e.get('err') or e.get('why') or ''}"
    rest = {x: y for x, y in e.items() if x not in ("t", "kind")}
    return f"{t} · {k} {json.dumps(rest, ensure_ascii=False)[:160]}"


def newest(log_dir: Path) -> Path | None:
    files = sorted(log_dir.glob("events-*.jsonl"))
    return files[-1] if files else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-n", type=int, default=20)
    args = ap.parse_args()
    log_dir = ROOT / load_settings()["paths"]["log_dir"]
    path, fh = None, None
    print("실시간 보기 — Ctrl+C 로 끈다 (엔진은 계속 돈다)", flush=True)
    try:
        while True:
            cur = newest(log_dir)
            if cur != path and cur is not None:
                if fh:
                    fh.close()
                path, fh = cur, open(cur, encoding="utf-8")
                lines = fh.readlines()
                for line in lines[-args.n:] if args.n else []:
                    _show(line, args.all)
            line = fh.readline() if fh else ""
            if line:
                _show(line, args.all)
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def _show(line: str, show_all: bool) -> None:
    try:
        e = json.loads(line)
    except ValueError:
        return
    if not show_all and e.get("kind") in QUIET:
        return
    print(fmt(e), flush=True)


if __name__ == "__main__":
    sys.exit(main())
