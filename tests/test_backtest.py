import datetime as dt

from hbwb.clock import ET
from hbwb.strategy.backtest import simulate

MIN = 60_000
PUB = int(dt.datetime(2026, 11, 18, 11, 0, tzinfo=ET).timestamp() * 1000)


def bars(seq, start=PUB - 2 * MIN):
    """seq: [(o,h,l,c)] 1분씩, 발행 2분 전 봉부터."""
    return [{"start_ms": start + i * MIN, "o": o, "h": h, "l": l, "c": c, "v": 100} for i, (o, h, l, c) in enumerate(seq)]


FLAT = (1.0, 1.0, 1.0, 1.0)


def test_no_trigger_means_no_trade(cfg):
    r = simulate(bars([FLAT] * 2 + [(1.0, 1.04, 1.0, 1.03)] * 10), PUB, cfg, 100 / 3, 0.001)
    assert not r.triggered and r.why == "no_trigger" and r.ref_price == 1.0


def test_trigger_after_window_ignored(cfg):
    seq = [FLAT] * 2 + [FLAT] * 5 + [(1.0, 1.2, 1.0, 1.2)]       # 발행 5분 뒤에야 +20%
    assert not simulate(bars(seq), PUB, cfg, 100 / 3, 0.001).triggered


def test_trigger_then_ladder_win(cfg):
    # 발행 2분째에 +5% 신호 → 사다리 익절
    seq = [FLAT] * 2 + [FLAT, (1.0, 1.06, 1.0, 1.06), (1.06, 1.12, 1.06, 1.11), (1.11, 1.30, 1.11, 1.29),
                        (1.29, 1.30, 1.20, 1.21)] + [(1.21, 1.21, 1.21, 1.21)] * 3
    r = simulate(bars(seq), PUB, cfg, 100 / 3, 0.001)
    assert r.triggered and r.win
    assert abs(r.entry_price - 1.05 * 1.007) < 1e-6
    assert r.exit_reasons.startswith("tp")


def test_trigger_then_stop_loss(cfg):
    seq = [FLAT] * 2 + [FLAT, (1.0, 1.06, 1.0, 1.05), (1.05, 1.05, 0.95, 0.96)] + [FLAT] * 3
    r = simulate(bars(seq), PUB, cfg, 100 / 3, 0.001)
    assert r.triggered and not r.win and r.exit_reasons == "stop_loss"


def test_time_stop_5m(cfg):
    seq = [FLAT] * 2 + [FLAT, (1.0, 1.06, 1.0, 1.06)] + [(1.06, 1.065, 1.06, 1.06)] * 10
    r = simulate(bars(seq), PUB, cfg, 100 / 3, 0.001)
    assert r.triggered and r.exit_reasons == "time_stop_5m"
