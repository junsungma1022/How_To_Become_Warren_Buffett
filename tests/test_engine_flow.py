"""엔진 한 바퀴 (shadow): 기사 → 종목 범위 → Jev → Claude → 1차 매수 → 2차 매수 → 분할 매도 → 거래 기록."""

import asyncio

from hbwb.ai.openrouter import Usage
from hbwb.broker.toss import Quote
from hbwb.clock import now_ms
from hbwb.config import load_secrets
from hbwb.engine.executor import ShadowExecutor
from hbwb.engine.runner import Engine
from hbwb.eventlog import EventLog
from hbwb.ledger import articles, trades
from hbwb.news.rtpr import Alert, Article
from hbwb.universe import UniverseCheck

SECRETS = {"HBWB_RTPR_KEY": "rtpr-test-key", "HBWB_OPENROUTER_KEY": "sk-or-v1-test", "HBWB_TOSS_CLIENT_ID": "cid",
           "HBWB_TOSS_CLIENT_SECRET": "sec"}


class FakeToss:
    def __init__(self):
        self.px = 2.0

    async def last_prices(self, syms):
        return {s: self.px for s in syms}

    async def quote(self, sym):
        return Quote(self.px * 0.999, self.px * 1.001, now_ms())

    async def aclose(self):
        pass


class FakeUniverse:
    async def check(self, sym, t):
        return UniverseCheck(True, "ok", 20_000_000, 1.8, 1e7, "NASDAQ", "X")


class FakeRtpr:
    async def article(self, url):
        return Article("ABC wins $50M contract", "NASDAQ", "body text", 200)

    async def aclose(self):
        pass


class FakeAI:
    def __init__(self, jev_choice="big_positive", claude='{"decision":"BUY","impact_grade":5,"confidence":0.8,"catalyst_type":"contract","report_ko":"근거"}'):
        self.jev_choice, self.claude = jev_choice, claude
        self.chats = 0

    async def decide(self, model, state, questions, timeout=20):
        return {"impact": {"type": "choice", "choice": self.jev_choice, "probabilities": {self.jev_choice: 1.0},
                           "confidence": 0.3}}, Usage(cost_usd=0.0001)

    async def chat(self, model, messages, **kw):
        self.chats += 1
        return self.claude, Usage(cost_usd=0.01)

    async def aclose(self):
        pass


class FakeDiscord:
    def __init__(self):
        self.sent = []

    async def send(self, ch, text, key=None, min_interval_s=0):
        self.sent.append((ch, text))
        return True

    async def aclose(self):
        pass


def make_engine(tmp_path, cfg, ai=None):
    eng = Engine(cfg, load_secrets(SECRETS), "shadow")
    eng.data_dir = tmp_path
    eng.ev = EventLog(tmp_path / "logs", echo=False)
    eng.articles, eng.trades = articles(tmp_path), trades(tmp_path)
    eng.state_path = tmp_path / "state" / "position.json"
    eng.toss, eng.universe, eng.rtpr, eng.ai, eng.discord = FakeToss(), FakeUniverse(), FakeRtpr(), ai or FakeAI(), FakeDiscord()
    eng.executor = ShadowExecutor(cfg, eng.ev.write, 0.001, eng.toss.quote)
    t = now_ms()
    eng.windows = [("pre", t - 3_600_000, t + 3_600_000), ("regular", t + 3_600_000, t + 7_200_000)]
    return eng


def alert(age_s=5):
    t = now_ms()
    return Alert("ABC", "https://rtpr.io/a/ID1", "https://rtpr.io/a/ID1?sig=x", t - age_s * 1000, t)


