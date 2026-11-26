import asyncio
import json

import httpx

from hbwb.ai import claude as C
from hbwb.ai import jev as J
from hbwb.ai.openrouter import OpenRouter, Usage


async def _nosleep(_s):
    return None


def test_jev_parse_and_expected_grade():
    ans = {"impact": {"type": "choice", "choice": "small_positive",
                      "probabilities": {"neutral": 0.2, "small_positive": 0.5, "big_positive": 0.3}, "confidence": 0.4}}
    choice, probs, conf, refusal = J.parse_answer(ans)
    assert choice == "small_positive" and conf == 0.4 and not refusal
    assert abs(J.expected_grade(probs) - (3 * .2 + 4 * .5 + 5 * .3)) < 1e-9
    r = J.JevResult(choice, probs, conf, J.expected_grade(probs), Usage())
    assert r.passes(["small_positive", "big_positive"]) and r.grade == 4


def test_jev_refusal_and_unknown_choice():
    assert J.parse_answer({"impact": {"type": "refusal"}})[3] is True
    assert J.parse_answer({"impact": {"type": "choice", "choice": "weird"}})[0] is None


def test_claude_parse_ok_and_failures():
    ok = C.parse_reply('blah {"decision":"BUY","impact_grade":5,"confidence":0.8,"catalyst_type":"FDA approval",'
                       '"key_facts":["a"],"risks":["b"],"report_ko":"리포트"} tail', Usage())
    assert ok.buy and ok.impact_grade == 5 and ok.report_ko == "리포트"
    assert not C.parse_reply("no json here", Usage()).buy
    assert C.parse_reply('{"decision":"MAYBE"}', Usage()).parse_error.startswith("bad_decision")
    assert C.parse_reply('{"decision":"BUY","impact_grade":9}', Usage()).parse_error.startswith("bad_field")
    assert not C.parse_reply('{"decision":"BUY","impact_grade":', Usage()).buy


def test_brief_prompt_has_no_report():
    assert "report_ko" in C.SYSTEM and "report_ko" not in C.SYSTEM_BRIEF


def test_openrouter_decide_and_chat_requests(cfg):
    seen = []

    def h(req):
        seen.append(req)
        body = json.loads(req.content)
        if req.url.path == "/api/alpha/decisions":
            assert body["questions"]["impact"]["type"] == "choice"
            assert set(body["questions"]["impact"]["criteria"]) == set(J.CLASSES)
            return httpx.Response(200, json={"answers": {"impact": {"type": "choice", "choice": "big_positive",
                                                                    "probabilities": {"big_positive": 1.0}, "confidence": 0.9}},
                                             "usage": {"input_tokens": 100, "output_tokens": 0, "cost": 0.00001}})
        if req.url.path == "/api/v1/chat/completions":
            assert body["model"] == cfg["claude"]["model"] and body["reasoning"] == {"effort": "low"}
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"decision":"PASS","impact_grade":3,"confidence":0.6}'}}],
                                             "usage": {"prompt_tokens": 500, "completion_tokens": 50, "cost": 0.003}})
        return httpx.Response(404)

    ai = OpenRouter("sk-or-v1-" + "x" * 40, http=httpx.AsyncClient(base_url="https://openrouter.ai", transport=httpx.MockTransport(h)),
                    sleep=_nosleep)
    jr = asyncio.run(J.classify(ai, "typesafe/jev-1.13", J.build_state("ABCD", "t", "b", 1e7, 100)))
    assert jr.choice == "big_positive" and jr.usage.cost_usd == 0.00001
    cfg["claude"].update(max_tokens=100, timeout_s=5)
    cfg["news"]["body_max_chars"] = 100
    cr = asyncio.run(C.judge(ai, cfg, "ABCD", "t", "b", {}, with_report=False))
    assert cr.decision == "PASS" and abs(cr.usage.cost_usd - 0.003) < 1e-12
    assert all(r.headers["Authorization"].startswith("Bearer sk-or-v1-") for r in seen)


def test_openrouter_retries_then_scrubs_error():
    n = {"k": 0}
    key = "sk-or-v1-" + "y" * 40

    def h(req):
        n["k"] += 1
        return httpx.Response(429, text=f"slow down {key}")
    ai = OpenRouter(key, http=httpx.AsyncClient(base_url="https://openrouter.ai", transport=httpx.MockTransport(h)), sleep=_nosleep)
    try:
        asyncio.run(ai.decide("m", "s", {}))
        raise AssertionError("should fail")
    except Exception as e:  # noqa: BLE001
        assert "yyyy" not in str(e)
    assert n["k"] == 3


def test_discord_retries_on_429():
    from hbwb.config import load_secrets
    from hbwb.notify.discord import Discord
    n = {"k": 0}

    def h(req):
        n["k"] += 1
        if n["k"] == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(204)
    sec = load_secrets({"HBWB_DISCORD_WEBHOOK_WARNINGS": "https://discord.com/api/webhooks/1/abc"})
    d = Discord(sec, lambda *a, **k: {}, http=httpx.AsyncClient(transport=httpx.MockTransport(h)), sleep=_nosleep)
    assert asyncio.run(d.send("warnings", "hi")) is True and n["k"] == 2


def test_claude_prompt_matches_real_rules(cfg):
    """D-18: 프롬프트가 실제 매수 · 매도 규칙(settings.yaml)을 설명한다 — 예전 '30분 · +10/+20% · −5%' 가 아니다."""
    sp = C.system_prompt(cfg)
    assert "{RULES}" not in sp and "+5% to +10%" in sp and "stop-loss -7%" in sp
    assert "+5%, +10%, +15%" in sp and "5% below the peak" in sp and "below +2% after 5 minutes" in sp
    assert "30 minutes" not in sp and "-5%)" not in sp
    assert "BUY 일 때만" in sp and "report_ko" not in C.system_prompt(cfg, with_report=False)
    cfg["entry"]["trigger_max_rise_pct"] = 12.0                      # KNIME 으로 상한을 바꾸면 프롬프트도 따라간다
    assert "+5% to +12%" in C.system_prompt(cfg)
    assert "{UNIVERSE}" not in sp and "any size" in sp                # D-19: 시총 제한 없음
    cfg["universe"].update(min_market_cap_usd=5_000_000, max_market_cap_usd=300_000_000)
    assert "market cap $5M–$300M" in C.system_prompt(cfg)
