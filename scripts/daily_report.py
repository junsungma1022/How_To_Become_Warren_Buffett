#!/usr/bin/env python3
"""매일 KST 09:00 하루 손익 보고서 → Discord #daily_summary.

    python scripts/daily_report.py                # 오늘 09:00 에 끝난 하루
    python scripts/daily_report.py --dry-run      # 보내지 않고 화면에만
    python scripts/daily_report.py --date 2026-11-20

매일 자동 실행: `bash scripts/install_launchd.sh` (맥 launchd · 09:00)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.broker.toss import TossClient  # noqa: E402
from hbwb.clock import KST  # noqa: E402
from hbwb.config import ROOT, load_dotenv_file, load_secrets, load_settings, scrub  # noqa: E402
from hbwb.eventlog import EventLog, read_events  # noqa: E402
from hbwb.notify.discord import Discord  # noqa: E402
from hbwb.report.daily import read_csv, render, summarize, window_ending  # noqa: E402


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
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(KST).date()
    w = window_ending(day, int(cfg["report"]["daily_time_kst"].split(":")[0]))
    s = summarize(w, read_csv(data / "ledger" / "articles.csv"), read_csv(data / "ledger" / "trades.csv"), read_events(logs))
    modes = {t.get("mode") for t in s["trades"]} or {"live" if cfg["mode"]["live_enabled"] else "shadow"}
    equity = None
    try:
        equity = await account_equity(sec)
    except Exception as e:  # noqa: BLE001 - 계좌 조회가 안 돼도 보고서는 보낸다
        print("계좌 조회 실패:", scrub(e, sec.all_values()))
    text = render(s, "/".join(sorted(m for m in modes if m)), equity, cfg["capital"]["budget_usd"])
    print(text)
    if args.dry_run:
        return 0
    ev = EventLog(logs, sec.all_values(), echo=False)
    d = Discord(sec, ev.write, prefix="HBWB")
    ok = await d.send("daily_summary", text)
    await d.aclose()
    ev.write("daily_report", ok=ok, window=w.label, realized=s["realized"], trades=len(s["trades"]))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date")
    sys.exit(asyncio.run(main(ap.parse_args())))
