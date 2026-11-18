"""시각 도우미 — 내부 시각은 모두 UTC epoch ms. 사람에게 보여 줄 때만 KST · ET 로 바꾼다."""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_iso_ms(s: str | None) -> int | None:
    """ISO 8601 (Z · ±hh:mm) → epoch ms. 못 읽으면 None."""
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None  # 시간대 없는 시각은 추측하지 않는다
    return int(t.timestamp() * 1000)


def ms_to(ms: int, tz: ZoneInfo) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz)


def fmt_kst(ms: int | None) -> str:
    return ms_to(ms, KST).strftime("%m-%d %H:%M:%S KST") if ms else "-"


def fmt_et(ms: int | None) -> str:
    return ms_to(ms, ET).strftime("%m-%d %H:%M:%S ET") if ms else "-"


def iso_utc(ms: int | None) -> str:
    return ms_to(ms, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if ms else ""
