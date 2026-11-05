"""A home with no ``.env`` gets an empty one before a namespace spawn, so the mask mounts.

``mount(2)`` cannot mask an absent path and the data-home root is writable in-sandbox, so
without a file at the name the launcher binds nothing over ``.env``. The empty file it
creates must read exactly as a missing one, and an operator's own ``.env`` is never
touched. Every path is under ``tmp_path``.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.config import loader_credentials


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def test_an_absent_env_is_created_empty_and_reads_as_absent(crew_home: Path) -> None:
    env = crew_home / ".env"
    assert not os.path.lexists(env)
    established: list[str] = []

    assert sandbox._materialize_env_mask_target(established) == str(env)

    info = os.lstat(env)
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    assert env.read_bytes() == b""
    if sys.platform != "win32":
        assert stat.S_IMODE(info.st_mode) & 0o077 == 0
    assert established == [str(env)]
    definitive: list[bool] = []
    assert loader_credentials.read_env_credentials_raw(definitive=definitive) == {}
    assert definitive == [True]


def test_an_existing_env_is_left_alone_and_still_required(crew_home: Path) -> None:
    env = crew_home / ".env"
    env.write_text("SLACK_BOT_TOKEN=fixture-not-a-secret\n", encoding="utf-8")
    established: list[str] = []

    assert sandbox._materialize_env_mask_target(established) is None

    assert env.read_text(encoding="utf-8") == "SLACK_BOT_TOKEN=fixture-not-a-secret\n"
    assert established == [str(env)]


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs a privilege on Windows")
def test_a_symlinked_env_is_left_alone(crew_home: Path, tmp_path: Path) -> None:
    dotfile = tmp_path / "dotfiles.env"
    dotfile.write_text("OWNER_ID=fixture\n", encoding="utf-8")
    (crew_home / ".env").symlink_to(dotfile)
    established: list[str] = []

    assert sandbox._materialize_env_mask_target(established) is None

    assert (crew_home / ".env").is_symlink()
    assert dotfile.read_text(encoding="utf-8") == "OWNER_ID=fixture\n"
    assert established == []


def test_a_failed_create_warns_and_lets_the_spawn_go_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path / "nope")
    established: list[str] = []

    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        assert sandbox._materialize_env_mask_target(established) is None

    assert established == []
    assert not (tmp_path / "nope").exists()
    assert "could not create an empty" in caplog.text
