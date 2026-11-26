"""1단계: Jev 로 기사를 5단계로 나눈다 (최소 컷 — 쓰레기만 거른다).

설계 결정 (사용자 · 2026-11-18): Jev 는 1번만 부른다 (같은 입력 반복 앙상블은 의미 없음).
Jev 는 선택지마다 확률을 주므로 '평균 점수'는 기대값 Σ p·등급 으로 한 번에 얻는다.
`pass_choices`(작은 호재 · 큰 호재)면 confidence 와 상관없이 Claude 로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .openrouter import OpenRouter, Usage

CLASSES = ["big_negative", "small_negative", "neutral", "small_positive", "big_positive"]
GRADE = {c: i + 1 for i, c in enumerate(CLASSES)}
KO = {"big_negative": "큰 악재", "small_negative": "작은 악재", "neutral": "악재도 호재도 아님",
      "small_positive": "작은 호재", "big_positive": "큰 호재"}

INSTRUCTIONS = (
    "You are screening a press release about a US-listed stock (market cap is given when known; many are small caps). "
    "Judge how this news most likely moves THIS company's share price within the next 30 minutes after release. "
    "Small caps react strongly to concrete, material, company-specific news and weakly to routine announcements; "
    "large caps need news that is large relative to their size."
)
CRITERIA = {
    "big_negative": "Severe negative: large dilutive offering or warrants at a discount, failed pivotal trial, FDA rejection, "
                    "bankruptcy/going-concern, delisting, fraud or major investigation, loss of key customer or contract.",
    "small_negative": "Mildly negative: small or at-market offering/ATM, guidance cut, earnings miss, executive departure, "
                      "minor regulatory or legal setback.",
    "neutral": "No clear price impact: routine or promotional news, conference or webinar participation, reiterated plans, "
               "law-firm class-action solicitations, minor personnel news, vague partnerships without numbers.",
    "small_positive": "Mildly positive: modest contract or partnership, incremental product or regulatory progress, "
                      "earnings slightly above expectations, small buyback.",
    "big_positive": "Strong positive catalyst: FDA approval or positive pivotal data, acquisition at a premium, contract or "
                    "order large relative to market cap, major strategic partnership with a well-known company, "
                    "transformative earnings beat or guidance raise.",
}


@dataclass
class JevResult:
    choice: str | None
    probabilities: dict
    confidence: float | None
    expected_grade: float | None
    usage: Usage
    refusal: bool = False

    @property
    def grade(self) -> int | None:
        return GRADE.get(self.choice) if self.choice else None

    def passes(self, pass_choices) -> bool:
        return (not self.refusal) and self.choice in set(pass_choices)


def build_state(ticker: str, title: str, body: str, market_cap: float | None, body_max: int) -> dict:
    st = {"ticker": ticker, "headline": title, "press_release": (body or "")[:body_max]}
    if market_cap:
        st["market_cap_usd"] = round(market_cap)
    return st


def parse_answer(ans: dict) -> tuple[str | None, dict, float | None, bool]:
    a = (ans or {}).get("impact") or {}
    if a.get("type") == "refusal":
        return None, {}, None, True
    choice = a.get("choice") if a.get("choice") in GRADE else None
    probs = {k: float(v) for k, v in (a.get("probabilities") or {}).items() if k in GRADE}
    conf = a.get("confidence")
    return choice, probs, (float(conf) if conf is not None else None), False


def expected_grade(probs: dict) -> float | None:
    s = sum(probs.values())
    if s <= 0:
        return None
    return sum(GRADE[k] * v for k, v in probs.items()) / s


async def classify(client: OpenRouter, model: str, state: dict) -> JevResult:
    q = {"impact": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA}}
    answers, usage = await client.decide(model, state, q)
    choice, probs, conf, refusal = parse_answer(answers)
    return JevResult(choice, probs, conf, expected_grade(probs), usage, refusal)
