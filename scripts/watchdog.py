#!/usr/bin/env python3
"""엔진 감시 — launchd 가 10분마다 부른다. 엔진의 heartbeat.json 이 15분 넘게 낡았으면 Discord #critical.

launchd 의 자동 재시작은 프로세스가 '죽었을' 때만 돈다. 살아 있는데 멈춘 경우(멈춤 · 맥 잠자기 뒤)를 여기서 잡는다.
같은 경고는 60분에 한 번만, 다시 살아나면 한 번 알린다.

    .venv/bin/python scripts/watchdog.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.clock import now_ms  # noqa: E402
from hbwb.config import ROOT, load_dotenv_file, load_secrets, load_settings  # noqa: E402
from hbwb.eventlog import EventLog  # noqa: E402
from hbwb.notify.discord import Discord  # noqa: E402
from hbwb.report.daily import heartbeat_status  # noqa: E402

REPEAT_MS = 60 * 60_000


async def main() -> int:
    load_dotenv_file()
    cfg, sec = load_settings(), load_secrets()
    state_dir = ROOT / cfg["paths"]["data_dir"] / "state"
    hb_path, wd_path = state_dir / "heartbeat.json", state_dir / "watchdog.json"
    hb = json.loads(hb_path.read_text(encoding="utf-8")) if hb_path.exists() else None
    wd = json.loads(wd_path.read_text(encoding="utf-8")) if wd_path.exists() else {"alerted_ms": 0, "down": False}
    alive, line = heartbeat_status(hb, now_ms())
    if hb is None:
        return 0                      # 엔진을 아직 한 번도 안 켰다 — 알릴 것 없음
    ev = EventLog(ROOT / cfg["paths"]["log_dir"], sec.all_values(), echo=False)
    d = Discord(sec, ev.write, prefix="HBWB·감시")
    try:
        if not alive and now_ms() - wd["alerted_ms"] >= REPEAT_MS:
            await d.send("critical", line + " — 맥이 잠들었거나 엔진이 멈췄다 · `launchctl list | grep hbwb` 확인")
            wd = {"alerted_ms": now_ms(), "down": True}
        elif alive and wd.get("down"):
            await d.send("warnings", "✅ 엔진 신호 다시 들어옴 · " + line)
            wd = {"alerted_ms": 0, "down": False}
    finally:
        await d.aclose()
    state_dir.mkdir(parents=True, exist_ok=True)
    wd_path.write_text(json.dumps(wd), encoding="utf-8")
    ev.write("watchdog", alive=alive, line=line)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
