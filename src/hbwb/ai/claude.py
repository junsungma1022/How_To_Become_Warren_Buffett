"""2단계: Claude (settings 의 claude.model · 지금 Sonnet 5.5 · D-19) 가 메인 판단을 하고, 매수할 때는 한국어 '매수 근거 리포트'를 쓴다 (생성형 AI 의 역할).

응답은 JSON 하나. 형식이 깨지면 매수하지 않는다 (fail-closed · 이유는 기록).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .openrouter import OpenRouter, Usage

SYSTEM = """You are the final decision-maker in an automated news-trading experiment on {UNIVERSE}
(including pre-market and after-hours). A cheap screening model has already flagged the
press release below as likely positive. Your job: decide whether the engine may BUY on this news.

{RULES}
So a good BUY is news strong enough to keep the price rising for minutes, not just a brief spike.

This is a paper-trading experiment: we want to observe what happens after positive news, so do not PASS
merely out of general caution. Decide in two steps:
1. Grade the news honestly (impact_grade below), judging the magnitude relative to the company's market cap.
   New, concrete, company-specific facts (numbers, named counterparties, regulatory decisions) grade higher;
   routine or procedural items grade as neutral.
2. BUY when impact_grade is {MIN_GRADE} or higher AND none of these exclusions applies:
   a. The positive news is offset by dilution or financing terms in the same release (offerings, warrants,
      convertible notes, ATM).
   b. It is promotional fluff, a reiteration of old news, conference/webinar participation, or a law-firm solicitation.
   c. The stock already rose sharply BEFORE publication (see "Change before publication"), so the news looks
      leaked or priced in. A rise AFTER publication is expected and is not a reason to PASS: the engine itself
      requires that rise before it buys.
   Otherwise PASS.

