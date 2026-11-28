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
    "claude_parse_error", "ref_price", "ref_source", "signal_utc", "signal_price", "signal_rise_pct",
    "over_cap_max_pct",   # 상한(+10%) 위라서 안 샀을 때 본 최고 상승률
    "action", "action_reason", "ai_cost_usd", "mode",
    # D-21: 새 열은 끝에만 붙인다 (예전 파일은 열을 늘려 그대로 이어 쓴다)
    "change_before_pub_pct", "move_since_pub_pct", "duplicate_of",
]

TRADE_COLS = [
    "trade_id", "article_id", "ticker", "mode", "slot", "published_utc", "ref_price",
    "entry_utc", "entry_qty", "entry_price", "slot_usd",
    "exit_utc", "exit_reasons", "sold_qty", "avg_exit_price", "fees_usd", "pnl_usd", "ret_pct",
    "hold_min", "peak_price", "peak_pct", "jev_choice", "claude_grade", "claude_confidence",
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
                if len(head) < len(cols) and cols[:len(head)] == head:
                    self._extend_header()          # 끝에 열만 늘었다 → 옛 줄은 빈칸으로 두고 그대로 이어 쓴다
                else:
                    raise RuntimeError(f"{self.path} 의 열이 코드와 다르다 — 파일을 옮기고 새로 시작할 것")

    def _extend_header(self) -> None:
        with open(self.path, encoding="utf-8", newline="") as f:
            old = list(csv.DictReader(f))
        tmp = self.path.with_suffix(".csv.tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.cols)
            w.writeheader()
            w.writerows({k: r.get(k) or "" for k in self.cols} for r in old)
        tmp.replace(self.path)

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
