#!/usr/bin/env python3
"""KNIME 분석용 과거 데이터셋 만들기 (RTPR 과거 기사 → 종목 범위 → Jev → Claude → 토스 1분봉 → 정답 라벨 → CSV).

이전 프로젝트에서 받아 둔 RTPR 기사 본문(bodies.jsonl · 2026-05 ~ 10)을 읽는다. 원문은 저장소에 올리지 않는다
(data/ 는 .gitignore). 단계마다 결과를 data/backfill/ 에 쌓아 두므로 끊겨도 같은 명령으로 이어서 돈다.

    # 0) 몇 건이 어느 단계로 가는지 · AI 비용 추정만 (AI 호출 없음 · 토스 시세는 받음)
    python scripts/build_dataset.py --stage prices --limit 300
    python scripts/build_dataset.py --estimate
    # 1) Jev (아주 쌈) → 2) Claude (비쌈 · --claude-max 로 상한) → 3) CSV
    python scripts/build_dataset.py --stage jev
    python scripts/build_dataset.py --stage claude --claude-max 50     # 사용자: $5 크레딧 안에서 50건
    python scripts/build_dataset.py --stage build

정답 라벨 (사용자 승인 2026-11-18):
- triggered     : 실거래와 같은 매수 신호 — AI 판단 60초 뒤부터 발행 +5분 안에 기준가(발행 전 마지막 1분봉 종가) 대비 +5%
                  (실거래의 +10% 상한은 여기 적용하지 않는다 · 진입 상승률은 sim_entry_rise_pct → KNIME 결정트리로 상한을 정한다)
- label_win     : 신호가 났을 때 칸 금액(자금의 1/3)으로 사서 **실거래와 같은 매도 규칙**(이전 프로그램)으로 청산했을 때
                  수수료를 빼고 이익이면 WIN, 아니면 LOSS. 신호가 없었으면 빈칸.
- label_surge30 : 보조 — 그날 고가 ≥ 전일 종가 × 1.3 (사용자 원안 '전날 종가 대비 +30%')
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.ai import claude as claude_mod  # noqa: E402
from hbwb.ai import jev as jev_mod  # noqa: E402
from hbwb.ai.openrouter import OpenRouter, OpenRouterError  # noqa: E402
from hbwb.broker.toss import TossClient, TossError  # noqa: E402
from hbwb.clock import ET, iso_utc, ms_to  # noqa: E402
from hbwb.config import ROOT, load_dotenv_file, load_secrets, load_settings  # noqa: E402
from hbwb.news.rtpr import parse_article_text  # noqa: E402
from hbwb.strategy.backtest import simulate  # noqa: E402
from hbwb.universe import judge, prev_close_from_daily  # noqa: E402

DEFAULT_BODIES = ROOT.parent / "Auto_Trader_Futures" / "data" / "news" / "pilot" / "rtpr_history" / "bodies.jsonl"
US_EXCH = {"NASDAQ", "NYSE", "NYSE AMERICAN", "AMEX", "NYSEAMERICAN", "NYSE ARCA"}

# 비용 추정 (OpenRouter 가격 · 2026-11-18 확인): Jev 입력 $0.042/M · 출력 무료, Opus 5.5 입력 $4/M · 출력 $20/M
JEV_IN_PER_M, OPUS_IN_PER_M, OPUS_OUT_PER_M = 0.042, 4.0, 20.0


def session_of(ms: int) -> str:
    t = ms_to(ms, ET)
    if t.weekday() >= 5:
        return "closed"
    m = t.hour * 60 + t.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "pre"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "regular"
    if 16 * 60 <= m < 20 * 60:
        return "after"
    return "closed"


def after_close_ms(ms: int) -> int:
    t = ms_to(ms, ET)
    return int(t.replace(hour=20, minute=0, second=0, microsecond=0).timestamp() * 1000)


class Store:
    """JSONL 캐시 — key 마다 마지막 줄이 이긴다."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.d: dict[str, dict] = {}
        if path.exists():
            for ln in path.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    r = json.loads(ln)
                    self.d[r["k"]] = r

    def put(self, k: str, rec: dict) -> None:
        rec = {"k": k, **rec}
        self.d[k] = rec
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_articles(path: Path, args) -> list[dict]:
    out, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for ln in f:
            r = json.loads(ln)
            if str(r.get("status")) != "200" or not r.get("text") or r["url"] in seen:
                continue
            seen.add(r["url"])
            title, exch = parse_article_text(r["text"])
            if exch and exch.upper() not in US_EXCH:
                continue
            pub = int(r["pub_ms"])
            day = ms_to(pub, ET).strftime("%Y-%m-%d")
            if (args.since and day < args.since) or (args.until and day > args.until):
                continue
            out.append({"k": r["url"], "ticker": r["ticker"].upper(), "pub_ms": pub, "title": title, "exchange": exch,
                        "text": r["text"]})
    out.sort(key=lambda x: x["pub_ms"])
    return out[: args.limit] if args.limit else out


