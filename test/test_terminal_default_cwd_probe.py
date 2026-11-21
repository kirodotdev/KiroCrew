"""The Windows shell's default directory is stat'ed on the path-probe pool, not the loop.

``api_terminal_ws`` falls back to the home folder when the configured default is
not a directory. That check is ``terminal._probed_isdir``: the stat runs on the
path-probe pool, and a refused or unanswered probe counts as "not a directory".
"""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from kiro_crew.dashboard.handlers import terminal


@pytest.mark.asyncio
async def test_a_directory_is_a_directory_and_a_file_is_not(tmp_path):
    a_file = tmp_path / "f.txt"
    a_file.write_text("x", encoding="utf-8")

    assert await terminal._probed_isdir(str(tmp_path), 5.0) is True
    assert await terminal._probed_isdir(str(a_file), 5.0) is False
    assert await terminal._probed_isdir(str(tmp_path / "missing"), 5.0) is False


@pytest.mark.asyncio
async def test_the_stat_runs_off_the_event_loop_thread(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real_isdir = os.path.isdir

    def recording_isdir(path):
        seen.append(threading.get_ident())
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", recording_isdir)

    assert await terminal._probed_isdir(str(tmp_path), 5.0) is True
    assert seen and loop_thread not in seen


@pytest.mark.asyncio
async def test_a_refused_probe_counts_as_not_a_directory(tmp_path, monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise terminal._PathProbeBusy()

    monkeypatch.setattr(terminal, "_run_path_probe", refuse)

    assert await terminal._probed_isdir(str(tmp_path), 5.0) is False


@pytest.mark.asyncio
async def test_a_probe_that_does_not_answer_counts_as_not_a_directory(tmp_path, monkeypatch):
    never = asyncio.Event()

    async def hang(*_args, **_kwargs):
        await never.wait()
        return True

    monkeypatch.setattr(terminal, "_run_path_probe", hang)

    assert await terminal._probed_isdir(str(tmp_path), 0.05) is False