def test_full_cycle_buy_split_sell(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    asyncio.run(eng.handle_alert(alert()))
    rows = eng.articles.rows()
    assert rows[-1]["action"] == "buy" and rows[-1]["claude_decision"] == "BUY" and rows[-1]["jev_pass"] == "true"
    assert eng.pos is not None and eng.pos.qty > 0 and eng.state_path.exists()
    q1 = eng.pos.qty
    assert any(ch == "entries" and "매수 근거 리포트" in t for ch, t in eng.discord.sent)

    eng.toss.px = 2.0 * 1.035                     # +3.5% → 2차 매수
    asyncio.run(eng.manage_once())
    assert eng.pos.entry2_done and eng.pos.qty > q1
    eng.toss.px = eng.pos.avg_price * 1.105       # +10% → 1차 익절 (절반)
    asyncio.run(eng.manage_once())
    assert eng.pos.tp1_done and eng.pos.qty > 0
    eng.toss.px = eng.pos.avg_price * 1.21        # +20% → 나머지 전량
    asyncio.run(eng.manage_once())
    assert eng.pos is None and not eng.state_path.exists()
    t = eng.trades.rows()[-1]
    assert t["exit_reasons"] == "tp1|tp2" and float(t["pnl_usd"]) > 0 and t["mode"] == "shadow"


def test_jev_filter_skips_claude(tmp_path, cfg):
    ai = FakeAI(jev_choice="neutral")
    eng = make_engine(tmp_path, cfg, ai)
    asyncio.run(eng.handle_alert(alert()))
    assert ai.chats == 0 and eng.articles.rows()[-1]["action_reason"] == "jev_filtered"


def test_stale_alert_not_traded(tmp_path, cfg):
    ai = FakeAI()
    eng = make_engine(tmp_path, cfg, ai)
    asyncio.run(eng.handle_alert(alert(age_s=600)))
    assert ai.chats == 0 and eng.articles.rows()[-1]["action_reason"] == "stale_alert" and eng.pos is None


def test_claude_bad_json_does_not_buy(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg, FakeAI(claude="I think BUY"))
    asyncio.run(eng.handle_alert(alert()))
    assert eng.pos is None and eng.articles.rows()[-1]["action_reason"] == "claude_pass"
    assert any(ch == "warnings" for ch, _ in eng.discord.sent)


def test_duplicate_alert_ignored(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    a = alert()
    asyncio.run(eng.handle_alert(a))
    asyncio.run(eng.handle_alert(a))
    assert len(eng.articles.rows()) == 1


def test_stop_loss_and_daily_loss_limit(tmp_path, cfg):
    cfg["risk"]["daily_loss_limit_usd"] = 1.0
    eng = make_engine(tmp_path, cfg)
    asyncio.run(eng.handle_alert(alert()))
    eng.toss.px = eng.pos.avg_price * 0.94
    asyncio.run(eng.manage_once())
    assert eng.pos is None and eng.trades.rows()[-1]["exit_reasons"] == "stop_loss"
    assert eng.risk.realized_today < -1.0
    a2 = Alert("XYZ", "https://rtpr.io/a/ID2", "https://rtpr.io/a/ID2", now_ms(), now_ms())
    asyncio.run(eng.handle_alert(a2))
    assert eng.articles.rows()[-1]["action_reason"] == "risk:daily_loss_limit"


def test_restart_restores_position(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    asyncio.run(eng.handle_alert(alert()))
    qty = eng.pos.qty
    eng2 = make_engine(tmp_path, cfg)
    asyncio.run(eng2.restore_state())
    assert eng2.pos is not None and eng2.pos.qty == qty and eng2.pos.symbol == "ABC"


def test_no_sell_orders_when_all_markets_closed(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    asyncio.run(eng.handle_alert(alert()))
    eng.windows = []                               # 장이 모두 닫힘
    eng.toss.px = eng.pos.avg_price * 0.5          # 손절선 아래여도
    asyncio.run(eng.manage_once())
    assert eng.pos is not None and eng.pos.qty > 0  # 주문을 내지 않는다 (거부될 주문 반복 방지)
    assert any(ch == "critical" for ch, _ in eng.discord.sent)
