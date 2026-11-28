#!/usr/bin/env python3
"""paper 엔진이 본 기사들의 '그 뒤 가격'을 토스 1분봉으로 다시 보고, 샀다면 어땠을지 계산한다 (D-20 · 진단 · KNIME).

    .venv/bin/python scripts/replay_live.py            # 종목 범위를 통과하고 Jev 가 판단한 기사 전부
    .venv/bin/python scripts/replay_live.py --only-claude

- 질문: "Claude 가 PASS 한 기사 중 실제로 5분 안 +5% ~ +10% 신호가 난 것이 있었나? 샀다면 이겼나?"
- 실거래 엔진과 같은 규칙 (strategy/backtest.simulate · 기준가 = 발행 전 마지막 1분봉 종가 · 판단 60초 뒤부터 · 매도 사다리)
- 상한 +10% 는 시뮬레이션에 적용하지 않고 `entry_rise_pct` 와 `in_band` 로 따로 남긴다 (데이터셋과 같음)
- AI 는 부르지 않는다 (비용 0). 토스 시세만 쓴다 → 맥에서 (허용 IP) 실행
- 결과: data/knime/live_replay.csv (KNIME 워크플로우 A 의 실시간판 입력으로도 쓴다) + 화면 요약
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.broker.toss import TossClient, TossError  # noqa: E402
from hbwb.clock import ET, iso_utc, ms_to, parse_iso_ms  # noqa: E402
from hbwb.config import ROOT, load_dotenv_file, load_secrets, load_settings, mode_cfg  # noqa: E402
from hbwb.report.daily import read_csv  # noqa: E402
from hbwb.strategy.backtest import simulate  # noqa: E402

COLS = ["article_id", "ticker", "received_utc", "session", "market_cap_usd", "change_vs_prev_close_pct",
        "jev_choice", "jev_expected_grade", "jev_pass", "claude_called", "claude_decision", "claude_grade",
        "claude_confidence", "claude_catalyst", "action_reason", "bars", "triggered", "entry_rise_pct", "in_band",
        "max_up_5m_pct", "sim_ret_pct", "sim_exit_reasons", "label_win", "label_buy", "claude_pred"]


def after_end_ms(ms: int) -> int:
    """그날 토스 애프터 끝 (ET 19:50 · D-13)."""
    t = ms_to(ms, ET)
    return int(t.replace(hour=19, minute=50, second=0, microsecond=0).timestamp() * 1000)


def max_up_5m(bars: list[dict], pub_ms: int, ref: float | None, window_min: float) -> float | None:
    if not ref:
        return None
    hs = [b["h"] for b in bars if pub_ms - 60_000 < b["start_ms"] <= pub_ms + window_min * 60_000]
    return round(100 * (max(hs) / ref - 1), 2) if hs else None


def summarize(rows: list[dict]) -> str:
    g = defaultdict(list)
    for r in rows:
        key = ("Claude 판단 (" + (r["claude_decision"] or "?") + ")") if r["claude_called"] == "true" else (
            "Jev 에서 걸림" if r["jev_pass"] != "true" else "Jev 통과 · Claude 안 부름")
        g[key].append(r)
    out = ["", "구분                       건수  가격자료  신호(+5%↑)  구간(+5~10%)  구간 중 이김  구간 평균수익"]
    for k, rs in sorted(g.items()):
        has = [r for r in rs if r["bars"] not in ("", "0")]
        trig = [r for r in has if r["triggered"] == "yes"]
        band = [r for r in trig if r["in_band"] == "yes"]
        wins = [r for r in band if r["label_win"] == "WIN"]
        avg = sum(float(r["sim_ret_pct"]) for r in band) / len(band) if band else 0.0
        out.append(f"{k:26s} {len(rs):5d} {len(has):8d} {len(trig):10d} {len(band):12d} {len(wins):12d} {avg:+12.2f}%")
    return "\n".join(out)


async def main(args) -> int:
    load_dotenv_file()
    cfg = mode_cfg(load_settings(), "paper")
    sec = load_secrets()
    data = ROOT / cfg["paths"]["data_dir"]
    arts = [a for a in read_csv(data / "ledger" / "articles.csv")
            if a.get("universe_ok") == "true" and a.get("jev_choice") and (not args.only_claude or a.get("claude_called") == "true")]
    # 같은 (종목, 발행 시각) 중복 기사는 한 번만 (RTPR 이 REG- 판 등으로 두 번 보내는 경우)
    uniq = {}
    for a in arts:
        uniq.setdefault((a["ticker"], a.get("published_utc") or a.get("received_utc")), a)
    arts = list(uniq.values())
    print(f"기사 {len(arts)}건 · 토스 1분봉 받는 중 (AI 호출 없음)", flush=True)
    slot = cfg["capital"]["budget_usd"] / cfg["capital"]["slots"]
    fee = cfg["fees"]["commission_rate_default"]
    cap = cfg["entry"].get("trigger_max_rise_pct")
    toss = TossClient(sec.get("toss_client_id"), sec.get("toss_client_secret"))
    sem = asyncio.Semaphore(4)
    rows: list[dict] = []

    async def one(a):
        pub = parse_iso_ms(a.get("published_utc")) or parse_iso_ms(a.get("received_utc"))
        bars, err = [], ""
        async with sem:
            try:
                bars = await toss.candles(a["ticker"], "1m", count=200, before=iso_utc(pub + 170 * 60_000))
            except TossError as e:
                err = e.code
        sim = simulate(bars, pub, cfg, slot, fee, after_end_ms(pub)) if bars else None
        trig = bool(sim and sim.triggered)
        band = trig and sim.entry_rise_pct is not None and (cap is None or sim.entry_rise_pct <= float(cap))
        win = "WIN" if trig and sim.win else ("LOSS" if trig else "")
        rows.append({
            **{k: a.get(k, "") for k in COLS if k in a},
            "bars": len(bars) if not err else f"0 ({err})",
            "triggered": "yes" if trig else "no",
            "entry_rise_pct": sim.entry_rise_pct if trig else "",
            "in_band": "yes" if band else "no",
            "max_up_5m_pct": max_up_5m(bars, pub, sim.ref_price if sim else None, cfg["entry"]["trigger_window_min"]) if bars else "",
            "sim_ret_pct": round(sim.ret_pct, 3) if trig else "",
            "sim_exit_reasons": sim.exit_reasons if trig else "",
            "label_win": win,
            "label_buy": ("BUY" if win == "WIN" else "SKIP") if trig else "",
            "claude_pred": "BUY" if (a.get("claude_decision") or "").upper() == "BUY" else ("SKIP" if a.get("claude_called") == "true" else ""),
        })
    try:
        await asyncio.gather(*(one(a) for a in arts))
    finally:
        await toss.aclose()
    rows.sort(key=lambda r: r.get("received_utc", ""))
    out = data / "knime" / "live_replay.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(summarize(rows))
    band_claude = [r for r in rows if r["claude_called"] == "true" and r["in_band"] == "yes"]
    if band_claude:
        print("\nClaude 가 판단했고 구간(+5~10%) 신호가 났던 기사:")
        for r in band_claude:
            print(f"  {r['ticker']:6s} {r['received_utc'][5:16]} Claude {r['claude_decision']} (등급 {r['claude_grade']}) → "
                  f"샀다면 {float(r['sim_ret_pct']):+.2f}% {r['label_win']} · {r['claude_catalyst'][:50]}")
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only-claude", action="store_true", help="Claude 가 판단한 기사만")
    sys.exit(asyncio.run(main(ap.parse_args())))
