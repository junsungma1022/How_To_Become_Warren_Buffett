"""KNIME 가 바로 읽는 CSV 기록 (data/ 아래 · 저장소에 올리지 않는다).

- articles.csv : 받은 기사 한 건 = 한 줄 (종목 범위 · Jev · Claude · 실행 결과)
- trades.csv   : 거래 한 건 = 한 줄 (분할 체결 요약 · 손익 · 청산 이유)
열 이름은 바뀌지 않게 고정한다 (KNIME 워크플로우가 열 이름으로 노드를 설정한다).
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path

ARTICLE_COLS = [
    "article_id", "ticker", "published_utc", "received_utc", "age_s", "session", "title", "exchange",
    "universe_ok", "universe_reason", "market_cap_usd", "prev_close", "last_price", "change_vs_prev_close_pct",
    "jev_choice", "jev_grade", "jev_expected_grade", "jev_confidence",
    "jev_p_big_negative", "jev_p_small_negative", "jev_p_neutral", "jev_p_small_positive", "jev_p_big_positive",
    "jev_pass", "claude_called", "claude_decision", "claude_grade", "claude_confidence", "claude_catalyst",
    "claude_parse_error", "action", "action_reason", "ai_cost_usd", "mode",
]

TRADE_COLS = [
    "trade_id", "article_id", "ticker", "mode", "entry1_utc", "entry1_qty", "entry1_price",
    "entry2_utc", "entry2_qty", "entry2_price", "avg_price", "bought_qty",
    "exit_utc", "exit_reasons", "sold_qty", "avg_exit_price", "fees_usd", "pnl_usd", "ret_pct",
    "hold_min", "peak_price", "jev_choice", "claude_grade", "claude_confidence",
]


class CsvLedger:
    def __init__(self, path: Path, cols: list[str]):
        self.path, self.cols = Path(path), cols
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if self.path.exists():
            with open(self.path, encoding="utf-8", newline="") as f:
                head = next(csv.reader(f), None)
            if head and head != cols:
                raise RuntimeError(f"{self.path} 의 열이 코드와 다르다 — 파일을 옮기고 새로 시작할 것")

    def append(self, row: dict) -> None:
        unknown = set(row) - set(self.cols)
        if unknown:
            raise KeyError(f"모르는 열: {sorted(unknown)}")
        with self._lock:
            new = not self.path.exists() or self.path.stat().st_size == 0
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=self.cols)
                if new:
                    w.writeheader()
                w.writerow({k: _cell(row.get(k)) for k in self.cols})

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1e15 else str(v)
    return str(v).replace("\r", " ").replace("\n", " ")


def articles(data_dir: Path) -> CsvLedger:
    return CsvLedger(Path(data_dir) / "ledger" / "articles.csv", ARTICLE_COLS)


def trades(data_dir: Path) -> CsvLedger:
    return CsvLedger(Path(data_dir) / "ledger" / "trades.csv", TRADE_COLS)
