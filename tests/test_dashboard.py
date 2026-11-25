"""D-17: 대시보드 상태 · 실시간 보기 한 줄 · 지금 하루 날짜."""

import csv
import datetime as dt
import importlib.util
import json
from pathlib import Path

from hbwb.clock import KST
from hbwb.ledger import ARTICLE_COLS, TRADE_COLS
from hbwb.report.labels import reason_ko, universe_ko
from hbwb.report.live_state import build_state, window_start_ms

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def kst_ms(*a):
    return int(dt.datetime(*a, tzinfo=KST).timestamp() * 1000)


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_window_start_and_current_day():
    assert window_start_ms(kst_ms(2026, 11, 25, 18, 0), 10) == kst_ms(2026, 11, 25, 10, 0)
    assert window_start_ms(kst_ms(2026, 11, 26, 9, 30), 10) == kst_ms(2026, 11, 25, 10, 0)
    dr = _load("daily_report")
    assert dr.current_window_day(dt.datetime(2026, 11, 25, 18, 0, tzinfo=KST), 10) == dt.date(2026, 11, 26)
    assert dr.current_window_day(dt.datetime(2026, 11, 26, 9, 0, tzinfo=KST), 10) == dt.date(2026, 11, 26)


def test_labels():
    assert reason_ko("risk:slots_full") == "3칸 모두 사용 중"
    assert reason_ko("session_closed:closed") == "거래 세션 밖"
    assert universe_ko("security_type:ETF") == "보통주 아님 (ETF)"


def test_build_state_paper(tmp_path, cfg):
    now = kst_ms(2026, 11, 25, 18, 30)
    d = tmp_path / "data"
    (d / "state").mkdir(parents=True)
    (d / "ledger").mkdir()
    (tmp_path / "logs").mkdir()
    with open(d / "ledger" / "articles.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ARTICLE_COLS)
        w.writeheader()
        w.writerow({"article_id": "A", "ticker": "ABCD", "received_utc": iso(now - 60_000), "universe_ok": "true",
                    "jev_pass": "true", "claude_called": "true", "claude_decision": "BUY", "action": "buy",
                    "action_reason": "filled", "ai_cost_usd": "0.03", "mode": "paper", "title": "<b>x</b>"})
        w.writerow({"article_id": "B", "ticker": "SHEL", "received_utc": iso(now - 30_000), "universe_ok": "false",
                    "universe_reason": "market_cap_out", "action": "skip", "action_reason": "universe", "mode": "paper"})
        w.writerow({"article_id": "C", "ticker": "OLD", "received_utc": iso(kst_ms(2026, 11, 25, 9, 0)), "action": "skip",
                    "action_reason": "jev_filtered", "ai_cost_usd": "0.0001", "mode": "paper"})   # 어제 하루
    with open(d / "ledger" / "trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, TRADE_COLS)
        w.writeheader()
        w.writerow({"ticker": "Q", "mode": "paper", "pnl_usd": "-5", "ret_pct": "-1.5", "exit_reasons": "stop_loss",
                    "entry_utc": iso(now - 3_600_000), "exit_utc": iso(now - 3_000_000)})
        w.writerow({"ticker": "L", "mode": "live", "pnl_usd": "99"})                                   # 다른 모드는 안 셈
    (d / "state" / "positions.json").write_text(json.dumps({"ABCD": {"pos": {
        "symbol": "ABCD", "slot": 0, "entry_ms": now - 60_000, "peak": 2.4, "tp_done": [], "trailing": False,
        "fills": [{"side": "BUY", "qty": 100, "price": 2.0, "t_ms": now, "reason": "entry", "fee": 0.2}]}}}))
    (d / "state" / "heartbeat.json").write_text(json.dumps({"t": now - 10_000, "mode": "paper", "marks": {"ABCD": 2.2},
                                                           "last_alert_ms": now - 30_000}))
    st = build_state(tmp_path, cfg, now)
    a = st["account"]
    assert st["engine"]["alive"] and st["engine"]["mode"] == "paper"
    assert a["start"] == 1000.0 and a["realized"] == -5.0 and a["trades"] == 1
    assert abs(a["cash"] - (1000 - 5 - 200.2)) < 1e-6 and abs(a["equity"] - (a["cash"] + 220)) < 1e-6   # heartbeat 계좌 없으면 기록으로
    assert st["positions"][0]["pnl_pct"] == 10.0
    f = dict(st["funnel"])
    assert f["기사 처리"] == 2 and f["종목 범위 통과"] == 1 and f["체결"] == 1      # 오늘(10:00 뒤)만
    assert dict(st["universe"]) == {"시총 범위 밖": 1}
    assert st["ai"]["today"] == 0.03 and st["articles"][0]["ticker"] == "SHEL"
    assert st["trades"][0]["why"] == "손절 −7%"


def test_watch_format():
    w = _load("watch")
    t = kst_ms(2026, 11, 25, 18, 0)
    assert "시총 범위 밖" in w.fmt({"t": t, "kind": "article", "ticker": "S", "action": "skip",
                               "action_reason": "universe", "universe_reason": "market_cap_out"})
    assert w.fmt({"t": t, "kind": "trade_closed", "ticker": "A", "pnl_usd": "2", "ret_pct": "1",
                  "exit_reasons": "tp1|trailing_stop", "hold_min": 3}).startswith("18:00:00 ✅")


def test_dashboard_server_binds_localhost_only():
    src = (ROOT / "scripts" / "dashboard.py").read_text(encoding="utf-8")
    assert 'ThreadingHTTPServer(("127.0.0.1"' in src
    assert "openrouter_key" in src and "Authorization" in src and "esc(r.title)" in src
