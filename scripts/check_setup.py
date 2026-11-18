#!/usr/bin/env python3
"""실거래 전 점검 — 키가 채워졌는지(값은 보지 않는다), 각 API 가 실제로 응답하는지.

    python scripts/check_setup.py              # 전부
    python scripts/check_setup.py --no-ai      # AI 호출(소액 과금) 빼고
    python scripts/check_setup.py --discord    # 다섯 채널에 시험 메시지

주문은 보내지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.ai import jev as jev_mod  # noqa: E402
from hbwb.ai.openrouter import OpenRouter  # noqa: E402
from hbwb.broker.toss import TossClient  # noqa: E402
from hbwb.clock import fmt_kst  # noqa: E402
from hbwb.config import ENV_PATH, MissingSecret, load_dotenv_file, load_secrets, load_settings, scrub  # noqa: E402
from hbwb.engine import sessions as S  # noqa: E402
from hbwb.notify.discord import CHANNELS, Discord  # noqa: E402

OK, BAD = "✅", "❌"


async def main(args) -> int:
    load_dotenv_file()
    cfg, sec = load_settings(), load_secrets()
    fails = 0
    print(f"• .env 파일: {'있음' if ENV_PATH.exists() else '없음'}")
    if ENV_PATH.exists() and (ENV_PATH.stat().st_mode & 0o077):
        print(f"  {BAD} .env 권한이 너무 넓다 → chmod 600 .env")
        fails += 1
    for name, present in sec.present().items():
        print(f"  {OK if present else BAD} {name}")
    secrets = sec.all_values()

    # 토스
    try:
        toss = TossClient(sec.get("toss_client_id"), sec.get("toss_client_secret"))
        await toss.token()
        print(f"{OK} 토스 토큰")
        await toss.account_seq()
        print(f"{OK} 토스 종합매매 계좌 확인 (계좌번호는 출력 · 저장하지 않음)")
        bp = await toss.buying_power_usd()
        print(f"{OK if bp >= 1 else BAD} USD 매수가능금액 ${bp:.2f}" + ("" if bp >= 1 else " — 앱에서 달러 환전 필요"))
        fails += bp < 1
        rate = await toss.commission_rate_us()
        print(f"{OK} 미국 주식 수수료율 {rate}")
        cal = await toss.us_calendar()
        for n, a, b in S.windows_from_calendar(cal):
            print(f"   세션 {n:8s} {fmt_kst(a)} ~ {fmt_kst(b)}  {'(매매)' if S.allowed(n, cfg) else ''}")
        q = await toss.quote("AAPL")
        print(f"{OK} 시세 AAPL bid {q.bid} ask {q.ask}")
        info = (await toss.stocks(["AAPL"]) or [{}])[0]
        has_shares = bool(info.get("sharesOutstanding")) and float(info.get("sharesOutstanding") or 0) > 0
        print(f"{OK if has_shares else BAD} 종목정보 sharesOutstanding (시가총액 계산에 필요): {info.get('sharesOutstanding')}")
        fails += not has_shares
        await toss.aclose()
    except MissingSecret as e:
        print(f"{BAD} {e}")
        fails += 1
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} 토스: {scrub(e, secrets)}  (ip-not-allowed 면 WTS 허용 IP 에 이 맥의 공인 IP 등록)")
        fails += 1

    # OpenRouter (Jev 한 번 · 약 $0.0001)
    if not args.no_ai:
        try:
            ai = OpenRouter(sec.get("openrouter_key"))
            st = jev_mod.build_state("TEST", "Company receives FDA approval for lead drug",
                                     "The FDA approved the company's lead drug for adults.", 50_000_000, 2000)
            jr = await jev_mod.classify(ai, cfg["jev"]["model"], st)
            print(f"{OK} Jev ({cfg['jev']['model']}): {jr.choice} conf={jr.confidence} cost=${jr.usage.cost_usd:.5f}")
            text, u = await ai.chat(cfg["claude"]["model"], [{"role": "user", "content": "Reply with the single word OK."}],
                                    max_tokens=20)
            print(f"{OK} Claude ({cfg['claude']['model']}): {text.strip()[:20]!r} cost=${u.cost_usd:.5f}")
            await ai.aclose()
        except Exception as e:  # noqa: BLE001
            print(f"{BAD} OpenRouter: {scrub(e, secrets)}")
            fails += 1

    # RTPR 키 (기사 본문 하나는 알림이 와야 받을 수 있어 여기서는 키 존재만)
    try:
        sec.get("rtpr_key")
        print(f"{OK} RTPR 키 있음 (연결은 run_live --mode shadow 로 확인)")
    except MissingSecret as e:
        print(f"{BAD} {e}")
        fails += 1

    if args.discord:
        logs = []
        d = Discord(sec, lambda kind, **f: logs.append((kind, f)) or {}, prefix="HBWB·점검")
        for ch in CHANNELS:
            ok = await d.send(ch, f"점검 메시지 → #{ch}")
            print(f"{OK if ok else BAD} Discord #{ch}")
            fails += not ok
        await d.aclose()

    print("\n결과:", "모두 통과" if not fails else f"{fails}개 문제")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--discord", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args())))
