"""엔진 한 바퀴 (shadow): 기사 → 종목 범위 → Jev → Claude → 5분 안 +5% 신호 → 칸 매수 → 사다리 매도 → 거래 기록."""

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
MIN = 60_000


class FakeToss:
    def __init__(self, ref=2.0):
        self.ref, self.px = ref, {}
        self.default = ref * 1.06          # 기본: 이미 +6% (바로 신호)

    def price(self, s):
        return self.px.get(s, self.default)

    async def last_prices(self, syms):
        return {s: self.price(s) for s in syms}

    async def quote(self, sym):
        p = self.price(sym)
        return Quote(p * 0.999, p * 1.001, now_ms())

    async def candles(self, sym, interval="1m", count=40, before=None):
        t = now_ms()
        return [{"start_ms": t - 3 * MIN, "o": self.ref, "h": self.ref, "l": self.ref, "c": self.ref, "v": 1}]

    async def recent_trades(self, sym, count=50):
        return []

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
    cfg["entry"]["poll_interval_s"] = 0
    eng = Engine(cfg, load_secrets(SECRETS), "shadow")
    eng.data_dir = tmp_path
    eng.ev = EventLog(tmp_path / "logs", echo=False)
    eng.articles, eng.trades = articles(tmp_path), trades(tmp_path)
    eng.state_path = tmp_path / "state" / "positions.json"
    eng.toss, eng.universe, eng.rtpr, eng.ai, eng.discord = FakeToss(), FakeUniverse(), FakeRtpr(), ai or FakeAI(), FakeDiscord()
    eng.executor = ShadowExecutor(cfg, eng.ev.write, 0.001, eng.toss.quote)
    t = now_ms()
    eng.windows = [("pre", t - 3_600_000, t + 3_600_000), ("regular", t + 3_600_000, t + 7_200_000)]
    eng.risk.roll_day("d", 100.0)
    return eng


def alert(ticker="ABC", aid="ID1", age_s=5):
    t = now_ms()
    return Alert(ticker, f"https://rtpr.io/a/{aid}", f"https://rtpr.io/a/{aid}?sig=x", t - age_s * 1000, t)


def age(eng, sym, seconds):
    """보유 시각을 과거로 돌린다 (시간 규칙 · 30초 유예 시험용)."""
    p = eng.positions[sym]
    for f in p.fills:
        f.t_ms -= seconds * 1000
    p.entry_ms -= seconds * 1000