async def stage_prices(arts, toss: TossClient, cfg, stores):
    info_s, daily_s, bars_s = stores["info"], stores["daily"], stores["bars"]
    tickers = sorted({a["ticker"] for a in arts} - set(info_s.d))
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            rows = {str(r.get("symbol")).upper(): r for r in await toss.stocks(chunk)}
        except TossError as e:
            rows = {}
            print("stocks 오류", e.code, "— 한 종목씩 다시", flush=True)
            for t in chunk:
                try:
                    rows.update({str(r.get("symbol")).upper(): r for r in await toss.stocks([t])})
                except TossError:
                    pass
        for t in chunk:
            info_s.put(t, {"info": rows.get(t)})
    for t in sorted({a["ticker"] for a in arts} - set(daily_s.d)):
        try:
            daily_s.put(t, {"daily": await toss.candles(t, "1d", count=200)})
        except TossError as e:
            daily_s.put(t, {"daily": [], "err": e.code})
    sem = asyncio.Semaphore(8)

    async def one(a):
        if a["k"] in bars_s.d or not (info_s.d.get(a["ticker"]) or {}).get("info"):
            return
        async with sem:
            try:
                # 발행 30분 전 ~ 170분 뒤 (트레일링으로 오래 들고 있는 경우까지)
                bars = await toss.candles(a["ticker"], "1m", count=200, before=iso_utc(a["pub_ms"] + 170 * 60_000))
                bars_s.put(a["k"], {"bars": bars})
            except TossError as e:
                bars_s.put(a["k"], {"bars": [], "err": e.code})
    await asyncio.gather(*(one(a) for a in arts))


def universe_of(a, cfg, stores):
    info = (stores["info"].d.get(a["ticker"]) or {}).get("info")
    daily = (stores["daily"].d.get(a["ticker"]) or {}).get("daily") or []
    pc = prev_close_from_daily(daily, a["pub_ms"])
    return judge(info, pc, cfg), daily


async def stage_jev(arts, ai, cfg, stores):
    sem, n = asyncio.Semaphore(8), 0

    async def one(a):
        nonlocal n
        uc, _ = universe_of(a, cfg, stores)
        if not uc.ok or a["k"] in stores["jev"].d:
            return
        async with sem:
            try:
                jr = await jev_mod.classify(ai, cfg["jev"]["model"], jev_mod.build_state(
                    a["ticker"], a["title"], a["text"], uc.market_cap, cfg["news"]["body_max_chars"]))
                stores["jev"].put(a["k"], {"choice": jr.choice, "probs": jr.probabilities, "conf": jr.confidence,
                                           "refusal": jr.refusal, "cost": jr.usage.cost_usd})
                n += 1
                if n % 200 == 0:
                    print(f"  Jev {n}건", flush=True)
            except OpenRouterError as e:
                print("Jev 오류", e.status, str(e)[:120], flush=True)
    await asyncio.gather(*(one(a) for a in arts))


def price_ctx(a, uc, bars_s):
    bars = (bars_s.d.get(a["k"]) or {}).get("bars") or []
    before = [b for b in bars if b["start_ms"] + 60_000 <= a["pub_ms"]]   # 기사 전에 끝난 봉만 (미래 정보 금지)
    last = before[-1]["c"] if before else None
    chg = round(100 * (last / uc.prev_close - 1), 2) if last and uc.prev_close else None
    return {"market_cap_usd": round(uc.market_cap) if uc.market_cap else None, "session": session_of(a["pub_ms"]),
            "minutes_since_publish": 1.0, "prev_close": uc.prev_close, "last_price": last, "change_vs_prev_close_pct": chg}


async def stage_claude(arts, ai, cfg, stores, cap: int):
    todo = []
    for a in arts:
        j = stores["jev"].d.get(a["k"])
        if j and not j.get("refusal") and j.get("choice") in cfg["jev"]["pass_choices"] and a["k"] not in stores["claude"].d:
            todo.append(a)
    todo = todo[:cap]
    print(f"Claude 대상 {len(todo)}건 (상한 {cap})", flush=True)
    sem, n = asyncio.Semaphore(4), 0

    async def one(a):
        nonlocal n
        uc, _ = universe_of(a, cfg, stores)
        async with sem:
            try:
                cr = await claude_mod.judge(ai, cfg, a["ticker"], a["title"], a["text"], price_ctx(a, uc, stores["bars"]),
                                            with_report=False)
                stores["claude"].put(a["k"], {"decision": cr.decision, "grade": cr.impact_grade, "conf": cr.confidence,
                                              "catalyst": cr.catalyst_type, "parse_error": cr.parse_error, "cost": cr.usage.cost_usd})
                n += 1
                if n % 25 == 0:
                    print(f"  Claude {n}건", flush=True)
            except OpenRouterError as e:
                print("Claude 오류", e.status, str(e)[:120], flush=True)
    await asyncio.gather(*(one(a) for a in todo))


