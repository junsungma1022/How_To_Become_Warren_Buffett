"""시험의 첫 줄: 실제 네트워크 · 실제 키 · 실제 기록 폴더를 쓰지 못하게 막는다.

(이전 프로젝트에서 시험이 실제 API 를 불러 크레딧을 쓴 사고가 있었다 — 같은 일을 막는다.)
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_real_connect = socket.socket.connect


def _blocked(*a, **k):
    raise RuntimeError("시험에서 네트워크 연결 금지 (tests/conftest.py)")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    for k in list(os.environ):
        if k.startswith("HBWB_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    # 엔진이 저장소 안 data/ · logs/ 를 만들지 않게 기준 폴더를 임시 폴더로
    import hbwb.engine.runner as runner
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    yield


@pytest.fixture
def cfg():
    from hbwb.config import load_settings
    return load_settings()
