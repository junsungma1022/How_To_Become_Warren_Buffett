"""이벤트 기록 (JSONL · 하루 한 파일). 침묵 실패 금지 — 모든 판단 · 주문 · 오류가 여기 남는다.

기록 전에 항상 `scrub` 를 거쳐 비밀값이 파일에 남지 않게 한다.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

from .clock import KST, ms_to, now_ms
from .config import scrub


class EventLog:
    def __init__(self, log_dir: Path, secrets: Iterable[str] = (), echo: bool = True):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._secrets = list(secrets)
        self._lock = threading.Lock()
        self.echo = echo

    def path_for(self, ms: int) -> Path:
        return self.dir / f"events-{ms_to(ms, KST):%Y%m%d}.jsonl"

    def write(self, kind: str, **fields: Any) -> dict:
        ms = now_ms()
        rec = {"t": ms, "kind": kind, **fields}
        line = scrub(json.dumps(rec, ensure_ascii=False, default=str), self._secrets)
        with self._lock:
            with open(self.path_for(ms), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.echo:
            print(line[:400], flush=True)
        return rec


def read_events(log_dir: Path, day_kst: str | None = None) -> list[dict]:
    """`day_kst` = 'YYYYMMDD' (없으면 전부)."""
    out = []
    pattern = f"events-{day_kst}.jsonl" if day_kst else "events-*.jsonl"
    for p in sorted(Path(log_dir).glob(pattern)):
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        out.append({"kind": "corrupt_line", "file": p.name})
    return out
