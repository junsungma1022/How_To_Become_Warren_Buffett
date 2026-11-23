"""2026-11-23 사용자 승인 변경 (D-12 ~ D-15): 하루 경계 10:00 · 애프터 끝 보정 · 시작 점검 · 매수 상한 +10%."""

import asyncio
import datetime as dt

import pytest

from hbwb.broker.toss import OrderState, TossError, sessions_from_calendar_day, toss_after_end
from hbwb.clock import ET, KST
from hbwb.config import _validate, day_hour
from hbwb.engine.runner import report_day_key
from hbwb.strategy.backtest import simulate
from hbwb.strategy.rules import entry_signal

from test_engine_flow import alert, make_engine

MIN = 60_000


def ms(y, mo, d, h, mi, tz):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)


# ── D-12 하루 경계 KST 10:00 ──────────────────────────────────────────────
def test_day_boundary_is_10_kst(cfg):
    assert day_hour(cfg) == 10
    # 겨울 애프터 마감 직전 (KST 09:45 = ET 19:45) 은 아직 전날
    assert report_day_key(ms(2026, 11, 24, 9, 45, KST)) == "2026-11-23"
    assert report_day_key(ms(2026, 11, 24, 10, 0, KST)) == "2026-11-24"


def test_daily_time_must_be_on_the_hour(cfg):
    cfg["report"]["daily_time_kst"] = "10:30"
    with pytest.raises(ValueError, match="daily_time_kst"):
        _validate(cfg)


# ── D-13 애프터 끝 보정 (토스 = 거래소 − 10분) ────────────────────────────
def test_after_end_cut_only_when_on_the_hour():
    assert toss_after_end(ms(2026, 11, 23, 20, 0, ET)) == ms(2026, 11, 23, 19, 50, ET)
    assert toss_after_end(ms(2026, 11, 27, 17, 0, ET)) == ms(2026, 11, 27, 16, 50, ET)     # 조기마감 날
    assert toss_after_end(ms(2026, 11, 23, 19, 50, ET)) == ms(2026, 11, 23, 19, 50, ET)    # 이미 토스 시각


def test_calendar_after_window_adjusted_regular_untouched():
    iso = lambda h, m: dt.datetime(2026, 11, 23, h, m, tzinfo=ET).isoformat()
    day = {"regularMarket": {"startTime": iso(9, 30), "endTime": iso(16, 0)},
           "afterMarket": {"startTime": iso(16, 0), "endTime": iso(20, 0)}}
    w = sessions_from_calendar_day(day)
    assert w["regular"][1] == ms(2026, 11, 23, 16, 0, ET)
    assert w["after"][1] == ms(2026, 11, 23, 19, 50, ET)


# ── D-15 매수 상한 +10% ───────────────────────────────────────────────────
@pytest.mark.parametrize("rise,expect", [(4.9, None), (5.0, "signal"), (9.9, "signal"), (10.0, "signal"), (10.5, "over_cap")])
def test_entry_band_5_to_10(cfg, rise, expect):
    assert cfg["entry"]["trigger_max_rise_pct"] == 10.0
    assert entry_signal(100.0, 100.0 * (1 + rise / 100), 1_000, 0, cfg) == expect


def test_cap_null_means_no_cap(cfg):
    cfg["entry"]["trigger_max_rise_pct"] = None
    assert entry_signal(100.0, 140.0, 1_000, 0, cfg) == "signal"


def test_cap_must_be_above_trigger(cfg):
    cfg["entry"]["trigger_max_rise_pct"] = 5.0
    with pytest.raises(ValueError, match="trigger_max_rise_pct"):
        _validate(cfg)


def test_engine_does_not_chase_above_cap(tmp_path, cfg):
    cfg["entry"]["trigger_window_min"] = 0.005                # 0.3초 창 (시험용) · 기사는 방금 나옴
    eng = make_engine(tmp_path, cfg)
    eng.toss.default = 2.0 * 1.25                             # 이미 +25%
    asyncio.run(eng.handle_alert(alert(age_s=0)))
    row = eng.articles.rows()[-1]
    assert row["action_reason"] == "over_cap_5m" and float(row["over_cap_max_pct"]) >= 24.9
    assert not eng.positions and not eng.pending


def test_engine_buys_inside_band(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    eng.toss.default = 2.0 * 1.08
    asyncio.run(eng.handle_alert(alert()))
    assert eng.articles.rows()[-1]["action"] == "buy"


def test_backtest_records_entry_rise_without_cap(cfg):
    pub = 10 * MIN
    bars = [{"start_ms": pub - MIN, "o": 1, "h": 1, "l": 1, "c": 1.0, "v": 1}]
    bars += [{"start_ms": pub + i * MIN, "o": 1.3, "h": 1.35, "l": 1.28, "c": 1.3, "v": 1} for i in range(1, 30)]
    r = simulate(bars, pub, cfg, 33.0, 0.001)
    assert r.triggered and r.entry_rise_pct == pytest.approx(30.0)   # 상한은 백테스트에 적용하지 않는다 (KNIME 이 정한다)


# ── D-14 시작 점검 (계좌 ↔ 기록) ──────────────────────────────────────────
class AcctToss:
    def __init__(self, orders, items):
        self.orders, self.items, self.cancelled = orders, items, []

    async def open_orders(self, symbol=None):
        return self.orders

    async def cancel(self, oid):
        self.cancelled.append(oid)
        return oid

    async def holdings(self, symbol=None):
        return {"items": self.items}


def order(oid, sym, side="BUY"):
    return OrderState(oid, sym, side, "PENDING", 3, 0, None, None, 1.0, {})


def test_reconcile_cancels_own_orders_and_halts_on_orphans(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg)
    eng.ev.write("order_sent", side="BUY", symbol="ABC", order_id="O-MINE")
    eng.toss = AcctToss([order("O-MINE", "ABC"), order("O-USER", "TSLA")],
                        [{"symbol": "XYZ", "quantity": "7", "marketCountry": "US", "currency": "USD"},
                         {"symbol": "005930", "quantity": "1", "marketCountry": "KR", "currency": "KRW"}])
    asyncio.run(eng.reconcile_account())
    assert eng.toss.cancelled == ["O-MINE"]                    # 내 주문만 취소 · 사람 주문은 건드리지 않는다
    assert eng.risk.halted and "XYZ 7주" in eng.risk.halted and "TSLA" in eng.risk.halted
    assert "005930" not in eng.risk.halted                     # 국내 주식은 무관
    assert any(ch == "critical" and "신규 매수 멈춤" in t for ch, t in eng.discord.sent)
    assert eng.risk.block_reason("NEW", "2026-11-23", 100.0, 0).startswith("halted:")


def test_reconcile_clean_account_and_ignore_list(tmp_path, cfg):
    cfg["reconcile"] = {"ignore_symbols": ["AAPL"]}
    eng = make_engine(tmp_path, cfg)
    eng.toss = AcctToss([], [{"symbol": "AAPL", "quantity": "2", "marketCountry": "US", "currency": "USD"}])
    asyncio.run(eng.reconcile_account())
    assert eng.risk.halted is None and not eng.discord.sent
