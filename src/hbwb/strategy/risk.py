"""안전장치 — 금액이 아니라 % 기준 (사용자 2026-11-19 · 이전 프로그램 risk_manager: 3연패 · 일 −10%).

- 그날 실현손실 ≥ 그날 시작 자산의 daily_loss_limit_pct% → 그날 신규 매수 중단
- 연속 손실 max_consecutive_losses 번 → 그날 신규 매수 중단
- 자산 ≤ 시작 자금 × (1 − max_drawdown_pct%) → 시스템 정지 (사람이 풀 때까지)
- 칸: 보유 중인 칸 수 ≥ slots 면 매수 안 함
- 같은 종목은 미국 날짜 하루에 한 번
'하루' 는 일일 보고서와 같은 KST 10:00 ~ 다음 10:00 (미국 프리~애프터 한 바퀴 · report.daily_time_kst).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskBook:
    cfg: dict
    day_key: str = ""
    day_start_equity: float | None = None
    realized_today: float = 0.0
    consecutive_losses: int = 0
    traded: set = field(default_factory=set)          # (종목, 미국 날짜)
    halted: str | None = None

    def roll_day(self, key: str, equity: float | None) -> None:
        if key != self.day_key:
            self.day_key, self.realized_today, self.consecutive_losses = key, 0.0, 0
            self.day_start_equity = equity

    def record_open(self, symbol: str, us_day: str) -> None:
        self.traded.add((symbol, us_day))

    def record_close(self, pnl: float) -> None:
        self.realized_today += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl <= 0 else 0

    def rebuild(self, trades: list[dict], day_key_of, us_day_of, parse_ms, open_entries: list[tuple[str, str]]) -> None:
        """재시작해도 안전장치가 풀리지 않게 trades.csv 로 오늘 상태를 다시 만든다.
        - 오늘(보고 기준 하루) 끝난 거래의 실현손익 합 · 끝에서부터 센 연속 손실
        - 오늘(미국 날짜) 이미 산 종목 (끝난 거래 + 지금 보유)"""
        today = self.day_key
        rows = sorted((r for r in trades if r.get("exit_utc")), key=lambda r: r["exit_utc"])
        todays = [r for r in rows if parse_ms(r["exit_utc"]) and day_key_of(parse_ms(r["exit_utc"])) == today]
        self.realized_today = sum(float(r.get("pnl_usd") or 0) for r in todays)
        streak = 0
        for r in reversed(todays):
            if float(r.get("pnl_usd") or 0) <= 0:
                streak += 1
            else:
                break
        self.consecutive_losses = streak
        for r in rows:
            t = parse_ms(r.get("entry_utc") or "")
            if t:
                self.traded.add((r.get("ticker"), us_day_of(t)))
        for e in open_entries:
            self.traded.add(tuple(e))

    def daily_loss_pct(self) -> float:
        if not self.day_start_equity:
            return 0.0
        return 100.0 * min(0.0, self.realized_today) / self.day_start_equity

    def block_reason(self, symbol: str, us_day: str, equity: float | None, slots_used: int) -> str | None:
        r = self.cfg["risk"]
        if self.halted:
            return f"halted:{self.halted}"
        start = float(self.cfg["capital"]["budget_usd"])
        if equity is not None and equity <= start * (1 - r["max_drawdown_pct"] / 100):
            self.halted = f"drawdown {100 * (equity / start - 1):.1f}% ≤ −{r['max_drawdown_pct']}%"
            return f"halted:{self.halted}"
        if slots_used >= int(self.cfg["capital"]["slots"]):
            return "slots_full"
        if self.daily_loss_pct() <= -abs(r["daily_loss_limit_pct"]):
            return "daily_loss_limit"
        if self.consecutive_losses >= r["max_consecutive_losses"]:
            return "consecutive_losses"
        if r.get("one_trade_per_ticker_per_day", True) and (symbol, us_day) in self.traded:
            return "ticker_already_traded_today"
        return None
