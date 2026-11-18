"""미국 장 세션 (토스 장 캘린더 기준 · KST 시각으로 온다 · 서머타임은 토스가 반영).

세션 이름: day(토스 데이마켓) · pre · regular · after.
"""

from __future__ import annotations

from ..broker.toss import sessions_from_calendar_day

ALLOW_KEY = {"day": "trade_daymarket", "pre": "trade_premarket", "regular": "trade_regular", "after": "trade_aftermarket"}


def windows_from_calendar(cal: dict) -> list[tuple[str, int, int]]:
    out = []
    for k in ("previousBusinessDay", "today", "nextBusinessDay"):
        for name, (a, b) in sessions_from_calendar_day((cal or {}).get(k)).items():
            out.append((name, a, b))
    return sorted(set(out), key=lambda x: x[1])


def current(windows: list[tuple[str, int, int]], now_ms: int) -> tuple[str, int, int] | None:
    for w in windows:
        if w[1] <= now_ms < w[2]:
            return w
    return None


def allowed(name: str | None, cfg: dict) -> bool:
    return bool(name) and bool(cfg["sessions"].get(ALLOW_KEY.get(name, ""), False))


def trading_block_end(windows: list[tuple[str, int, int]], now_ms: int, cfg: dict, gap_ms: int = 60_000) -> int | None:
    """지금 세션부터 이어지는 '허용된' 세션들의 마지막 끝 시각 (포지션은 그 전에 모두 정리한다)."""
    cur = current(windows, now_ms)
    if cur is None or not allowed(cur[0], cfg):
        return None
    end = cur[2]
    for name, a, b in windows:
        if a <= cur[1]:
            continue
        if allowed(name, cfg) and a - end <= gap_ms:
            end = max(end, b)
        elif a > end:
            break
    return end
