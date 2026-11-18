"""scripts/build_dataset.py — 네트워크 없이 캐시만으로 CSV 를 만드는지."""

import csv
import datetime as dt
import importlib.util
import json
from pathlib import Path

from hbwb.clock import ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_dataset", ROOT / "scripts" / "build_dataset.py")
bd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bd)

MIN = 60_000


def ms(y, mo, d, h, mi):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=ET).timestamp() * 1000)


def test_build_from_cache(tmp_path, cfg):
    pub = ms(2026, 6, 3, 9, 40)
    bodies = tmp_path / "bodies.jsonl"
    rows = [{"url": "https://rtpr.io/a/A1", "ticker": "abc", "pub_ms": pub, "status": 200,
             "text": "ABC wins big contract\nABC (NASDAQ) · GlobeNewswire · Jun 3, 2026\nbody"},
            {"url": "https://rtpr.io/a/L1", "ticker": "LSE1", "pub_ms": pub, "status": 200,
             "text": "UK news\nLSE1 (LSE) · RNS · Jun 3, 2026\nbody"}]
    bodies.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    class A:
        since = until = limit = None
    arts = bd.load_articles(bodies, A)
    assert [a["ticker"] for a in arts] == ["ABC"]          # LSE 기사는 뺀다

    stores = {k: bd.Store(tmp_path / f"{k}.jsonl") for k in ("info", "daily", "bars", "jev", "claude")}
    stores["info"].put("ABC", {"info": {"symbol": "ABC", "market": "NASDAQ", "securityType": "STOCK", "isCommonShare": True,
                                        "status": "ACTIVE", "sharesOutstanding": "10000000"}})
    stores["daily"].put("ABC", {"daily": [{"start_ms": ms(2026, 6, 2, 0, 0), "o": 2, "h": 2, "l": 2, "c": 2.0, "v": 1},
                                          {"start_ms": ms(2026, 6, 3, 0, 0), "o": 2, "h": 2.7, "l": 2, "c": 2.5, "v": 1}]})
    bars = [{"start_ms": pub - 5 * MIN + i * MIN, "o": 2.0, "h": 2.0, "l": 2.0, "c": 2.0, "v": 100} for i in range(5)]
    bars += [{"start_ms": pub + i * MIN, "o": 2.0 + 0.1 * i, "h": 2.05 + 0.1 * i, "l": 2.0 + 0.1 * i, "c": 2.05 + 0.1 * i, "v": 100}
             for i in range(10)]
    stores["bars"].put(arts[0]["k"], {"bars": bars})
    stores["jev"].put(arts[0]["k"], {"choice": "big_positive", "probs": {"big_positive": 0.7, "small_positive": 0.3}, "conf": 0.5})
    stores["claude"].put(arts[0]["k"], {"decision": "BUY", "grade": 5, "conf": 0.8, "catalyst": "contract"})

    out = tmp_path / "out.csv"
    assert bd.build(arts, cfg, stores, out, 0.001, 100.0) == 1
    r = list(csv.DictReader(open(out, encoding="utf-8")))[0]
    assert list(r) == bd.COLS
    assert r["session"] == "regular" and r["jev_pred"] == "BUY" and r["claude_pred"] == "BUY"
    assert r["tradeable"] == "yes" and r["label_win"] == "WIN" and r["label_buy"] == "BUY"
    assert r["label_surge30"] == "YES" and float(r["market_cap_usd"]) == 20_000_000
    assert float(r["price_at_news"]) == 2.0                 # 기사 전에 끝난 봉의 종가 (미래 정보 없음)


def test_store_resumes(tmp_path):
    s = bd.Store(tmp_path / "x.jsonl")
    s.put("a", {"v": 1})
    s.put("a", {"v": 2})
    assert bd.Store(tmp_path / "x.jsonl").d["a"]["v"] == 2
