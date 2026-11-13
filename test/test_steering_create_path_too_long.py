"""A steering create whose joined path is too long answers 400 ``name_too_long``.

``MAX_STEERING_NAME_BYTES`` bounds the relative name only. A name inside it can
still join onto a deep project folder into a path a Windows host without long
paths refuses. The create maps the host's length refusal onto the same coded
400 the name bound answers. Every other write failure answers ``500
write_failed``.

The Windows refusal is a seam here: ``os.open`` raises the bare
``FileNotFoundError`` the CRT ``_wopen`` raises for a path past ``MAX_PATH``.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_steering_api import _make_app, _state

import kiro_crew.dashboard.handlers.steering as steering_mod
from kiro_crew.dashboard.handlers.steering import _create_path_too_long


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so the user steering root is a writable sandbox."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


_NAME = "n" * 150
_real_open = os.open


def _refuse_long_target(monkeypatch: pytest.MonkeyPatch, exc_factory) -> None:
    """``os.open`` of the probe's own leaf raises *exc_factory(path)*; others pass."""

    def _open(path, flags, mode=0o777, *args, **kwargs):
        if os.fspath(path).endswith(f"{_NAME}.md"):
            raise exc_factory(os.fspath(path))
        return _real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(steering_mod, "_DIR_FD_SUPPORTED", False)
    monkeypatch.setattr(steering_mod.os, "open", _open)


async def _create(name: str = _NAME):
    async with TestClient(TestServer(_make_app(_state()))) as client:
        resp = await client.post("/api/steering", json={"name": name, "content": "# probe\n"})
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_windows_by_name_length_refusal_is_coded_400(fake_home, monkeypatch):
    monkeypatch.setattr(steering_mod, "_IS_WINDOWS", True)
    _refuse_long_target(
        monkeypatch, lambda p: FileNotFoundError(errno.ENOENT, "No such file or directory", p)
    )

    status, data = await _create()

    assert status == 400, data
    assert data["code"] == "name_too_long"
    assert not (fake_home / ".kiro" / "steering" / f"{_NAME}.md").exists()


@pytest.mark.asyncio
async def test_off_windows_a_missing_file_error_stays_write_failed(fake_home, monkeypatch):
    monkeypatch.setattr(steering_mod, "_IS_WINDOWS", False)
    _refuse_long_target(
        monkeypatch, lambda p: FileNotFoundError(errno.ENOENT, "No such file or directory", p)
    )

    status, data = await _create()

    assert status == 500, data
    assert data["code"] == "write_failed"


@pytest.mark.asyncio
async def test_enametoolong_is_coded_400(fake_home, monkeypatch):
    monkeypatch.setattr(steering_mod, "_IS_WINDOWS", False)
    _refuse_long_target(monkeypatch, lambda p: OSError(errno.ENAMETOOLONG, "File name too long", p))

    status, data = await _create()

    assert status == 400, data
    assert data["code"] == "name_too_long"


@pytest.mark.asyncio
async def test_other_write_failures_stay_write_failed(fake_home, monkeypatch):
    monkeypatch.setattr(steering_mod, "_IS_WINDOWS", True)
    _refuse_long_target(monkeypatch, lambda p: PermissionError(errno.EACCES, "Access denied", p))

    status, data = await _create()

    assert status == 500, data
    assert data["code"] == "write_failed"


def test_missing_parent_is_not_read_as_length(tmp_path, monkeypatch):
    """A ``FileNotFoundError`` whose parent is absent is a real missing parent."""
    monkeypatch.setattr(steering_mod, "_IS_WINDOWS", True)
    missing = tmp_path / "gone" / "leaf.md"
    exc = FileNotFoundError(errno.ENOENT, "No such file or directory", str(missing))
    assert _create_path_too_long(exc) is False


def test_existing_parent_is_read_as_length_on_windows_only(tmp_path, monkeypatch):
    leaf = tmp_path / "leaf.md"
    exc = FileNotFoundError(errno.ENOENT, "No such file or directory", str(leaf))
    monkeypatch.setattr(steering_mod, "_IS_WINDOWS", True)
    assert _create_path_too_long(exc) is True
    monkeypatch.setattr(steering_mod, "_IS_WINDOWS", False)
    assert _create_path_too_long(exc) is False


def test_no_filename_is_not_read_as_length(monkeypatch):
    monkeypatch.setattr(steering_mod, "_IS_WINDOWS", True)
    assert _create_path_too_long(FileNotFoundError(errno.ENOENT, "gone")) is False
