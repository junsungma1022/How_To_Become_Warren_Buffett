"""비밀값이 저장소에 들어가지 않게 지키는 시험 (pre-commit gitleaks 와 겹쳐서 이중으로)."""

import re
import subprocess
from pathlib import Path

from hbwb.config import SECRET_ENV, load_secrets, scrub

ROOT = Path(__file__).resolve().parents[1]

SECRET_SHAPES = [
    re.compile(r"sk-or-v1-[A-Za-z0-9]{30,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{30,}"),
    re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def tracked_or_candidate_files():
    """git 이 있으면 커밋될 파일(추적 + 무시되지 않은 새 파일), 없으면 저장소의 일반 파일."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
        return [ROOT / p for p in out]
    except Exception:  # noqa: BLE001
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "data" not in p.parts]


def test_env_example_lists_exactly_the_secret_names_and_no_values():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    pairs = dict(ln.split("=", 1) for ln in text.splitlines() if ln and not ln.startswith("#") and "=" in ln)
    assert set(pairs) == set(SECRET_ENV.values())
    assert all(v.strip() == "" for v in pairs.values()), ".env.example 에 값이 들어 있다"


def test_gitignore_blocks_env_and_data():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for needed in (".env", ".env.*", "!.env.example", "data/", "logs/", "*.jsonl"):
        assert needed in gi, needed


def test_env_file_is_not_committable():
    files = {p.relative_to(ROOT).as_posix() for p in tracked_or_candidate_files()}
    assert ".env" not in files
    assert not any(f.startswith(("data/", "logs/")) for f in files)


def test_no_secret_shapes_in_repo_files():
    hits = []
    for p in tracked_or_candidate_files():
        if p.suffix in {".png", ".jpg", ".knwf", ".zip", ".pdf"} or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for rx in SECRET_SHAPES:
            if rx.search(text):
                hits.append(f"{p.relative_to(ROOT)}: {rx.pattern[:30]}")
    assert not hits, hits


def test_scrub_hides_known_values_and_shapes():
    s = scrub("key=abc123SECRET hook https://discord.com/api/webhooks/1/abc Bearer abcdefghijklmnopqrstuv "
              "url?apiKey=zzz&x=1", ["abc123SECRET"])
    assert "abc123SECRET" not in s and "webhooks/1" not in s and "abcdefghijklmnop" not in s and "zzz" not in s


def test_secrets_repr_hides_values():
    sec = load_secrets({"HBWB_OPENROUTER_KEY": "sk-or-v1-zzzzzzzz"})
    assert "zzzz" not in repr(sec) and sec.present()["HBWB_OPENROUTER_KEY"] is True
