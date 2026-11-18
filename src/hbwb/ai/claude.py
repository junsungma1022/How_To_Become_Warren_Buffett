"""2단계: Claude Opus 5.5 가 메인 판단을 하고, 매수할 때는 한국어 '매수 근거 리포트'를 쓴다 (생성형 AI 의 역할).

응답은 JSON 하나. 형식이 깨지면 매수하지 않는다 (fail-closed · 이유는 기록).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .openrouter import OpenRouter, Usage

SYSTEM = """You are the final decision-maker in an automated news-trading experiment on US small-cap stocks
(market cap $5M–$300M, including pre-market and after-hours). A cheap screening model has already flagged the
press release below as likely positive. Your job: decide whether to BUY right now for a short hold (at most 30 minutes;
take-profit +10%/+20%, stop-loss -5%).

BUY only when ALL of these hold:
1. The news is new, concrete, material and company-specific (numbers, named counterparties, regulatory decisions).
2. The magnitude is large relative to the company's market cap.
3. It is not offset by dilution or financing terms in the same release (offerings, warrants, convertible notes, ATM).
4. It is not promotional fluff, a reiteration of old news, conference/webinar participation, or a law-firm solicitation.
5. The price has not obviously already absorbed the news (use the price context if given).
Otherwise PASS. When unsure, PASS — missing a trade costs nothing, a bad trade costs money.

Respond with ONE JSON object and nothing else:
{
  "decision": "BUY" or "PASS",
  "impact_grade": integer 1-5 (1 big negative, 2 small negative, 3 neutral, 4 small positive, 5 big positive),
  "confidence": number 0-1,
  "catalyst_type": short English label (e.g. "FDA approval", "large contract", "offering"),
  "key_facts": [up to 4 short strings],
  "risks": [up to 3 short strings],
  "report_ko": "한국어 매수 근거 리포트 4~8줄: 무슨 기사인지, 왜 사는지/안 사는지, 핵심 숫자, 위험 요인"
}"""

SYSTEM_BRIEF = SYSTEM.replace(
    ',\n  "report_ko": "한국어 매수 근거 리포트 4~8줄: 무슨 기사인지, 왜 사는지/안 사는지, 핵심 숫자, 위험 요인"', "")


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


def user_message(ticker: str, title: str, body: str, ctx: dict, body_max: int) -> str:
    lines = [f"Ticker: {ticker}", f"Headline: {title}"]
    for k, label in (("market_cap_usd", "Market cap (USD)"), ("session", "Session"),
                     ("minutes_since_publish", "Minutes since publication"), ("prev_close", "Previous close"),
                     ("last_price", "Last trade price"), ("change_vs_prev_close_pct", "Change vs previous close (%)")):
        if ctx.get(k) is not None:
            lines.append(f"{label}: {ctx[k]}")
    lines += ["", "Press release:", (body or "")[:body_max]]
    return "\n".join(lines)


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
    msgs = [{"role": "system", "content": SYSTEM if with_report else SYSTEM_BRIEF},
            {"role": "user", "content": user_message(ticker, title, body, ctx, cfg["news"]["body_max_chars"])}]
    text, usage = await client.chat(c["model"], msgs, max_tokens=c["max_tokens"] if with_report else 700,
                                    reasoning_effort=c.get("reasoning_effort"), timeout=c.get("timeout_s", 60))
    return parse_reply(text, usage)
