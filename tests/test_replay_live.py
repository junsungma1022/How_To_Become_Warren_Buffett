"""D-20: paper 기사 다시 보기 — 가격 경로로 '샀다면' 을 계산 (AI 호출 없음)."""

import asyncio
import csv
import datetime as dt
import importlib.util
from pathlib import Path

from hbwb.ledger import ARTICLE_COLS

ROOT = Path(__file__).resolve().parents[1]
MIN = 60_000


def _load():
    spec = importlib.util.spec_from_file_location("replay_live", ROOT / "scripts" / "replay_live.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_replay_marks_band_signal(tmp_path, monkeypatch, cfg):
    rl = _load()
    pub = int(dt.datetime(2026, 11, 27, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5))).timestamp() * 1000)
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    with open(led / "articles.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ARTICLE_COLS)
        w.writeheader()
        w.writerow({"article_id": "A", "ticker": "UP", "published_utc": iso(pub), "received_utc": iso(pub + 1000),
                    "universe_ok": "true", "jev_choice": "big_positive", "jev_pass": "true", "claude_called": "true",
                    "claude_decision": "PASS", "claude_grade": "4"})
        w.writerow({"article_id": "B", "ticker": "FLAT", "published_utc": iso(pub), "received_utc": iso(pub + 2000),
                    "universe_ok": "true", "jev_choice": "neutral", "jev_pass": "false"})

    class FakeToss:
        def __init__(self, *a, **k):
            pass

        async def candles(self, sym, interval, count, before=None):
            bars = [{"start_ms": pub - MIN, "o": 1, "h": 1, "l": 1, "c": 1.0, "v": 1}]
            for i in range(1, 60):
                p = 1.07 if (sym == "UP" and i >= 1) else 1.0
                if sym == "UP" and i >= 3:
                    p = 1.25                                          # 그 뒤 크게 오름 → 익절
                bars.append({"start_ms": pub + i * MIN, "o": p, "h": p, "l": p, "c": p, "v": 1})
            return bars

        async def aclose(self):
            pass

    monkeypatch.setattr(rl, "TossClient", FakeToss)
    monkeypatch.setattr(rl, "ROOT", tmp_path)
    monkeypatch.setattr(rl, "load_settings", lambda: cfg)
    monkeypatch.setattr(rl, "load_dotenv_file", lambda: None)
    monkeypatch.setattr(rl, "load_secrets", lambda: type("S", (), {"get": lambda self, k, required=True: "x"})())
    import argparse
    assert asyncio.run(rl.main(argparse.Namespace(only_claude=False))) == 0
    rows = {r["ticker"]: r for r in csv.DictReader(open(tmp_path / "data" / "knime" / "live_replay.csv", encoding="utf-8"))}
    assert rows["UP"]["triggered"] == "yes" and rows["UP"]["in_band"] == "yes" and rows["UP"]["label_win"] == "WIN"
    assert rows["UP"]["claude_pred"] == "SKIP" and rows["UP"]["label_buy"] == "BUY"     # Claude 가 놓친 기회
    assert rows["FLAT"]["triggered"] == "no" and rows["FLAT"]["claude_pred"] == ""