def test_full_cycle_slot_buy_and_ladder(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    asyncio.run(eng.handle_alert(alert()))
    row = eng.articles.rows()[-1]
    assert row["action"] == "buy" and row["ref_source"] == "prev_bar_close" and float(row["signal_rise_pct"]) >= 5
    pos = eng.positions["ABC"]
    assert pos.slot == 0 and pos.qty * pos.avg_price <= 100 / 3 + 0.01      # 칸 금액(1/3)만
    assert any(ch == "entries" and "칸 1/3" in t and "매수 근거 리포트" in t for ch, t in eng.discord.sent)
    age(eng, "ABC", 60)
    base = pos.avg_price
    for mult, n_after in ((1.051, None), (1.101, None), (1.151, None)):
        eng.toss.px["ABC"] = base * mult
        asyncio.run(eng.manage_one("ABC"))
    assert eng.positions["ABC"].trailing
    eng.toss.px["ABC"] = base * 1.151 * 0.94                 # 고점 대비 −6% → 남은 물량
    asyncio.run(eng.manage_one("ABC"))
    assert "ABC" not in eng.positions and not eng.state_path.exists()
    t = eng.trades.rows()[-1]
    assert t["exit_reasons"] == "tp1|tp2|tp3|trailing_stop" and float(t["pnl_usd"]) > 0 and t["slot"] == "1"


def test_three_slots_then_full(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    for i, sym in enumerate(["AAA", "BBB", "CCC", "DDD"]):
        asyncio.run(eng.handle_alert(alert(sym, f"I{i}")))
    assert sorted(eng.positions) == ["AAA", "BBB", "CCC"]
    assert sorted(p.slot for p in eng.positions.values()) == [0, 1, 2]
    assert eng.articles.rows()[-1]["action_reason"] == "risk:slots_full"
    # 한 칸이 비면 다시 살 수 있다 (칸 잠금 없음)
    age(eng, "AAA", 60)
    eng.toss.px["AAA"] = eng.positions["AAA"].avg_price * 0.9
    asyncio.run(eng.manage_one("AAA"))
    assert "AAA" not in eng.positions
    asyncio.run(eng.handle_alert(alert("EEE", "I9")))
    assert "EEE" in eng.positions and eng.positions["EEE"].slot == 0


def test_no_trigger_within_5min(tmp_path, cfg):
    cfg["entry"]["trigger_window_min"] = 0.001                # 창을 아주 짧게 (시험용)
    eng = make_engine(tmp_path, cfg)
    eng.toss.default = 2.0 * 1.02                            # +2% 뿐
    asyncio.run(eng.handle_alert(alert()))
    assert eng.articles.rows()[-1]["action_reason"] == "no_trigger_5m" and not eng.positions and not eng.pending


def test_jev_filter_skips_claude(tmp_path, cfg):
    ai = FakeAI(jev_choice="neutral")
    eng = make_engine(tmp_path, cfg, ai)
    asyncio.run(eng.handle_alert(alert()))
    assert ai.chats == 0 and eng.articles.rows()[-1]["action_reason"] == "jev_filtered"


def test_stale_alert_not_traded(tmp_path, cfg):
    ai = FakeAI()
    eng = make_engine(tmp_path, cfg, ai)
    asyncio.run(eng.handle_alert(alert(age_s=600)))
    assert ai.chats == 0 and eng.articles.rows()[-1]["action_reason"] == "stale_alert" and not eng.positions


def test_claude_bad_json_does_not_buy(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg, FakeAI(claude="I think BUY"))
    asyncio.run(eng.handle_alert(alert()))
    assert not eng.positions and eng.articles.rows()[-1]["action_reason"] == "claude_pass"
    assert any(ch == "warnings" for ch, _ in eng.discord.sent)


def test_duplicate_alert_and_same_ticker_once_per_day(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    a = alert()
    asyncio.run(eng.handle_alert(a))
    asyncio.run(eng.handle_alert(a))
    assert len(eng.articles.rows()) == 1
    age(eng, "ABC", 60)
    eng.toss.px["ABC"] = eng.positions["ABC"].avg_price * 0.9
    asyncio.run(eng.manage_one("ABC"))
    asyncio.run(eng.handle_alert(alert("ABC", "ID2")))       # 같은 날 같은 종목 다른 기사
    assert eng.articles.rows()[-1]["action_reason"] == "risk:ticker_already_traded_today"


def test_consecutive_losses_stop_new_entries(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    for i, sym in enumerate(["AAA", "BBB", "CCC"]):
        asyncio.run(eng.handle_alert(alert(sym, f"L{i}")))
        age(eng, sym, 60)
        eng.toss.px[sym] = eng.positions[sym].avg_price * 0.9
        asyncio.run(eng.manage_one(sym))
    assert eng.risk.consecutive_losses == 3
    asyncio.run(eng.handle_alert(alert("DDD", "L9")))
    assert eng.articles.rows()[-1]["action_reason"] == "risk:consecutive_losses"
    assert any("3연패" in t for _, t in eng.discord.sent)


def test_restart_restores_positions(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    asyncio.run(eng.handle_alert(alert("AAA", "R1")))
    asyncio.run(eng.handle_alert(alert("BBB", "R2")))
    qty = {s: p.qty for s, p in eng.positions.items()}
    eng2 = make_engine(tmp_path, cfg)
    asyncio.run(eng2.restore_state())
    assert {s: p.qty for s, p in eng2.positions.items()} == qty
    assert sorted(p.slot for p in eng2.positions.values()) == [0, 1]


def test_no_sell_orders_when_all_markets_closed(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    asyncio.run(eng.handle_alert(alert()))
    eng.windows = []
    eng.toss.px["ABC"] = eng.positions["ABC"].avg_price * 0.5
    asyncio.run(eng.manage_one("ABC"))
    assert eng.positions["ABC"].qty > 0
    assert any(ch == "critical" for ch, _ in eng.discord.sent)


def test_loop_death_stops_engine_for_restart(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)

    async def ok():
        return None

    async def boom():
        raise ValueError("bug")

    async def forever():
        await asyncio.sleep(3600)
    eng.start_with_retry = ok
    eng.news_loop, eng.position_loop, eng.housekeeping_loop = boom, forever, forever
    try:
        asyncio.run(eng.run())
        raise AssertionError("should raise")
    except RuntimeError as e:
        assert "news" in str(e)
    assert any(ch == "critical" and "news" in t for ch, t in eng.discord.sent)


def test_rtpr_fatal_is_engine_fatal(tmp_path, cfg):
    from hbwb.engine.runner import EngineFatal
    from hbwb.news.rtpr import RtprFatal
    eng = make_engine(tmp_path, cfg)

    class R:
        async def alerts(self, on_event):
            raise RtprFatal("RTPR 구독이 끝났다")
            yield  # noqa: unreachable — 비동기 생성기로 만들기 위함
    eng.rtpr = R()
    try:
        asyncio.run(eng.news_loop())
        raise AssertionError("should raise")
    except EngineFatal:
        pass


def test_start_retry_fatal_on_toss_auth(tmp_path, cfg):
    from hbwb.broker.toss import TossError
    from hbwb.engine.runner import EngineFatal
    eng = make_engine(tmp_path, cfg)

    async def bad():
        raise TossError(403, "ip-not-allowed", "x")
    eng.preflight = bad
    try:
        asyncio.run(eng.start_with_retry())
        raise AssertionError("should raise")
    except EngineFatal:
        pass
    assert any(ch == "critical" for ch, _ in eng.discord.sent)


def test_preflight_restores_risk_and_seen(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    asyncio.run(eng.handle_alert(alert("AAA", "S1")))
    age(eng, "AAA", 60)
    eng.toss.px["AAA"] = eng.positions["AAA"].avg_price * 0.9
    asyncio.run(eng.manage_one("AAA"))
    eng2 = make_engine(tmp_path, cfg)

    async def cal():
        return {}
    eng2.toss.us_calendar = cal
    eng2.toss.buying_power_usd = None
    asyncio.run(eng2.preflight())
    assert eng2.risk.consecutive_losses == 1 and eng2.risk.realized_today < 0
    assert "S1" in eng2.seen
    assert eng2.risk.block_reason("AAA", __import__("hbwb.engine.runner", fromlist=["x"]).us_day(now_ms()), 100, 0) == \
        "ticker_already_traded_today"
