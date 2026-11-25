#!/usr/bin/env python3
"""실거래 전 점검 — 키가 채워졌는지(값은 보지 않는다), 각 API 가 실제로 응답하는지. 주문은 보내지 않는다.

    .venv/bin/python scripts/check_setup.py              # 키가 있는 것만 전부
    .venv/bin/python scripts/check_setup.py --no-ai      # AI 호출(소액 과금) 빼고
    .venv/bin/python scripts/check_setup.py --discord    # 웹훅이 있는 채널에 시험 메시지

키가 아직 비어 있는 서비스는 ⏭️ (건너뜀)으로 표시하고 실패로 세지 않는다 (토스 키는 마지막에 넣어도 된다).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.ai import jev as jev_mod  # noqa: E402
from hbwb.ai.openrouter import OpenRouter  # noqa: E402
from hbwb.broker.toss import TossClient, raw_after_end, toss_after_end  # noqa: E402
from hbwb.clock import fmt_et, fmt_kst  # noqa: E402
from hbwb.config import ENV_PATH, load_dotenv_file, load_secrets, load_settings, scrub  # noqa: E402
from hbwb.engine import sessions as S  # noqa: E402
from hbwb.notify.discord import CHANNELS, Discord  # noqa: E402

OK, BAD, SKIP = "✅", "❌", "⏭️ "


async def check_toss(cfg, sec, secrets) -> int:
    toss = TossClient(sec.get("toss_client_id"), sec.get("toss_client_secret"))
    fails = 0
    try:
        await toss.token()
        print(f"{OK} 토스 토큰")
        await toss.account_seq()
        print(f"{OK} 토스 종합매매 계좌 확인 (계좌번호는 출력 · 저장하지 않음)")
        bp = await toss.buying_power_usd()
        print(f"{OK if bp >= 1 else BAD} USD 매수가능금액 ${bp:.2f}" + ("" if bp >= 1 else " — 앱에서 달러 환전 필요"))
        fails += bp < 1
        print(f"{OK} 미국 주식 수수료율 {await toss.commission_rate_us()}")
        cal = await toss.us_calendar()
        for n, a, b in S.windows_from_calendar(cal):
            print(f"   세션 {n:8s} {fmt_kst(a)} ~ {fmt_kst(b)}  {'(매매)' if S.allowed(n, cfg) else ''}")
        raw = raw_after_end((cal or {}).get("today"))
        if raw:   # D-13: 토스 장 캘린더가 애프터 끝을 몇 시로 주는지 (19:50 이면 그대로 · 20:00 이면 엔진이 10분 당겨 씀)
            used = toss_after_end(raw)
            print(f"{OK} 애프터 끝: 장 캘린더 {fmt_et(raw)} → 엔진이 쓰는 값 {fmt_et(used)}" + (" (10분 당김)" if used != raw else " (그대로)"))
        q = await toss.quote("AAPL")
        print(f"{OK} 시세 AAPL bid {q.bid} ask {q.ask}")
        info = (await toss.stocks(["AAPL"]) or [{}])[0]
        has_shares = float(info.get("sharesOutstanding") or 0) > 0
        print(f"{OK if has_shares else BAD} 종목정보 sharesOutstanding (시가총액 계산에 필요): {info.get('sharesOutstanding')}")
        fails += not has_shares
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} 토스: {scrub(e, secrets)}  (ip-not-allowed 면 WTS 허용 IP 에 이 맥의 공인 IP 등록)")
        fails += 1
    finally:
        await toss.aclose()
    return fails


async def check_ai(cfg, sec, secrets) -> int:
    ai = OpenRouter(sec.get("openrouter_key"))
    try:
        st = jev_mod.build_state("TEST", "Company receives FDA approval for lead drug",
                                 "The FDA approved the company's lead drug for adults.", 50_000_000, 2000)
        jr = await jev_mod.classify(ai, cfg["jev"]["model"], st)
        print(f"{OK} Jev ({cfg['jev']['model']}): {jr.choice} conf={jr.confidence} cost=${jr.usage.cost_usd:.5f}")
        text, u = await ai.chat(cfg["claude"]["model"], [{"role": "user", "content": "Reply with the single word OK."}],
                                max_tokens=20)
        print(f"{OK} Claude ({cfg['claude']['model']}): {text.strip()[:20]!r} cost=${u.cost_usd:.5f}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} OpenRouter: {scrub(e, secrets)}")
        return 1
    finally:
        await ai.aclose()


async def check_discord(sec) -> int:
    d = Discord(sec, lambda kind, **f: {}, prefix="HBWB·점검")
    fails = 0
    try:
        for ch in CHANNELS:
            if not d.urls.get(ch):
                print(f"{SKIP} Discord #{ch}: 웹훅 없음 — 건너뜀")
                continue
            ok = await d.send(ch, f"점검 메시지 → #{ch}")
            print(f"{OK if ok else BAD} Discord #{ch}")
            fails += not ok
    finally:
        await d.aclose()
    return fails


async def main(args) -> int:
    load_dotenv_file()
    cfg, sec = load_settings(), load_secrets()
    have, secrets = sec.present(), sec.all_values()
    fails = 0
    print(f"• .env 파일: {'있음' if ENV_PATH.exists() else '없음 (cp .env.example .env && chmod 600 .env)'}")
    if ENV_PATH.exists() and (ENV_PATH.stat().st_mode & 0o077):
        print(f"  {BAD} .env 권한이 너무 넓다 → chmod 600 .env")
        fails += 1
    for name, present in have.items():
        print(f"  {OK if present else SKIP} {name}{'' if present else ' (비어 있음)'}")

    if have["HBWB_TOSS_CLIENT_ID"] and have["HBWB_TOSS_CLIENT_SECRET"]:
        fails += await check_toss(cfg, sec, secrets)
    else:
        print(f"{SKIP} 토스: 키가 아직 없다 — 건너뜀")

    if args.no_ai:
        print(f"{SKIP} OpenRouter: --no-ai")
    elif have["HBWB_OPENROUTER_KEY"]:
        fails += await check_ai(cfg, sec, secrets)
    else:
        print(f"{SKIP} OpenRouter: 키가 아직 없다 — 건너뜀")

    print(f"{OK if have['HBWB_RTPR_KEY'] else SKIP} RTPR 키 {'있음 (연결은 run_live --mode paper 로 확인)' if have['HBWB_RTPR_KEY'] else '없음 — 건너뜀'}")

    if args.discord:
        fails += await check_discord(sec)

    print("\n결과:", "문제 없음 (⏭️ 는 아직 키를 안 넣은 것)" if not fails else f"{fails}개 문제")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--discord", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args())))
