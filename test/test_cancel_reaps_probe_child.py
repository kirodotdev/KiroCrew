"""A cancelled probe, divergence count or pip run must not orphan its child.

The remote crew diagnosis's ssh probes (``instances.diagnostics._run_ok`` and
``_run_stdout``), the update check's ``git_divergence.count_divergence`` and the
embeddings setup's two pip runs each killed their child only when the wait timed
out. A cancel (an instance edit, a gateway shutdown) ended the wait and left the
child running with no owner.

Each test starts the call as a task, waits until the child is being awaited,
cancels it, and asserts the cancel propagated with the child killed. The fake
child has no process tree, so only the tree kill is neutralised; the shielded
``platform_compat.kill_and_reap`` itself runs, and its own shielding is covered
by ``test_platform_compat_kill_reap.py``.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import git_divergence, platform_compat
from kiro_crew.dashboard.handlers import memory, memory_embeddings
from kiro_crew.instances import diagnostics
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until


class _RunsUntilKilled:
    """A child whose ``wait()`` and ``communicate()`` block until it is killed."""

    def __init__(self) -> None:
        self.pid = UNALLOCATABLE_PID
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self._killed = asyncio.Event()

    async def _block(self) -> None:
        self.started.set()
        await self._killed.wait()

    async def wait(self) -> int:
        await self._block()
        return -9

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._block()
        return b"", b""

    def kill(self) -> None:
        self.returncode = -9
        self._killed.set()


@pytest.fixture
def child(monkeypatch: pytest.MonkeyPatch) -> _RunsUntilKilled:
    """The one child every spawn below returns."""
    proc = _RunsUntilKilled()

    async def _spawn(*_argv: Any, **_kwargs: Any) -> _RunsUntilKilled:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(memory_embeddings, "create_subprocess_limited", _spawn)
    return proc


@pytest.fixture(autouse=True)
def _no_tree_kill(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake child has no tree to signal; its pid-scoped ``kill()`` is what counts."""

    async def _skip(pid: int, sig: int) -> bool:
        return True

    _floor_monkeypatch.setattr(platform_compat, "kill_process_tree_async", _skip)


@pytest.fixture
def pip_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send the embeddings setup down its pip paths with no sandbox in the way."""

    async def _unwrapped(argv: list[str], **_kwargs: Any) -> tuple[list[str], None]:
        return argv, None

    monkeypatch.setitem(sys.modules, "pip", None)
    monkeypatch.setitem(sys.modules, "faiss", None)
    monkeypatch.setattr(memory_embeddings, "wrap_argv_async", _unwrapped)
    monkeypatch.setattr(memory_embeddings, "cgroup_scope_argv", lambda argv: argv)


async def _cancel_once_running(call: Any, proc: _RunsUntilKilled) -> None:
    task = asyncio.create_task(call)
    await await_until(proc.started, what="the child being awaited")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(task, what="the cancelled call finishing")


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["_run_ok", "_run_stdout"])
async def test_a_cancelled_diagnosis_probe_kills_its_ssh_child(
    monkeypatch: pytest.MonkeyPatch, child: _RunsUntilKilled, probe: str
) -> None:
    monkeypatch.setattr(diagnostics, "ssh_spawn_argv_env", lambda argv: (argv, None))
    await _cancel_once_running(getattr(diagnostics, probe)(["ssh"], FALLBACK_TIMEOUT_SECS), child)
    assert child.returncode == -9, f"a cancelled {probe} left its ssh child running"


@pytest.mark.asyncio
async def test_a_cancelled_divergence_count_kills_its_git_child(child: _RunsUntilKilled) -> None:
    call = git_divergence.count_divergence("/repo", "@{u}", timeout=FALLBACK_TIMEOUT_SECS)
    await _cancel_once_running(call, child)
    assert child.returncode == -9, "a cancelled divergence count left git running"


@pytest.mark.asyncio
async def test_a_cancelled_pip_bootstrap_kills_its_child(
    child: _RunsUntilKilled, pip_missing: None
) -> None:
    await _cancel_once_running(memory_embeddings._ensure_pip_available(), child)
    assert child.returncode == -9, "a cancelled ensurepip bootstrap left its child running"


@pytest.mark.asyncio
async def test_a_cancelled_faiss_install_kills_its_child(
    monkeypatch: pytest.MonkeyPatch, child: _RunsUntilKilled, pip_missing: None
) -> None:
    async def _allowed(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _pip_ready() -> tuple[bool, str]:
        return True, ""

    # The facade binds this name too, so it is patched through the facade.
    monkeypatch.setattr(memory, "require_owner_dashboard_request", _allowed)
    monkeypatch.setattr(memory_embeddings, "_ollama_provider_refusal", _allowed)
    monkeypatch.setattr(memory_embeddings, "_ensure_pip_available", _pip_ready)
    monkeypatch.setattr(memory_embeddings, "model_download_manager", MagicMock)
    monkeypatch.setattr(memory_embeddings, "model_file_present", lambda: True)
    monkeypatch.setattr(memory_embeddings, "_embedding_setup_status", {"step": "idle", "error": ""})

    await _cancel_once_running(memory_embeddings.api_memory_enable_embeddings(MagicMock()), child)
    assert child.returncode == -9, "a cancelled faiss-cpu install left its child running"
