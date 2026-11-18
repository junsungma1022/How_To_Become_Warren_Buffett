from hbwb.strategy.rules import Fill, Position, entry2_triggered, exit_decision, plan_entry1, shares_for, update_peak

MIN = 60_000


def pos_with(qty, price, t=0, **kw):
    p = Position("ABC", 100.0)
    p.add(Fill("BUY", qty, price, t, "entry1"))
    p.peak = price
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_shares_for_rounds_down_with_fee():
    assert shares_for(100, 10, 0.001) == 9          # 10 주면 수수료 때문에 100 을 넘는다
    assert shares_for(100, 0, 0.001) == 0
    assert shares_for(0, 5, 0.0) == 0


def test_plan_entry1_split_and_single(cfg):
    q1, total, limit = plan_entry1(100, 2.0, cfg, 0.001)
    assert abs(limit - 2.02) < 1e-9
    assert total == 49 and q1 == 24                 # 50% 내림
    q1, total, _ = plan_entry1(100, 60.0, cfg, 0.001)
    assert (q1, total) == (1, 1)                    # 1주는 나눌 수 없다
    q1, total, _ = plan_entry1(100, 150.0, cfg, 0.001)
    assert (q1, total) == (0, 0)                    # 못 산다


def test_entry2_only_on_rise_within_window(cfg):
    p = pos_with(10, 1.0, t=0)
    assert not entry2_triggered(p, 1.02, 1 * MIN, cfg)
    assert entry2_triggered(p, 1.031, 1 * MIN, cfg)
    assert not entry2_triggered(p, 1.031, 11 * MIN, cfg)      # 10분 지나면 안 한다
    assert not entry2_triggered(p, 0.90, 1 * MIN, cfg)        # 물타기 없음
    p.tp1_done = True
    assert not entry2_triggered(p, 1.05, 1 * MIN, cfg)        # 익절 뒤에는 안 한다


def test_stop_loss_sells_all_and_has_priority(cfg):
    p = pos_with(10, 1.0)
    ex = exit_decision(p, 0.95, 40 * MIN, 0, cfg)             # 손절 + 시간 + 장마감 모두 해당 → 손절
    assert ex.qty == 10 and ex.reason == "stop_loss"


def test_tp1_partial_then_tp2(cfg):
    p = pos_with(10, 1.0)
    ex = exit_decision(p, 1.10, MIN, None, cfg)
    assert (ex.qty, ex.reason) == (5, "tp1")
    p.add(Fill("SELL", 5, 1.10, MIN, "tp1"))
    assert p.tp1_done and p.qty == 5
    update_peak(p, 1.15)
    assert exit_decision(p, 1.15, 2 * MIN, None, cfg) is None
    ex = exit_decision(p, 1.20, 3 * MIN, None, cfg)
    assert (ex.qty, ex.reason) == (5, "tp2")


def test_trailing_stop_after_tp1(cfg):
    p = pos_with(10, 1.0)
    p.add(Fill("SELL", 5, 1.10, MIN, "tp1"))
    update_peak(p, 1.18)
    assert exit_decision(p, 1.13, 2 * MIN, None, cfg) is None
    ex = exit_decision(p, 1.12, 2 * MIN, None, cfg)            # 1.18 × 0.95 = 1.121
    assert (ex.qty, ex.reason) == (5, "trailing_stop")


def test_no_trailing_before_tp1(cfg):
    p = pos_with(10, 1.0)
    update_peak(p, 1.08)
    assert exit_decision(p, 1.02, MIN, None, cfg) is None


def test_one_share_tp1_sells_all(cfg):
    p = pos_with(1, 1.0)
    ex = exit_decision(p, 1.11, MIN, None, cfg)
    assert (ex.qty, ex.reason) == (1, "tp1_all_1share")


def test_time_limit_and_session_close(cfg):
    p = pos_with(4, 1.0, t=0)
    assert exit_decision(p, 1.01, 29 * MIN, None, cfg) is None
    assert exit_decision(p, 1.01, 30 * MIN, None, cfg).reason == "time_limit"
    assert exit_decision(p, 1.01, 10 * MIN, 14 * MIN, cfg).reason == "session_close"   # 끝 5분 전


def test_realized_pnl_includes_fees(cfg):
    p = Position("X", 100)
    p.add(Fill("BUY", 10, 1.0, 0, "entry1", fee=0.01))
    p.add(Fill("SELL", 10, 1.1, MIN, "tp2", fee=0.011))
    assert abs(p.realized_pnl() - (1.0 - 0.021)) < 1e-9
    assert p.closed
