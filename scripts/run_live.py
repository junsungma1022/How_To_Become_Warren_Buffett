#!/usr/bin/env python3
"""매매 엔진 실행.

    python scripts/run_live.py --mode shadow     # 주문 없이 전 과정 점검 (기본)
    python scripts/run_live.py --mode live       # 실제 주문 — settings.yaml 의 mode.live_enabled: true 도 필요

맥에서 돌린다 (토스 허용 IP = 이 맥의 공인 IP). 잠자기 방지: `caffeinate -i python scripts/run_live.py --mode live`
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.config import load_dotenv_file, load_secrets, load_settings  # noqa: E402
from hbwb.engine.runner import Engine  # noqa: E402


async def main(mode: str) -> int:
    load_dotenv_file()
    eng = Engine(load_settings(), load_secrets(), mode)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, eng.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    try:
        await eng.run()
    finally:
        await eng.aclose()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    args = ap.parse_args()
    if args.mode == "live":
        print("⚠️  live 모드: 실제 주문이 나간다. 10초 안에 Ctrl+C 로 취소할 수 있다.", flush=True)
        try:
            import time
            time.sleep(10)
        except KeyboardInterrupt:
            sys.exit(1)
    sys.exit(asyncio.run(main(args.mode)))
