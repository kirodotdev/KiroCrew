"""``rename_with_retry``: the no-overwrite twin of ``replace_with_retry``.

``os.rename`` is patched to raise ``PermissionError`` a set number of times, so these
drive the retry's decisions on any OS: retried on Windows, re-raised at once on POSIX
and on the event loop, bounded by the shared attempt count, and never retrying a
refusal to overwrite.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat


@pytest.fixture(autouse=True)
def _no_backoff_sleep(_floor_monkeypatch):
    """Keep the bounded retry loop instant; attempt COUNT is what these pin."""
    _floor_monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def _contended_rename(monkeypatch, faults: int) -> list[tuple[str, str]]:
    """Make the first *faults* renames raise the sharing violation, then rename for real."""
    real = os.rename
    calls: list[tuple[str, str]] = []

    def _rename(src, dst):
        calls.append((src, dst))
        if len(calls) <= faults:
            raise PermissionError(32, "The process cannot access the file", src)
        real(src, dst)

    monkeypatch.setattr(aw.os, "rename", _rename)
    return calls


def test_windows_contention_is_retried_until_the_rename_lands(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    (tmp_path / "a.md").write_text("note", encoding="utf-8")
    calls = _contended_rename(monkeypatch, faults=2)

    aw.rename_with_retry(tmp_path / "a.md", tmp_path / "b.md")

    assert len(calls) == 3
    assert (tmp_path / "b.md").read_text(encoding="utf-8") == "note"
    assert not (tmp_path / "a.md").exists()


def test_windows_gives_up_after_the_shared_attempt_count(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    (tmp_path / "a.md").write_text("note", encoding="utf-8")
    calls = _contended_rename(monkeypatch, faults=10_000)

    with pytest.raises(PermissionError):
        aw.rename_with_retry(tmp_path / "a.md", tmp_path / "b.md")

    assert len(calls) == aw._REPLACE_MAX_ATTEMPTS
    assert (tmp_path / "a.md").exists()


def test_a_zero_attempt_count_still_renames_once(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_MAX_ATTEMPTS", 0)
    (tmp_path / "a.md").write_text("note", encoding="utf-8")
    calls = _contended_rename(monkeypatch, faults=0)

    aw.rename_with_retry(tmp_path / "a.md", tmp_path / "b.md")

    assert len(calls) == 1
    assert (tmp_path / "b.md").exists()


def test_posix_permission_error_surfaces_without_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    (tmp_path / "a.md").write_text("note", encoding="utf-8")
    calls = _contended_rename(monkeypatch, faults=1)

    with pytest.raises(PermissionError):
        aw.rename_with_retry(tmp_path / "a.md", tmp_path / "b.md")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_caller_on_the_event_loop_reraises_instead_of_sleeping(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    (tmp_path / "a.md").write_text("note", encoding="utf-8")
    calls = _contended_rename(monkeypatch, faults=2)

    with pytest.raises(PermissionError):
        aw.rename_with_retry(tmp_path / "a.md", tmp_path / "b.md")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_offloading_from_the_loop_keeps_the_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    (tmp_path / "a.md").write_text("note", encoding="utf-8")
    calls = _contended_rename(monkeypatch, faults=2)

    await asyncio.to_thread(aw.rename_with_retry, tmp_path / "a.md", tmp_path / "b.md")

    assert len(calls) == 3
    assert (tmp_path / "b.md").exists()


def test_an_existing_destination_is_never_overwritten_or_retried(tmp_path, monkeypatch):
    """A refusal to overwrite is not the sharing window, so it surfaces on the first try."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    (tmp_path / "a.md").write_text("note", encoding="utf-8")
    (tmp_path / "b.md").write_text("other", encoding="utf-8")
    real = os.rename
    calls: list[str] = []

    def _rename(src, dst):
        calls.append(src)
        if os.path.exists(dst):
            raise FileExistsError(183, "Cannot create a file when that file already exists", dst)
        real(src, dst)

    monkeypatch.setattr(aw.os, "rename", _rename)

    with pytest.raises(FileExistsError):
        aw.rename_with_retry(tmp_path / "a.md", tmp_path / "b.md")

    assert len(calls) == 1
    assert (tmp_path / "b.md").read_text(encoding="utf-8") == "other"
