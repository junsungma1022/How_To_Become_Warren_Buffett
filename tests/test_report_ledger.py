import datetime as dt

import pytest

from hbwb.ledger import ARTICLE_COLS, CsvLedger
from hbwb.report.daily import render, summarize, window_ending


def test_ledger_roundtrip_and_unknown_column(tmp_path):
    led = CsvLedger(tmp_path / "a.csv", ["x", "y"])
    led.append({"x": 1, "y": "멀티\n라인"})
    led.append({"x": True})
    rows = led.rows()
    assert rows == [{"x": "1", "y": "멀티 라인"}, {"x": "true", "y": ""}]
    with pytest.raises(KeyError):
        led.append({"z": 1})


def test_ledger_header_mismatch_refuses(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        CsvLedger(p, ["x"])


def test_daily_summary_window_and_render():
    w = window_ending(dt.date(2026, 11, 20))
    assert w.label.startswith("11-19 10:00") and w.label.endswith("11-20 10:00 KST")
    inside, outside = "2026-11-19T15:00:00.000Z", "2026-11-18T15:00:00.000Z"
    arts = [{"received_utc": inside, "universe_ok": "true", "jev_pass": "true", "claude_called": "true",
             "claude_decision": "BUY", "action": "buy", "ai_cost_usd": "0.01"},
            {"received_utc": outside, "universe_ok": "true"}]
    trades = [{"exit_utc": inside, "ticker": "ABC", "pnl_usd": "2.5", "ret_pct": "5", "fees_usd": "0.1",
               "exit_reasons": "tp1|tp2", "hold_min": "12", "jev_choice": "큰 호재", "claude_grade": "5"},
              {"exit_utc": inside, "ticker": "XYZ", "pnl_usd": "-1", "ret_pct": "-2", "fees_usd": "0.1"}]
    s = summarize(w, arts, trades, [{"t": w.start_ms + 1, "kind": "alert"}])
    assert s["articles"] == 1 and s["filled"] == 1 and s["wins"] == 1 and s["losses"] == 1
    assert abs(s["realized"] - 1.5) < 1e-9
    txt = render(s, "live", {"cash": 90.0, "holdings": 11.5}, 100.0)
    assert "$+1.50" in txt and "합계 **$101.50**" in txt and "승률 50%" in txt
    assert "지금(10:00) 리셋" in txt


def test_article_cols_are_unique():
    assert len(ARTICLE_COLS) == len(set(ARTICLE_COLS))


def test_heartbeat_and_cumulative():
    from hbwb.report.daily import cumulative, heartbeat_status
    now = 10_000_000
    ok, line = heartbeat_status({"t": now - 4 * 60_000, "mode": "live", "started_ms": now - 7_200_000,
                                 "positions": {"ABC": 3}}, now)
    assert ok and "ABC 3주" in line and "2.0시간째" in line
    ok, line = heartbeat_status({"t": now - 40 * 60_000}, now)
    assert not ok and "멈춤" in line
    assert heartbeat_status(None, now)[0] is False
    c = cumulative([{"exit_utc": "2026-11-19T01:00:00Z", "pnl_usd": "3"}, {"exit_utc": "2026-11-20T01:00:00Z", "pnl_usd": "-1"},
                    {"exit_utc": "", "pnl_usd": "99"}])
    assert c == {"trades": 2, "realized": 2.0, "wins": 1, "days": 2, "fees": 0.0}
    txt = render({"window": "w", "alerts": 0, "articles": 0, "universe_ok": 0, "jev_pass": 0, "claude_called": 0,
                  "claude_buy": 0, "filled": 0, "ai_cost": 0, "trades": [], "realized": 0, "wins": 0, "losses": 0,
                  "fees": 0, "warnings": 0}, "live", None, 100, c, line)
    assert "누적 (시작부터): 실현 $+2.00" in txt and "리셋" in txt


def test_history_table():
    from hbwb.engine.runner import report_day_key
    from hbwb.report.daily import history, render_history
    rows = history([{"exit_utc": "2026-11-19T01:00:00Z", "pnl_usd": "3"},      # KST 10:00 → 11-19
                    {"exit_utc": "2026-11-19T23:30:00Z", "pnl_usd": "-1"},     # KST 11-20 08:30 → 11-19
                    {"exit_utc": "2026-11-20T01:00:00Z", "pnl_usd": "2"}], report_day_key)
    assert [(r["day"], r["trades"], r["cumulative"]) for r in rows] == [("2026-11-19", 2, 2.0), ("2026-11-20", 1, 4.0)]
    assert "+4.0%" in render_history(rows, 100)
