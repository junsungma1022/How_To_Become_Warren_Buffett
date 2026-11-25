#!/usr/bin/env python3
"""매일 KST 10:00 하루 손익 보고서 → Discord #daily_summary.

    python scripts/daily_report.py                # 오늘 10:00 에 끝난 하루
    python scripts/daily_report.py --dry-run      # 보내지 않고 화면에만
    python scripts/daily_report.py --date 2026-11-20
    python scripts/daily_report.py --history      # 시작부터 날마다 표 (마지막 결과 보고용 · --dry-run 이면 화면만)

매일 자동 실행: `bash scripts/install_launchd.sh` (맥 launchd · 10:00)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.broker.toss import TossClient  # noqa: E402
from hbwb.clock import KST, now_ms  # noqa: E402
from hbwb.config import ROOT, day_hour, load_dotenv_file, load_secrets, load_settings, mode_cfg, normalize_mode, scrub  # noqa: E402
from hbwb.eventlog import EventLog, read_events  # noqa: E402
from hbwb.notify.discord import Discord  # noqa: E402
from hbwb.engine.runner import report_day_key  # noqa: E402
from hbwb.report.daily import (cumulative, heartbeat_status, history, read_csv, render, render_history,  # noqa: E402
                               summarize, window_ending)


def current_window_day(now_kst: dt.datetime, hour: int) -> dt.date:
    """지금이 속한 보고 하루가 끝나는 날 (10:00 이후면 내일 · 전이면 오늘)."""
    return now_kst.date() + dt.timedelta(days=1) if now_kst.hour >= hour else now_kst.date()


def start_capital(cfg: dict, trades: list[dict], modes: set[str] | None = None) -> float:
    """시작 자금: paper 기록뿐이면 capital.paper_start_usd ($1000), 아니면 live 예산 (D-16)."""
    modes = modes or ({normalize_mode(t.get("mode") or "") for t in trades} - {""})
    return float(mode_cfg(cfg, "paper" if modes == {"paper"} else "live")["capital"]["budget_usd"])


async def account_equity(sec) -> dict | None:
    toss = TossClient(sec.get("toss_client_id"), sec.get("toss_client_secret"))
    try:
        cash = await toss.buying_power_usd()
        h = await toss.holdings()
        mv = 0.0
        for it in (h or {}).get("items") or []:
            if str(it.get("currency")) == "USD":
                mv += float(it.get("lastPrice") or 0) * float(it.get("quantity") or 0)
        return {"cash": cash, "holdings": mv}
    finally:
        await toss.aclose()


async def main(args) -> int:
    load_dotenv_file()
    cfg, sec = load_settings(), load_secrets()
    data, logs = ROOT / cfg["paths"]["data_dir"], ROOT / cfg["paths"]["log_dir"]
    day = (dt.date.fromisoformat(args.date) if args.date
           else current_window_day(dt.datetime.now(KST), day_hour(cfg)) if args.current
           else dt.datetime.now(KST).date())
    w = window_ending(day, day_hour(cfg))
    all_trades = read_csv(data / "ledger" / "trades.csv")
    if args.history:
        text = render_history(history(all_trades, partial(report_day_key, hour=day_hour(cfg))), start_capital(cfg, all_trades))
        print(text)
        if not args.dry_run:
            d = Discord(sec, EventLog(logs, sec.all_values(), echo=False).write, prefix="HBWB")
            ok = await d.send("daily_summary", text)
            await d.aclose()
            return 0 if ok else 1
        return 0
    s = summarize(w, read_csv(data / "ledger" / "articles.csv"), all_trades, read_events(logs))
    hb_path = data / "state" / "heartbeat.json"
    hb = json.loads(hb_path.read_text(encoding="utf-8")) if hb_path.exists() else None
    alive, engine_line = heartbeat_status(hb, now_ms())
    modes = {normalize_mode(t.get("mode") or "") for t in s["trades"]} - {""}
    if not modes:
        modes = {normalize_mode((hb or {}).get("mode") or ("live" if cfg["mode"]["live_enabled"] else "paper"))}
    equity = None
    if modes == {"paper"}:
        p = (hb or {}).get("paper")           # 엔진이 5분마다 남기는 가상 계좌 (현금 · 평가)
        if p:
            equity = {"cash": float(p["cash_usd"]), "holdings": float(p["equity_usd"]) - float(p["cash_usd"])}
    else:
        try:
            equity = await account_equity(sec)
        except Exception as e:  # noqa: BLE001 - 계좌 조회가 안 돼도 보고서는 보낸다
            print("계좌 조회 실패:", scrub(e, sec.all_values()))
    text = render(s, "/".join(sorted(modes)), equity, start_capital(cfg, all_trades, modes),
                  cumulative(all_trades), engine_line)
    print(text)
    if args.dry_run:
        return 0
    ev = EventLog(logs, sec.all_values(), echo=False)
    d = Discord(sec, ev.write, prefix="HBWB")
    ok = await d.send("daily_summary", text)
    if not alive:
        await d.send("critical", engine_line + " — `launchctl list | grep hbwb` · logs/engine.err.log 확인")
    await d.aclose()
    ev.write("daily_report", ok=ok, window=w.label, realized=s["realized"], trades=len(s["trades"]))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--current", action="store_true", help="지금 진행 중인 하루 (10:00 부터 지금까지)")
    ap.add_argument("--history", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args())))
