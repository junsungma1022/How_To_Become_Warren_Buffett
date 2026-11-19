import datetime as dt

from hbwb.clock import ET
from hbwb.strategy.risk import RiskBook
from hbwb.strategy.rules import (Fill, Position, apply_exit, entry_signal, exit_decision, plan_entry, ref_from_bars,
                                 shares_for, slot_budget, update_peak)

MIN = 60_000
T0 = int(dt.datetime(2026, 11, 18, 11, 0, tzinfo=ET).timestamp() * 1000)    # ET 11:00 (개장 직후 창 밖)


def pos_with(qty, price, t=T0):
    p = Position("ABC", 0)
    p.add(Fill("BUY", qty, price, t, "entry"))
    return p


def step(p, price, t, cfg):
    """한 번 점검 → 팔면 체결까지 반영."""
    update_peak(p, price)
    ex = exit_decision(p, price, t, None, cfg)
    if ex:
        p.add(Fill("SELL", ex.qty, price, t, ex.reason))
        apply_exit(p, ex)
    return ex


def test_slot_budget_is_one_third(cfg):
    assert abs(slot_budget(100, 100, cfg) - 100 / 3) < 1e-9
    assert abs(slot_budget(150, 150, cfg) - 100 / 3) < 1e-9          # 예산 $100 을 넘게 쓰지 않는다
    assert abs(slot_budget(90, 90, cfg) - 30) < 1e-9                 # 자산이 줄면 칸도 준다
    assert slot_budget(100, 10, cfg) == 10                           # 현금보다 크지 않게


def test_plan_entry_limit_premium(cfg):
    qty, limit = plan_entry(100 / 3, 2.0, cfg, 0.001)
    assert abs(limit - 2.014) < 1e-9 and qty == shares_for(100 / 3, 2.014, 0.001) == 16
    assert plan_entry(100 / 3, 40.0, cfg, 0.001)[0] == 0             # 칸 금액으로 1주도 못 산다


def test_entry_signal_5pct_within_5min(cfg):
    assert entry_signal(1.0, 1.04, T0 + MIN, T0, cfg) is None
    assert entry_signal(1.0, 1.05, T0 + MIN, T0, cfg) == "signal"
    assert entry_signal(1.0, 1.20, T0 + 5 * MIN + 1, T0, cfg) == "expired"
    assert entry_signal(None, 1.20, T0 + MIN, T0, cfg) is None


def test_ref_from_bars_uses_last_bar_ended_before_publish(cfg):
    bars = [{"start_ms": T0 - 3 * MIN, "c": 1.0}, {"start_ms": T0 - MIN, "c": 1.1}, {"start_ms": T0, "c": 9.9}]
    assert ref_from_bars(bars, T0, cfg) == 1.1
    assert ref_from_bars([{"start_ms": T0 - 40 * MIN, "c": 1.0}], T0, cfg) is None   # 30분 넘게 낡음


def test_stop_loss_always_and_grace(cfg):
    p = pos_with(10, 1.0)
    assert exit_decision(p, 0.93, T0 + 5_000, None, cfg).reason == "stop_loss"     # 30초 안에도 손절
    assert exit_decision(p, 1.20, T0 + 5_000, None, cfg) is None                    # 30초 안에는 익절 안 봄


def test_time_stops(cfg):
    p = pos_with(10, 1.0)
    assert exit_decision(p, 1.019, T0 + 5 * MIN + 1000, None, cfg).reason == "time_stop_5m"
    assert exit_decision(p, 1.03, T0 + 6 * MIN, None, cfg) is None
    assert exit_decision(p, 1.049, T0 + 15 * MIN + 1000, None, cfg).reason == "time_stop_15m"


def test_take_profit_ladder_then_trailing(cfg):
    p = pos_with(10, 1.0)
    t = T0 + MIN
    assert step(p, 1.05, t, cfg).reason == "tp1" and p.qty == 7
    assert step(p, 1.06, t + 1000, cfg) is None
    assert step(p, 1.10, t + 2000, cfg).reason == "tp2" and p.qty == 4
    assert not p.trailing
    assert step(p, 1.16, t + 3000, cfg).reason == "tp3" and p.qty == 1 and p.trailing
    assert step(p, 1.25, t + 4000, cfg) is None                         # 고점 갱신
    assert step(p, 1.25 * 0.95, t + 5000, cfg).reason == "trailing_stop" and p.closed


