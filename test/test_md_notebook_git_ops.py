"""Lifecycle tests for the Notes git subprocess boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.apps.builtins.md_notebook import git_ops


@pytest.mark.asyncio
async def test_run_git_tree_kills_and_reaps_after_a_timeout(monkeypatch) -> None:
    """A timed-out Git tree is cleaned through the bounded shared primitive."""
    communicate = MagicMock(return_value=_communicate_result())
    proc = SimpleNamespace(communicate=communicate)
    spawn = AsyncMock(return_value=proc)
    reap = AsyncMock()
    monkeypatch.setattr(git_ops.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(git_ops.platform_compat, "kill_and_reap", reap)
    monkeypatch.setattr(git_ops, "_git_bin", lambda: "git")

    async def _timeout(awaitable, timeout):
        assert timeout == 7
        # wait_for owns and cancels this first communicate coroutine on a real
        # timeout. Close it here so the test injects that state without a clock.
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(git_ops.asyncio, "wait_for", _timeout)

    with pytest.raises(git_ops.GitError, match="git status timed out after 7s"):
        await git_ops.run_git(["status"], timeout=7)

    reap.assert_awaited_once_with(proc)
    assert communicate.call_count == 1
    spawn_kwargs = spawn.await_args.kwargs
    assert spawn_kwargs["start_new_session"] is git_ops.platform_compat.IS_POSIX
    assert spawn_kwargs["creationflags"] == git_ops.platform_compat.CREATE_NEW_PROCESS_GROUP


async def _communicate_result() -> tuple[bytes, bytes]:
    """Coroutine closed by the deterministic wait_for timeout double."""
    return b"", b""