COLS = ["article_id", "ticker", "published_utc", "published_et_hour", "weekday_et", "session", "exchange",
        "title_len", "body_len", "market_cap_usd", "log10_market_cap", "prev_close", "price_at_news",
        "change_vs_prev_close_pct", "ret_15m_before_pct", "volume_15m_before",
        "jev_choice", "jev_grade", "jev_expected_grade", "jev_confidence",
        "jev_p_big_negative", "jev_p_small_negative", "jev_p_neutral", "jev_p_small_positive", "jev_p_big_positive",
        "jev_pred", "claude_called", "claude_decision", "claude_pred", "claude_grade", "claude_confidence", "claude_catalyst",
        "ref_price", "triggered", "sim_entry_rise_pct", "sim_entry_price", "sim_pnl_usd", "sim_ret_pct", "sim_max_up_pct", "sim_max_down_pct",
        "sim_exit_reasons", "label_win", "label_buy", "label_surge30", "day_high_vs_prev_close_pct"]


def build(arts, cfg, stores, out: Path, fee_rate: float, slot_usd: float) -> int:
    import csv
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for a in arts:
            uc, daily = universe_of(a, cfg, stores)
            if not uc.ok:
                continue
            j = stores["jev"].d.get(a["k"]) or {}
            c = stores["claude"].d.get(a["k"]) or {}
            bars = (stores["bars"].d.get(a["k"]) or {}).get("bars") or []
            t = ms_to(a["pub_ms"], ET)
            before = [b for b in bars if a["pub_ms"] - 15 * 60_000 <= b["start_ms"] and b["start_ms"] + 60_000 <= a["pub_ms"]]
            ctx = price_ctx(a, uc, stores["bars"])
            sim = simulate(bars, a["pub_ms"], cfg, slot_usd, fee_rate, after_close_ms(a["pub_ms"])) if bars else None
            ok = bool(sim and sim.triggered)
            day = next((d for d in daily if ms_to(d["start_ms"], ET).date() == t.date()), None)
            hi_pct = round(100 * (day["h"] / uc.prev_close - 1), 2) if day and uc.prev_close else None
            probs = j.get("probs") or {}
            choice = j.get("choice")
            row = {
                "article_id": a["k"].rstrip("/").rsplit("/", 1)[-1], "ticker": a["ticker"], "published_utc": iso_utc(a["pub_ms"]),
                "published_et_hour": round(t.hour + t.minute / 60, 2), "weekday_et": t.strftime("%a"),
                "session": session_of(a["pub_ms"]), "exchange": a["exchange"], "title_len": len(a["title"]),
                "body_len": len(a["text"]), "market_cap_usd": round(uc.market_cap),
                "log10_market_cap": round(math.log10(uc.market_cap), 4), "prev_close": uc.prev_close,
                "price_at_news": ctx["last_price"], "change_vs_prev_close_pct": ctx["change_vs_prev_close_pct"],
                "ret_15m_before_pct": round(100 * (before[-1]["c"] / before[0]["o"] - 1), 3) if len(before) >= 2 else None,
                "volume_15m_before": sum(b["v"] for b in before) if before else 0,
                "jev_choice": choice or "", "jev_grade": jev_mod.GRADE.get(choice) if choice else None,
                "jev_expected_grade": round(jev_mod.expected_grade(probs), 4) if probs else None,
                "jev_confidence": j.get("conf"),
                **{f"jev_p_{k}": probs.get(k) for k in jev_mod.CLASSES},
                "jev_pred": ("BUY" if choice in cfg["jev"]["pass_choices"] else "SKIP") if choice else "",
                "claude_called": "yes" if c else "no", "claude_decision": c.get("decision", ""),
                # Claude 를 안 불렀으면(=Jev 에서 걸러짐) 파이프라인 전체의 판단은 SKIP
                # Jev 를 통과했는데 Claude 를 아직 안 불렀으면(--claude-max 상한) 빈칸 → KNIME Row Filter 로 뺀다
                "claude_pred": ("BUY" if c.get("decision") == "BUY" else "SKIP") if c
                               else ("SKIP" if choice and choice not in cfg["jev"]["pass_choices"] else ""),
                "claude_grade": c.get("grade"), "claude_confidence": c.get("conf"), "claude_catalyst": c.get("catalyst", ""),
                "ref_price": sim.ref_price if sim else None,
                "triggered": "yes" if ok else "no",
                "sim_entry_rise_pct": sim.entry_rise_pct if ok else None,
                "sim_entry_price": sim.entry_price if ok else None,
                "sim_pnl_usd": sim.pnl_usd if ok else None,
                "sim_ret_pct": sim.ret_pct if ok else None,
                "sim_max_up_pct": sim.max_up_pct if ok else None,
                "sim_max_down_pct": sim.max_down_pct if ok else None,
                "sim_exit_reasons": sim.exit_reasons if ok else "",
                "label_win": ("WIN" if sim.win else "LOSS") if ok else "",
                # Scorer 가 바로 비교할 수 있게 예측 열과 같은 값 이름(BUY/SKIP)으로 만든 정답
                "label_buy": ("BUY" if sim.win else "SKIP") if ok else "",
                "label_surge30": ("YES" if hi_pct >= 30 else "NO") if hi_pct is not None else "",
                "day_high_vs_prev_close_pct": hi_pct,
            }
            w.writerow({k: ("" if v is None else v) for k, v in row.items()})
            n += 1
    return n


