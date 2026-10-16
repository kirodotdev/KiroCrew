"""A staged replacement that fails at any step leaves no temp file behind.

``atomic_write`` and ``stage_replacement`` share one staging sequence. Each
step that can fail (the write, the ``fsync`` and the ``close`` that ends it)
must release the descriptor and remove the temp file before the error reaches
the caller. A temp file here is a full copy of the transcript being rewritten,
and ``stage_replacement`` returns no name when it raises, so nothing else could
remove it.

The write and ``fsync`` cases also fail the ``close`` that cleanup issues for
the orphaned descriptor, so a failing cleanup close cannot skip the unlink. In
every case the staged descriptor is closed exactly once: a ``close`` that failed
is not retried on a number that may already belong to another file.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew.history import ConversationLog

_POINTS = ["close", "fsync", "write"]


def _fail_the_first_staged_file(monkeypatch: pytest.MonkeyPatch, point: str) -> dict[str, Any]:
    """Fail *point* on the first temp file staged; its close fails in every case.

    Later temp files (for example the rewrite that runs under the session lock)
    are untouched. A failing close still releases the descriptor, as the kernel
    does when ``close(2)`` reports EIO, so its number is never targeted twice.
    """
    staged: dict[str, Any] = {}
    real_mkstemp = tempfile.mkstemp
    real_close, real_fsync, real_write = os.close, os.fsync, os.write

    def targeted(fd: int) -> bool:
        return staged.get("fd") == fd and not staged.get("closed")

    def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        if "fd" not in staged:
            staged.update(fd=fd, name=name)
        return fd, name

    def close(fd: int) -> None:
        if staged.get("fd") == fd and not staged.get("returned"):
            staged["close_calls"] = staged.get("close_calls", 0) + 1
        if targeted(fd):
            staged["closed"] = True
            real_close(fd)
            raise OSError(errno.EIO, "injected close failure")
        real_close(fd)

    def fsync(fd: int) -> None:
        if point == "fsync" and targeted(fd):
            raise OSError(errno.EIO, "injected fsync failure")
        real_fsync(fd)

    def write(fd: int, data: Any) -> int:
        if point == "write" and targeted(fd):
            raise OSError(errno.EIO, "injected write failure")
        return real_write(fd, data)

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "write", write)
    return staged


def _assert_closed_exactly_once(staged: dict[str, Any]) -> None:
    # A second close of a number the kernel already released can close another
    # thread's file that has since been given the same number.
    staged["returned"] = True
    calls = staged.get("close_calls")
    assert calls == 1, f"the staged descriptor was closed {calls} times"


def _temp_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.tmp"))


@pytest.mark.parametrize("point", _POINTS)
def test_stage_replacement_removes_its_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    target = tmp_path / "k.jsonl"
    target.write_bytes(b'{"meta": 1}\n{"row": 1}\n')
    staged = _fail_the_first_staged_file(monkeypatch, point)

    with pytest.raises(OSError):
        atomic_write_module.stage_replacement(target, '{"meta": 2}\n{"row": 1}\n')

    assert staged.get("name"), "the injection never saw a staged file"
    _assert_closed_exactly_once(staged)
    assert _temp_files(tmp_path) == []
    assert target.read_bytes() == b'{"meta": 1}\n{"row": 1}\n'


@pytest.mark.parametrize("point", _POINTS)
def test_a_synced_atomic_write_removes_its_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    target = tmp_path / "k.jsonl"
    target.write_bytes(b"original\n")
    staged = _fail_the_first_staged_file(monkeypatch, point)

    with pytest.raises(OSError):
        atomic_write_module.atomic_write(target, "replacement\n", fsync=True)

    assert staged.get("name"), "the injection never saw a staged file"
    _assert_closed_exactly_once(staged)
    assert _temp_files(tmp_path) == []
    assert target.read_bytes() == b"original\n"


@pytest.mark.parametrize("point", _POINTS)
def test_a_metadata_update_whose_staging_fails_leaves_no_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    log.append("k", "user", "a")
    log.append("k", "assistant", "b")
    staged = _fail_the_first_staged_file(monkeypatch, point)

    # The unlocked staging pass fails; the rewrite under the lock still lands.
    log.update_metadata("k", {"title": "renamed"})

    assert staged.get("name"), "the injection never saw a staged file"
    assert _temp_files(tmp_path) == []
    assert log.get_metadata("k")["title"] == "renamed"
    assert [m["content"] for m in ConversationLog(base_dir=tmp_path)._read_messages("k")] == [
        "a",
        "b",
    ]


def test_an_interrupt_before_the_publish_starts_removes_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A KeyboardInterrupt between staging and the publish's own cleanup leaks nothing.

    The interrupt is raised as the publish is entered, before its own ``try``, so
    only atomic_write's handling around the call can remove the staged copy.
    """
    target = tmp_path / "k.jsonl"
    target.write_bytes(b"original\n")
    entered: list[str] = []

    def interrupted_publish(staged: str, path: Any) -> None:
        entered.append(staged)
        raise KeyboardInterrupt

    monkeypatch.setattr(atomic_write_module, "publish_replacement", interrupted_publish)

    with pytest.raises(KeyboardInterrupt):
        atomic_write_module.atomic_write(target, "replacement\n", fsync=True)

    assert entered, "atomic_write never reached the publish"
    assert _temp_files(tmp_path) == []
    assert target.read_bytes() == b"original\n"
