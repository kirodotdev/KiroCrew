"""The backend SDK's file path gate (``backend_sdk.canonical_file_path``, seam level 14).

What is pinned is what an app cannot check for itself: the public name answers
exactly what the gateway's own gate answers, looked up at call time, refuses a
sensitive target behind a link, offers no knob of the private gate, and judges a
name only, so the one public read still refuses the hardlinked alias the gate admits.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import hooks
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_SRC = Path(__file__).resolve().parents[1] / "src"


def _sensitive_file() -> Path:
    from kiro_crew.config.paths import data_home

    # The data home is pinned per test, so this `.env` is the test's own.
    secret = Path(data_home()) / ".env"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("TOKEN=x\n", encoding="utf-8")
    return secret


# ── the name and its level ──────────────────────────────────────────────────


def test_the_gate_is_listed_and_published_at_fourteen() -> None:
    assert "canonical_file_path" in sdk.__all__
    assert "canonical_file_path" in dir(sdk)
    assert sdk._EXPORT_LEVELS["canonical_file_path"] == 14 <= sdk.SDK_LEVEL
    assert sdk.BACKEND_SDK_LEVEL == 7


# ── what it answers ─────────────────────────────────────────────────────────


def test_the_gate_answers_a_plain_file_with_its_canonical_path(tmp_path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")
    assert sdk.canonical_file_path(str(target)) == os.path.realpath(target)


# Every link below is a directory link with the file beneath it: a junction on
# Windows needs no privilege, so the linked-path checks run on every OS.
def test_the_gate_follows_a_link_and_answers_its_target(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    target = real / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    make_dir_link(link, real)
    assert sdk.canonical_file_path(str(link / "real.txt")) == os.path.realpath(target)


def test_the_gate_refuses_a_sensitive_path_and_a_link_into_one(tmp_path) -> None:
    secret = _sensitive_file()
    assert sdk.canonical_file_path(str(secret)) is None
    link = tmp_path / "innocent"
    make_dir_link(link, secret.parent)
    assert sdk.canonical_file_path(str(link / secret.name)) is None


@pytest.mark.parametrize("raw", ["", "a\x00b"])
def test_the_gate_refuses_an_empty_or_unrepresentable_path(raw) -> None:
    assert sdk.canonical_file_path(raw) is None


def test_the_gate_answers_exactly_what_the_private_gate_answers(tmp_path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()
    inner = folder / "inner.txt"
    inner.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    make_dir_link(link, folder)
    secret = _sensitive_file()
    home_link = tmp_path / "home-link"
    make_dir_link(home_link, secret.parent)
    candidates = [
        plain,
        tmp_path / "missing.txt",
        folder,
        secret,
        link,
        link / inner.name,
        home_link / secret.name,
    ]
    for candidate in candidates:
        raw = str(candidate)
        assert sdk.canonical_file_path(raw) == hooks.validate_file_path(raw), raw


# ── the delegate itself ─────────────────────────────────────────────────────


def test_the_gate_delegates_at_call_time_with_no_keyword(monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    def recorder(*args, **kwargs):
        calls.append((args, kwargs))
        return "/canon"

    monkeypatch.setattr("kiro_crew.hooks.validate_file_path", recorder)
    assert sdk.canonical_file_path("/x") == "/canon"
    assert calls == [(("/x",), {})]


def test_the_gate_takes_only_the_path() -> None:
    assert list(inspect.signature(sdk.canonical_file_path).parameters) == ["path"]


@pytest.mark.parametrize("knob", ["canonical_gate", "classify_sensitive"])
def test_the_gate_offers_no_knob_of_the_private_gate(knob) -> None:
    with pytest.raises(TypeError):
        sdk.canonical_file_path("/x", **{knob: True})


def test_the_gate_judges_names_so_the_read_still_refuses_a_hardlink(tmp_path) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"hello")
    hard = tmp_path / "hard.txt"
    os.link(target, hard)
    assert sdk.canonical_file_path(str(hard)) == os.path.realpath(hard)
    assert sdk.read_bytes_nofollow(str(hard)) is None


# ── a spawned backend ───────────────────────────────────────────────────────


def test_a_spawned_child_reaches_the_gate_on_the_gateway_interpreter(tmp_path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    probe = (
        "import os, sys\n"
        "import kiro_crew.apps.backend_sdk as sdk\n"
        "print('kiro_crew.hooks' in sys.modules)\n"
        "print(sdk.canonical_file_path(sys.argv[1]) == os.path.realpath(sys.argv[1]))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(_SRC),
        "KIROCREW_SPAWNED": "1",
        "KIROCREW_APP_NAME": "probe-app",
    }
    done = subprocess.run(
        [sys.executable, "-c", probe, str(plain)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    loaded, answered = done.stdout.split("\n")[:2]
    assert loaded == "False"
    assert answered == "True"
