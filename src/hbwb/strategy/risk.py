"""안전장치 — 신규 매수를 막을 이유가 있는지 (순수 로직 · 상태는 엔진이 넣는다)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskBook:
    cfg: dict
    day_key: str = ""                      # KST 보고 기준 하루 (09:00 ~ 다음 09:00) 의 이름
    realized_today: float = 0.0
    trades_today: int = 0
    last_exit_ms: dict = field(default_factory=dict)   # 종목 → 마지막 청산 시각
    halted: str | None = None              # 시스템 정지 사유 (equity_floor 등) — 사람이 풀 때까지

    def roll_day(self, key: str) -> None:
        if key != self.day_key:
            self.day_key, self.realized_today, self.trades_today = key, 0.0, 0

    def record_close(self, symbol: str, pnl: float, t_ms: int) -> None:
        self.realized_today += pnl
        self.last_exit_ms[symbol] = t_ms

    def record_open(self) -> None:
        self.trades_today += 1

    def block_reason(self, symbol: str, now_ms: int, equity: float | None, has_position: bool) -> str | None:
        r = self.cfg["risk"]
        if self.halted:
            return f"halted:{self.halted}"
        if has_position:
            return "position_open"
        if equity is not None and equity < r["equity_floor_usd"]:
            self.halted = f"equity {equity:.2f} < floor {r['equity_floor_usd']}"
            return f"halted:{self.halted}"
        if self.realized_today <= -abs(r["daily_loss_limit_usd"]):
            return "daily_loss_limit"
        if self.trades_today >= r["max_trades_per_day"]:
            return "max_trades_per_day"
        t = self.last_exit_ms.get(symbol)
        if t is not None and now_ms - t < r["ticker_cooldown_min"] * 60_000:
            return "ticker_cooldown"
        return None
