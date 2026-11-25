"""D-16: paper 가상 계좌 $1000 — 칸 금액 · 손익이 다음 날 · 재시작에도 이어진다."""

import asyncio
import json

from hbwb.config import mode_cfg, normalize_mode

from test_engine_flow import age, alert, make_engine


def _cal_stub(eng):
    async def cal():
        return {}
    eng.toss.us_calendar = cal


def test_mode_names_and_paper_budget(cfg):
    assert normalize_mode("shadow") == "paper" and normalize_mode("live") == "live"
    assert cfg["capital"]["paper_start_usd"] == 1000.0
    assert mode_cfg(cfg, "paper")["capital"]["budget_usd"] == 1000.0
    assert mode_cfg(cfg, "live")["capital"]["budget_usd"] == 100.0       # 실계좌 설정은 그대로
    assert cfg["capital"]["budget_usd"] == 100.0                          # 원본을 바꾸지 않는다


def test_paper_slot_is_one_third_of_1000(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg, start=1000.0)
    eng.risk.roll_day("d", 1000.0)
    asyncio.run(eng.handle_alert(alert()))
    pos = eng.positions["ABC"]
    assert 300 < pos.qty * pos.avg_price <= 1000 / 3 + 0.01
    assert eng.mode == "paper" and eng.trades.rows() == []


def test_paper_cash_carries_over_restart(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg, start=1000.0)
    eng.risk.roll_day("d", 1000.0)
    asyncio.run(eng.handle_alert(alert()))
    age(eng, "ABC", 60)
    eng.toss.px["ABC"] = eng.positions["ABC"].avg_price * 0.9            # 손절 −10%
    asyncio.run(eng.manage_one("ABC"))
    pnl = float(eng.trades.rows()[-1]["pnl_usd"])
    assert pnl < 0 and eng.trades.rows()[-1]["mode"] == "paper"

    eng2 = make_engine(tmp_path, cfg, start=1000.0)                       # 다음 날 · 재시작
    _cal_stub(eng2)
    asyncio.run(eng2.preflight())
    cash, eq = asyncio.run(eng2.equity())
    assert abs(cash - (1000.0 + pnl)) < 1e-6 and abs(eq - cash) < 1e-6    # 보유 없음 → 평가 = 현금
    assert abs(eng2.paper_realized - pnl) < 1e-9
    assert any("가상 계좌 시작 $1000" in t and f"{pnl:+.2f}" in t for ch, t in eng2.discord.sent)
    eng2.write_heartbeat()
    hb = json.loads(eng2.heartbeat_path.read_text(encoding="utf-8"))
    assert hb["mode"] == "paper" and abs(hb["paper"]["cash_usd"] - (1000.0 + pnl)) < 1e-3


def test_paper_open_position_survives_restart(tmp_path, cfg):
    eng = make_engine(tmp_path, cfg, start=1000.0)
    eng.risk.roll_day("d", 1000.0)
    asyncio.run(eng.handle_alert(alert()))
    cost = eng.positions["ABC"].cost
    eng2 = make_engine(tmp_path, cfg, start=1000.0)
    _cal_stub(eng2)
    asyncio.run(eng2.preflight())
    assert "ABC" in eng2.positions                                        # 보유 종목이 이어진다
    cash, _ = asyncio.run(eng2.equity())
    assert cash < 1000.0 - cost + 1e-6                                     # 보유 원가만큼 현금이 빠져 있다
