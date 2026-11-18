from hbwb.strategy.backtest import simulate

MIN = 60_000


def bars_from(prices, t0=0):
    """prices: [(o,h,l,c)] 1분씩."""
    return [{"start_ms": t0 + i * MIN, "o": o, "h": h, "l": l, "c": c, "v": 1000} for i, (o, h, l, c) in enumerate(prices)]


def test_untradeable_when_no_bars_after_news(cfg):
    bars = bars_from([(1, 1, 1, 1)] * 3, t0=-10 * MIN)
    r = simulate(bars, 0, cfg, 100, 0.001)
    assert not r.tradeable


def test_stop_loss_counts_first_in_bar(cfg):
    # 진입 봉 (t=1분) 안에서 저가가 -5% 아래, 고가가 +10% 위 → 보수적으로 손절
    bars = bars_from([(1, 1, 1, 1), (1.0, 1.2, 0.9, 1.0)] + [(1, 1, 1, 1)] * 5)
    r = simulate(bars, 0, cfg, 100, 0.001)
    assert r.tradeable and not r.win and r.exit_reasons.startswith("stop_loss")


def test_split_take_profit_win(cfg):
    seq = [(1, 1, 1, 1), (1.0, 1.0, 1.0, 1.0), (1.0, 1.04, 1.0, 1.04), (1.04, 1.12, 1.04, 1.11), (1.11, 1.25, 1.11, 1.24), (1.24, 1.31, 1.24, 1.30)]
    r = simulate(bars_from(seq), 0, cfg, 100, 0.001)
    assert r.tradeable and r.win
    assert "tp1" in r.exit_reasons and "tp2" in r.exit_reasons.split("|")[-1]
    assert r.did_entry2                            # +3% 위로 가서 2차 매수
    # 2차 매수로 평균가가 올라 tp2(평균가 +20%)는 1.28 위에서 닿는다
    assert r.exit_reasons == "tp1|tp2"


def test_time_limit_exit(cfg):
    seq = [(1, 1, 1, 1)] + [(1.0, 1.01, 0.99, 1.0)] * 40
    r = simulate(bars_from(seq), 0, cfg, 100, 0.001)
    assert r.tradeable and r.exit_reasons == "time_limit" and not r.win   # 수수료만큼 손해