def estimate(arts, cfg, stores):
    uni = [a for a in arts if universe_of(a, cfg, stores)[0].ok]
    passed = [a for a in uni if (stores["jev"].d.get(a["k"]) or {}).get("choice") in cfg["jev"]["pass_choices"]]
    tok = lambda a: (len(a["title"]) + min(len(a["text"]), cfg["news"]["body_max_chars"])) / 3.5  # noqa: E731
    jev_cost = sum(tok(a) for a in uni if a["k"] not in stores["jev"].d) / 1e6 * JEV_IN_PER_M
    pending = [a for a in passed if a["k"] not in stores["claude"].d]
    cl_cost = sum((tok(a) + 900) / 1e6 * OPUS_IN_PER_M + 350 / 1e6 * OPUS_OUT_PER_M for a in pending)
    print(f"기사 {len(arts)} · 종목범위 통과 {len(uni)} (시세 받은 것 기준) · Jev 완료 {sum(a['k'] in stores['jev'].d for a in uni)}")
    print(f"Jev 남은 비용 ≈ ${jev_cost:.2f}")
    print(f"Jev 통과 {len(passed)} · Claude 남은 {len(pending)}건 ≈ ${cl_cost:.2f} (Opus 5.5 · 추정)")


async def main(args) -> int:
    load_dotenv_file()
    cfg, sec = load_settings(), load_secrets()
    base = ROOT / cfg["paths"]["data_dir"] / "backfill"
    stores = {k: Store(base / f"{k}.jsonl") for k in ("info", "daily", "bars", "jev", "claude")}
    arts = load_articles(Path(args.bodies), args)
    print(f"기사 {len(arts)}건 ({args.bodies})", flush=True)
    slot_usd = cfg["capital"]["budget_usd"] / cfg["capital"]["slots"]
    fee = cfg["fees"]["commission_rate_default"]
    if args.estimate:
        estimate(arts, cfg, stores)
        return 0
    if args.stage == "prices":
        toss = TossClient(sec.get("toss_client_id"), sec.get("toss_client_secret"))
        try:
            await stage_prices(arts, toss, cfg, stores)
        finally:
            await toss.aclose()
        estimate(arts, cfg, stores)
    elif args.stage in ("jev", "claude"):
        ai = OpenRouter(sec.get("openrouter_key"))
        try:
            if args.stage == "jev":
                await stage_jev(arts, ai, cfg, stores)
            else:
                await stage_claude(arts, ai, cfg, stores, args.claude_max)
        finally:
            await ai.aclose()
        estimate(arts, cfg, stores)
    elif args.stage == "build":
        out = ROOT / cfg["paths"]["data_dir"] / "knime" / "news_dataset.csv"
        n = build(arts, cfg, stores, out, fee, slot_usd)
        print(f"CSV {n}행 → {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bodies", default=str(DEFAULT_BODIES))
    ap.add_argument("--stage", choices=["prices", "jev", "claude", "build"])
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--claude-max", type=int, default=50)   # 사용자 2026-11-25: OpenRouter $5 안에서 50건 (D-18)
    a = ap.parse_args()
    if not a.stage and not a.estimate:
        ap.error("--stage 또는 --estimate")
    sys.exit(asyncio.run(main(a)))
