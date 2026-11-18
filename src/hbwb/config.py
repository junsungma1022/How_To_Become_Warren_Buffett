"""설정과 비밀값.

- 설정(`config/settings.yaml`)은 저장소에 올라가는 숫자들이다.
- 비밀값은 **환경변수(.env)** 에서만 읽는다. 이 모듈 밖으로는 `Secrets` 객체로만 나가고,
  `repr` · 로그 · 예외 문구에는 절대 원문이 찍히지 않는다 (`scrub`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
ENV_PATH = ROOT / ".env"

# 비밀값 환경변수 이름 (값은 .env 에만). 이름 목록은 .env.example 과 같아야 한다 (tests 가 확인).
SECRET_ENV = {
    "rtpr_key": "HBWB_RTPR_KEY",
    "openrouter_key": "HBWB_OPENROUTER_KEY",
    "toss_client_id": "HBWB_TOSS_CLIENT_ID",
    "toss_client_secret": "HBWB_TOSS_CLIENT_SECRET",
    "discord_entries": "HBWB_DISCORD_WEBHOOK_ENTRIES",
    "discord_exits": "HBWB_DISCORD_WEBHOOK_EXITS",
    "discord_warnings": "HBWB_DISCORD_WEBHOOK_WARNINGS",
    "discord_critical": "HBWB_DISCORD_WEBHOOK_CRITICAL",
    "discord_daily_summary": "HBWB_DISCORD_WEBHOOK_DAILY_SUMMARY",
}

# 로그에 실수로 섞일 수 있는 비밀 모양 (값을 몰라도 지운다)
_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"),                          # OpenRouter
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),                         # Anthropic
    re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),   # JWT (토스 토큰)
    re.compile(r"(?i)(apiKey=)[^&\s]+"),
]


class MissingSecret(RuntimeError):
    pass


def load_dotenv_file(path: Path = ENV_PATH) -> None:
    """`.env` 를 환경변수로 올린다 (이미 있는 값은 덮지 않는다). 파일이 없으면 아무것도 안 한다."""
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 의존성은 requirements 에 있다
        raise SystemExit("python-dotenv 가 없다: pip install -r requirements.txt")
    load_dotenv(path, override=False)


@dataclass(frozen=True)
class Secrets:
    """비밀값 묶음. `repr` 에 값이 나오지 않는다."""

    values: Mapping[str, str] = field(repr=False)

    def get(self, name: str, required: bool = True) -> str:
        v = (self.values.get(name) or "").strip()
        if required and not v:
            raise MissingSecret(f"{SECRET_ENV.get(name, name)} 가 비어 있다 — .env 를 확인 (값은 채팅에 붙여넣지 말 것)")
        return v

    def present(self) -> dict[str, bool]:
        """어떤 키가 채워졌는지만 (값은 보지 않는다) — check_setup 용."""
        return {SECRET_ENV[k]: bool((self.values.get(k) or "").strip()) for k in SECRET_ENV}

    def all_values(self) -> list[str]:
        return [v for v in self.values.values() if v and len(v) >= 6]

    def __repr__(self) -> str:  # pragma: no cover - 방어용
        return "Secrets(<hidden>)"


def load_secrets(env: Mapping[str, str] | None = None) -> Secrets:
    env = os.environ if env is None else env
    return Secrets({k: env.get(v, "") for k, v in SECRET_ENV.items()})


def scrub(text: Any, secrets: Iterable[str] = ()) -> str:
    """문자열에서 비밀값(알려진 값 + 모양)을 `***` 로 바꾼다. 로그 · 예외 · 알림에 쓰기 전에 항상 거친다."""
    s = str(text)
    for v in sorted({x for x in secrets if x}, key=len, reverse=True):
        s = s.replace(v, "***")
    for p in _PATTERNS:
        s = p.sub(lambda m: (m.group(1) + "***") if m.lastindex else "***", s)
    return s


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    e, x, r = cfg["entry"], cfg["exit"], cfg["risk"]
    problems = []
    if not 0 < e["first_fraction"] <= 1:
        problems.append("entry.first_fraction 은 (0, 1]")
    if x["stop_loss_pct"] >= 0:
        problems.append("exit.stop_loss_pct 는 음수")
    if not 0 < x["tp1_pct"] < x["tp2_pct"]:
        problems.append("0 < exit.tp1_pct < exit.tp2_pct")
    if not 0 < x["tp1_fraction"] < 1:
        problems.append("exit.tp1_fraction 은 (0, 1)")
    if r["max_positions"] != 1:
        problems.append("risk.max_positions 는 지금 1 만 지원")
    if cfg["capital"]["budget_usd"] <= 0:
        problems.append("capital.budget_usd > 0")
    if problems:
        raise ValueError("settings.yaml 오류: " + " · ".join(problems))
