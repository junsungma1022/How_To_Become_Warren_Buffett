import datetime as dt

from hbwb.clock import ET
from hbwb.engine import sessions as S
from hbwb.news.rtpr import parse_article_text, parse_ws_text, strip_query
from hbwb.universe import judge, prev_close_from_daily

MIN = 60_000


def test_rtpr_ws_messages():
    alerts, ping, other = parse_ws_text(
        '[{"type":"ping"},{"type":"alert","ticker":"abcd","article_published_at":"2026-11-18T13:00:00.100Z",'
        '"article_url":"https://rtpr.io/a/XYZ?exp=1&sig=2"},{"type":"hello"}]', 1_000)
    assert ping and len(alerts) == 1 and other == [{"type": "hello"}]
    a = alerts[0]
    assert a.ticker == "ABCD" and a.url == "https://rtpr.io/a/XYZ" and a.article_id == "XYZ"
    assert a.raw_url.endswith("sig=2") and a.published_ms is not None
    assert parse_ws_text("not json", 0)[2][0]["type"] == "parse_error"
    assert strip_query("u?x=1") == "u"


def test_rtpr_article_text():
    title, exch = parse_article_text("# Big News for ABC\n\nABC (NASDAQ) · GlobeNewswire · Nov 18, 2026\nbody")
    assert title == "Big News for ABC" and exch == "NASDAQ"


INFO = {"symbol": "ABC", "market": "NASDAQ", "securityType": "STOCK", "isCommonShare": True, "status": "ACTIVE",
        "sharesOutstanding": "10000000", "englishName": "ABC Inc"}


def test_universe_judge(cfg):
    # D-19: 지금 설정은 시총 제한 없음 → 큰 회사도 통과 · 주식 수 없어도 통과 (시총만 빈칸)
    assert judge(INFO, 40.0, cfg).ok and judge(INFO, 40.0, cfg).market_cap == 4e8
    nos = judge({**INFO, "sharesOutstanding": None}, 2.0, cfg)
    assert nos.ok and nos.market_cap is None
    # 예전 범위 ($5M ~ $300M) 를 다시 넣으면 예전처럼 거른다
    cfg["universe"].update(min_market_cap_usd=5_000_000, max_market_cap_usd=300_000_000)
    assert judge(INFO, 2.0, cfg).ok                                  # $20M
    assert judge(INFO, 40.0, cfg).reason == "market_cap_out"          # $400M
    assert judge(INFO, None, cfg).reason == "unclassified:no_prev_close"
    assert judge(None, 2.0, cfg).reason == "unclassified:no_stock_info"
    assert judge({**INFO, "market": "US_ETC"}, 2.0, cfg).reason == "market:US_ETC"
    assert judge({**INFO, "securityType": "ETF"}, 2.0, cfg).reason == "security_type:ETF"
    assert judge({**INFO, "sharesOutstanding": None}, 2.0, cfg).reason == "unclassified:no_shares"


def test_prev_close_uses_earlier_day_only():
    def day(d):
        return int(dt.datetime(2026, 11, d, tzinfo=ET).timestamp() * 1000)
    daily = [{"start_ms": day(16), "c": 1.0}, {"start_ms": day(17), "c": 2.0}, {"start_ms": day(18), "c": 3.0}]
    ref = int(dt.datetime(2026, 11, 18, 7, 0, tzinfo=ET).timestamp() * 1000)
    assert prev_close_from_daily(daily, ref) == 2.0


def test_trading_block_end(cfg):
    w = [("day", 0, 100 * MIN), ("pre", 110 * MIN, 200 * MIN), ("regular", 200 * MIN, 300 * MIN), ("after", 300 * MIN, 400 * MIN)]
    assert S.trading_block_end(w, 150 * MIN, cfg) == 400 * MIN         # 프리 → 정규 → 애프터 끝까지
    assert S.trading_block_end(w, 50 * MIN, cfg) is None               # 데이마켓은 매매 안 함
    assert S.current(w, 105 * MIN) is None
