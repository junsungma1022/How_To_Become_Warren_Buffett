#!/usr/bin/env python3
"""매매 엔진 실행.

    python scripts/run_live.py --mode paper      # 가상 계좌 $1000 (주문 없음 · 기본 · 예전 이름 shadow)
    python scripts/run_live.py --mode live       # 실제 주문 — settings.yaml 의 mode.live_enabled: true 도 필요

맥에서 돌린다 (토스 허용 IP = 이 맥의 공인 IP).
24/7 무인 운영은 `bash scripts/install_launchd.sh --engine live` (죽으면 자동 재시작 · 잠자기 방지 · 로그인 때 시작).

종료 코드: 0 = 정상 종료 또는 사람이 고쳐야 하는 문제 (키 · 구독 · 허용 IP — launchd 가 다시 켜지 않는다)
           1 = 그 밖의 오류 (launchd 가 60초 뒤 다시 켠다)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.config import MissingSecret, load_dotenv_file, load_secrets, load_settings  # noqa: E402
from hbwb.engine.runner import Engine, EngineFatal  # noqa: E402


async def main(mode: str, quiet: bool) -> int:
    load_dotenv_file()
    try:
        eng = Engine(load_settings(), load_secrets(), mode)
    except (MissingSecret, ValueError, SystemExit) as e:
        # 키 없음 · 설정 오류 · live 이중 확인 — 다시 켜도 같다 → 0 으로 끝내 launchd 가 반복 재시작하지 않게
        print(f"🔴 시작 안 함: {e}", flush=True)
        return 0
    eng.ev.echo = not quiet
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, eng.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    code = 0
    try:
        await eng.run()
    except EngineFatal as e:
        eng.ev.write("engine_fatal", err=str(e)[:300])
        code = 0                      # 다시 켜도 같은 문제 — 사람이 고친다
    except Exception as e:  # noqa: BLE001
        eng.ev.write("engine_crash", err=f"{type(e).__name__}: {e}"[:300])
        code = 1                      # launchd 가 다시 켠다
    finally:
        await eng.aclose()
    return code


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["paper", "shadow", "live"], default="paper")
    ap.add_argument("--quiet", action="store_true", help="화면에 이벤트를 찍지 않는다 (launchd 용 · 기록 파일에는 남는다)")
    ap.add_argument("--no-delay", action="store_true", help="live 시작 전 10초 대기 생략 (launchd 용)")
    args = ap.parse_args()
    if args.mode == "live" and not args.no_delay:
        print("⚠️  live 모드: 실제 주문이 나간다. 10초 안에 Ctrl+C 로 취소할 수 있다.", flush=True)
        try:
            import time
            time.sleep(10)
        except KeyboardInterrupt:
            sys.exit(1)
    sys.exit(asyncio.run(main(args.mode, args.quiet)))
