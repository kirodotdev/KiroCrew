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


# ---------------------------------------------------------------------------
# The failure message names the CAUSE, not only the symptom git repeats
# ---------------------------------------------------------------------------

#: git's access-rights boilerplate, emitted for every fetch/push failure, with
#: an SSH transport diagnosis printed ABOVE it. A remote reachable only through
#: a ``ProxyCommand``/host alias in ``~/.ssh/config`` fails exactly this way once
#: the sandbox's ``ssh -F /dev/null`` discards that config.
_SSH_RESOLVE_FAILURE = (
    "ssh: Could not resolve hostname git.example.com: Name or service not known\n"
    "fatal: Could not read from remote repository.\n"
    "\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists.\n"
)


def test_a_transport_cause_outside_the_tail_reaches_the_message():
    """The hostname that failed to resolve must survive into ``GitError``.

    A last-3-lines tail keeps only the access-rights boilerplate, so the one line
    naming the real cause is dropped and the operator is sent to check
    credentials for a host that never resolved.
    """
    detail = git_ops._failure_detail(_SSH_RESOLVE_FAILURE)

    assert "Could not resolve hostname git.example.com" in detail
    # The tail git always prints is still carried -- the cause is added, not swapped for.
    assert "correct access rights" in detail


def test_the_tail_alone_is_unchanged_when_it_already_carries_the_cause():
    """A cause inside the tail must not be repeated, and other failures are untouched."""
    short = "fatal: ambiguous argument 'HEAD': unknown revision\n"

    assert git_ops._failure_detail(short) == short.strip()
    # Nothing is prepended, so the existing message text is byte-identical.
    plain = "error: cannot lock ref 'refs/heads/main'\n" * 5
    assert git_ops._failure_detail(plain) == " ".join(plain.strip().splitlines()[-3:])


def test_an_unprefixed_transport_phrase_is_also_recognised():
    """Some transports relay the diagnosis without ssh's ``prog:`` prefix."""
    unprefixed = (
        "git@git.example.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository.\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"
    )

    detail = git_ops._failure_detail(unprefixed)

    assert "Permission denied (publickey)" in detail


def test_empty_stderr_does_not_raise_inside_the_message_builder():
    """A git failure with no stderr must still produce a message, not an IndexError."""
    assert git_ops._failure_detail("") == ""
    assert git_ops._failure_detail("\n\n") == ""


@pytest.mark.asyncio
async def test_run_git_reports_the_transport_cause_on_a_failed_fetch(monkeypatch) -> None:
    """End to end through ``run_git``: the raised ``GitError`` names the cause."""
    proc = SimpleNamespace(
        communicate=MagicMock(return_value=_stderr_result(_SSH_RESOLVE_FAILURE)),
        returncode=128,
    )
    monkeypatch.setattr(git_ops.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(git_ops, "_git_bin", lambda: "git")

    with pytest.raises(git_ops.GitError) as excinfo:
        await git_ops.run_git(["fetch", "--prune", "origin"])

    assert "Could not resolve hostname git.example.com" in str(excinfo.value)
    assert "failed (128)" in str(excinfo.value)


async def _stderr_result(text: str) -> tuple[bytes, bytes]:
    """A communicate() result whose stderr is *text* and whose exit code is non-zero."""
    return b"", text.encode()


@pytest.mark.asyncio
async def test_run_git_tree_kills_and_reaps_on_cancellation(monkeypatch) -> None:
    """A shutdown that cancels run_git reaps the detached child BEFORE propagating.

    The child is session-detached, so if cancellation propagated without the
    reap the ``git merge`` would keep rewriting the working tree after
    ``vault_write_lock`` releases and a restarted writer would overlap it. The
    reap must run and be awaited, and the original ``CancelledError`` must still
    propagate so the caller's cancellation semantics are unchanged.
    """
    communicate = MagicMock(return_value=_communicate_result())
    proc = SimpleNamespace(communicate=communicate)
    spawn = AsyncMock(return_value=proc)
    reap = AsyncMock()
    monkeypatch.setattr(git_ops.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(git_ops.platform_compat, "kill_and_reap", reap)
    monkeypatch.setattr(git_ops, "_git_bin", lambda: "git")

    async def _cancelled(awaitable, timeout):
        # wait_for owns and cancels the communicate coroutine when the task is
        # cancelled. Close it here, then surface the cancellation run_git must
        # handle without a real event-loop shutdown.
        awaitable.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(git_ops.asyncio, "wait_for", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await git_ops.run_git(["merge", "--no-edit", "FETCH_HEAD"], timeout=7)

    reap.assert_awaited_once_with(proc)
    assert communicate.call_count == 1