def test_jump_straight_to_tp3_sells_levels_one_per_check(cfg):
    p = pos_with(10, 1.0)
    t = T0 + MIN
    reasons = [step(p, 1.16, t + i * 1000, cfg).reason for i in range(3)]
    assert reasons == ["tp3", "tp2", "tp1"] and p.qty == 1 and p.trailing


def test_open_window_trailing_is_tighter(cfg):
    t930 = int(dt.datetime(2026, 11, 18, 9, 30, tzinfo=ET).timestamp() * 1000)
    p = pos_with(10, 1.0, t=t930 - 10 * MIN)
    p.trailing, p.tp_done, p.peak = True, [0, 1, 2], 1.30
    p.fills.append(Fill("SELL", 9, 1.2, t930, "tp"))
    assert exit_decision(p, 1.30 * 0.965, t930, None, cfg).reason == "trailing_stop"    # −3% 창
    assert exit_decision(p, 1.30 * 0.965, t930 + 30 * MIN, None, cfg) is None           # 창 밖은 −5%


def test_small_quantity_ladder(cfg):
    p = pos_with(2, 1.0)
    assert step(p, 1.05, T0 + MIN, cfg).qty == 1
    assert step(p, 1.10, T0 + MIN + 1000, cfg).qty == 1 and p.closed


def test_session_close(cfg):
    p = pos_with(4, 1.0)
    assert exit_decision(p, 1.01, T0 + MIN, T0 + 5 * MIN, cfg).reason == "session_close"


def test_risk_percent_rules(cfg):
    r = RiskBook(cfg)
    r.roll_day("d1", 100.0)
    assert r.block_reason("A", "x", 100.0, 3) == "slots_full"
    assert r.block_reason("A", "x", 100.0, 2) is None
    r.record_close(-6.0)
    assert r.block_reason("A", "x", 94.0, 0) is None
    r.record_close(-4.5)                                     # 그날 −10.5% → 중단
    assert r.block_reason("A", "x", 89.5, 0) == "daily_loss_limit"
    r.roll_day("d2", 89.5)
    assert r.block_reason("A", "x", 89.5, 0) is None
    for _ in range(3):
        r.record_close(-0.1)
    assert r.block_reason("A", "x", 89.0, 0) == "consecutive_losses"
    r.roll_day("d3", 89.0)
    r.record_open("A", "x")
    assert r.block_reason("A", "x", 89.0, 0) == "ticker_already_traded_today"
    assert r.block_reason("A", "y", 59.0, 0).startswith("halted:drawdown")   # 시작 $100 대비 −41%
    assert r.block_reason("B", "y", 100.0, 0).startswith("halted")           # 정지는 사람이 풀 때까지


def test_risk_rebuild_after_restart(cfg):
    from hbwb.clock import parse_iso_ms
    from hbwb.engine.runner import report_day_key, us_day
    t = int(dt.datetime(2026, 11, 18, 23, 40, tzinfo=dt.timezone.utc).timestamp() * 1000)   # KST 11-19 08:40 → 하루 11-18
    iso = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()       # noqa: E731
    trades = [{"ticker": "OLD", "entry_utc": iso(t - 86_400_000), "exit_utc": iso(t - 86_400_000), "pnl_usd": "-5"},
              {"ticker": "A", "entry_utc": iso(t - 3 * MIN), "exit_utc": iso(t - 2 * MIN), "pnl_usd": "2"},
              {"ticker": "B", "entry_utc": iso(t - 2 * MIN), "exit_utc": iso(t - MIN), "pnl_usd": "-1"},
              {"ticker": "C", "entry_utc": iso(t - MIN), "exit_utc": iso(t), "pnl_usd": "-1.5"}]
    r = RiskBook(cfg)
    r.roll_day(report_day_key(t), 100.0)
    r.rebuild(trades, report_day_key, us_day, parse_iso_ms, [("D", us_day(t))])
    assert abs(r.realized_today - (-0.5)) < 1e-9          # 전날 거래(-5)는 빼고 오늘 것만
    assert r.consecutive_losses == 2                      # 끝에서부터 B · C
    assert r.block_reason("C", us_day(t), 100.0, 0) == "ticker_already_traded_today"
    assert r.block_reason("D", us_day(t), 100.0, 0) == "ticker_already_traded_today"