Respond with ONE JSON object and nothing else:
{
  "decision": "BUY" or "PASS",
  "impact_grade": integer 1-5 (1 big negative, 2 small negative, 3 neutral, 4 small positive, 5 big positive),
  "confidence": number 0-1,
  "catalyst_type": short English label (e.g. "FDA approval", "large contract", "offering"),
  "key_facts": [up to 4 short strings],
  "risks": [up to 3 short strings],
  "report_ko": "BUY 일 때만 한국어 매수 근거 리포트 4~8줄 (무슨 기사인지, 왜 사는지, 핵심 숫자, 위험 요인) · PASS 면 비워 둔다"
}"""

REPORT_LINE = ',\n  "report_ko": "BUY 일 때만 한국어 매수 근거 리포트 4~8줄 (무슨 기사인지, 왜 사는지, 핵심 숫자, 위험 요인) · PASS 면 비워 둔다"'
assert REPORT_LINE in SYSTEM
SYSTEM_BRIEF = SYSTEM.replace(REPORT_LINE, "")      # 데이터셋용: 리포트 없이 판단만


def rules_text(cfg: dict) -> str:
    """엔진의 실제 매수 · 매도 규칙을 settings.yaml 숫자로 설명한다 (규칙이 바뀌어도 프롬프트가 따라간다 · D-18)."""
    e, x = cfg["entry"], cfg["exit"]
    cap = e.get("trigger_max_rise_pct")
    band = (f"+{e['trigger_rise_pct']:g}% to +{float(cap):g}% above the last pre-news price (no chase above +{float(cap):g}%)"
            if cap is not None else f"at least +{e['trigger_rise_pct']:g}% above the last pre-news price")
    tps = ", ".join(f"+{lvl:g}%" for lvl, _ in sorted(x["take_profits"]))
    part = round(100 * x["take_profits"][0][1])
    times = " or ".join(f"below +{p:g}% after {m:g} minutes" for m, p in sorted(x["time_stops"], key=lambda t: t[0]))
    top = max(lvl for lvl, _ in x["take_profits"])
    return ("How the engine trades after a BUY decision (so you judge the right horizon):\n"
            f"- It buys only if, within {e['trigger_window_min']:g} minutes of publication, the price trades {band}.\n"
            f"  One position uses 1/{cfg['capital']['slots']} of the capital.\n"
            f"- Exits: stop-loss {x['stop_loss_pct']:g}%; sell {part}% each at {tps}; after +{top:g}% a trailing stop "
            f"{x['trail_pct']:g}% below the peak;\n"
            f"  sell all if still {times}; close before the session ends.")


def universe_text(cfg: dict) -> str:
    u = cfg["universe"]
    lo, hi = u.get("min_market_cap_usd"), u.get("max_market_cap_usd")
    if lo is None and hi is None:
        return "US-listed common stocks of any size"
    m = lambda v: f"${v / 1e6:,.0f}M"  # noqa: E731
    rng = f"{m(lo)}–{m(hi)}" if lo is not None and hi is not None else (f"at least {m(lo)}" if lo is not None else f"up to {m(hi)}")
    return f"US-listed common stocks (market cap {rng})"


def system_prompt(cfg: dict, with_report: bool = True) -> str:
    return ((SYSTEM if with_report else SYSTEM_BRIEF).replace("{RULES}", rules_text(cfg))
            .replace("{UNIVERSE}", universe_text(cfg)).replace("{MIN_GRADE}", str(int(cfg["claude"]["buy_min_grade"]))))


@dataclass
class ClaudeResult:
    decision: str                   # BUY | PASS
    impact_grade: int | None
    confidence: float | None
    catalyst_type: str = ""
    key_facts: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    report_ko: str = ""
    usage: Usage = field(default_factory=Usage)
    parse_error: str | None = None
    raw: str = ""

    @property
    def buy(self) -> bool:
        return self.decision == "BUY" and self.parse_error is None


CTX_LABELS = (("market_cap_usd", "Market cap (USD)"), ("session", "Session"),
              ("minutes_since_publish", "Minutes since publication"), ("prev_close", "Previous close"),
              ("pre_news_price", "Last price before publication"),
              ("change_before_pub_pct", "Change before publication (% vs previous close)"),
              ("last_price", "Last trade price now"),
              ("move_since_pub_pct", "Move since publication (% vs last price before publication)"))


def user_message(ticker: str, title: str, body: str, ctx: dict, body_max: int) -> str:
    """가격 맥락: 발행 전 등락과 발행 뒤 움직임을 따로 준다 (D-21 · 규칙 c 는 '발행 전' 만 본다)."""
    lines = [f"Ticker: {ticker}", f"Headline: {title}"]
    for k, label in CTX_LABELS:
        if ctx.get(k) is not None:
            lines.append(f"{label}: {ctx[k]}")
    lines += ["", "Press release:", (body or "")[:body_max]]
    return "\n".join(lines)


def price_context(prev_close: float | None, pre_news: float | None, last: float | None) -> dict:
    """발행 전 가격(기준가) · 지금 가격 → Claude 에 줄 등락률 두 개."""
    pct = lambda a, b: round(100 * (a / b - 1), 2) if a and b else None  # noqa: E731
    return {"prev_close": prev_close, "pre_news_price": pre_news, "last_price": last,
            "change_before_pub_pct": pct(pre_news, prev_close), "move_since_pub_pct": pct(last, pre_news)}


_OBJ = re.compile(r"\{.*\}", re.S)


def parse_reply(text: str, usage: Usage) -> ClaudeResult:
    m = _OBJ.search(text or "")
    if not m:
        return ClaudeResult("PASS", None, None, usage=usage, parse_error="no_json", raw=(text or "")[:500])
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        return ClaudeResult("PASS", None, None, usage=usage, parse_error=f"bad_json: {e}"[:120], raw=text[:500])
    dec = str(d.get("decision") or "").upper()
    if dec not in {"BUY", "PASS"}:
        return ClaudeResult("PASS", None, None, usage=usage, parse_error=f"bad_decision: {dec!r}", raw=text[:500])
    try:
        grade = int(d["impact_grade"]) if d.get("impact_grade") is not None else None
        if grade is not None and not 1 <= grade <= 5:
            raise ValueError(grade)
        conf = float(d["confidence"]) if d.get("confidence") is not None else None
    except (TypeError, ValueError) as e:
        return ClaudeResult("PASS", None, None, usage=usage, parse_error=f"bad_field: {e}"[:120], raw=text[:500])
    return ClaudeResult(dec, grade, conf, str(d.get("catalyst_type") or "")[:80],
                        [str(x)[:200] for x in (d.get("key_facts") or [])][:4],
                        [str(x)[:200] for x in (d.get("risks") or [])][:3],
                        str(d.get("report_ko") or "")[:1500], usage, None, text[:2000])


async def judge(client: OpenRouter, cfg: dict, ticker: str, title: str, body: str, ctx: dict,
                with_report: bool = True) -> ClaudeResult:
    c = cfg["claude"]
    msgs = [{"role": "system", "content": system_prompt(cfg, with_report)},
            {"role": "user", "content": user_message(ticker, title, body, ctx, cfg["news"]["body_max_chars"])}]
    text, usage = await client.chat(c["model"], msgs, max_tokens=c["max_tokens"] if with_report else 700,
                                    reasoning_effort=c.get("reasoning_effort"), timeout=c.get("timeout_s", 60))
    return parse_reply(text, usage)
