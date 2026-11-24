"""``acp.session_provider_shared.shutdown_shared``: a KAS chat leaving a shared process.

Chat sharing is KAS-only, so these drive the real shutdown on a KAS runtime: the
eviction keeps the stored record, and every failure along the way (the cancel,
the eviction, the release, the kill) still returns the lease and never strands
the process. The doubles come from ``test_chat_runtime_sharing``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from test_chat_runtime_sharing import KillableRuntime, a_key, shared_provider, spawner

import kiro_crew.runtime_ownership as ro
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.testing.ids import unallocatable_pids


class KasRuntime(KillableRuntime):
    """A KAS runtime that records which session it kept the record of."""

    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.acp_backend = ACP_BACKEND_KAS
        self.kept: list[str] = []

    @contextlib.contextmanager
    def keep_record_on_teardown(self, session_id):
        self.kept.append(session_id)
        yield


@pytest.fixture
def table():
    ro._reset_for_tests()
    yield ro.RUNTIME_OWNERSHIP
    ro._reset_for_tests()


def _two_on(table, rt):
    one = asyncio.run(table.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
    two = asyncio.run(table.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))
    return one, two


def test_a_kas_chat_leaving_evicts_inside_the_record_keeping_scope(table):
    rt = KasRuntime(pid=9700)
    one, _two = _two_on(table, rt)
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", one.lease)
    handle.session_id = "sess-1"
    hooks = []
    handle._cancel_hook_tasks = lambda: hooks.append(1)
    asyncio.run(provider.shutdown())
    assert rt.kept == ["sess-1"] and handle.destroyed == 1
    assert hooks == [1]
    assert rt.kills == [] and table.leases_on_runtime(rt) == 1


def test_a_failed_cancel_still_evicts_and_releases(table):
    rt = KasRuntime(pid=9701)
    one, _two = _two_on(table, rt)
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", one.lease)
    handle.session_id = "sess-1"
    handle.is_turn_active = True

    async def boom():
        raise RuntimeError("cancel failed")

    handle.cancel = boom
    asyncio.run(provider.shutdown())
    assert handle.destroyed == 1 and table.leases_on_runtime(rt) == 1


def test_a_failed_eviction_still_returns_the_lease(table):
    rt = KasRuntime(pid=9702)
    one, _two = _two_on(table, rt)
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", one.lease)
    handle.session_id = "sess-1"

    async def boom():
        raise RuntimeError("session/close failed")

    handle.destroy = boom
    asyncio.run(provider.shutdown())
    assert provider._runtime_lease is None and table.leases_on_runtime(rt) == 1


def test_a_cancelled_eviction_returns_the_lease_then_reraises(table):
    rt = KasRuntime(pid=9703)
    (only,) = [asyncio.run(table.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))]
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", only.lease)
    handle.session_id = "sess-1"

    async def cancelled():
        raise asyncio.CancelledError()

    handle.destroy = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.shutdown())
    assert table.leases_on_runtime(rt) == 0
    assert rt.kills == ["provider shutdown (last shared session)"]


def test_no_lease_means_no_release_and_no_kill(table):
    rt = KasRuntime(pid=9704)
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", None)
    handle.session_id = "sess-1"
    asyncio.run(provider.shutdown())
    assert handle.destroyed == 1 and rt.kills == []


def test_a_failed_kill_of_the_last_holder_is_survived(table):
    rt = KasRuntime(pid=9706)
    (only,) = [asyncio.run(table.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))]
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", only.lease)
    handle.session_id = "sess-1"

    async def boom(**_kw):
        raise RuntimeError("kill failed")

    rt.kill = boom
    asyncio.run(provider.shutdown())
    assert provider._runtime_lease is None and table.leases_on_runtime(rt) == 0


CODEX_PID, SURVIVOR_PID = unallocatable_pids(2)


class CodexRuntime(KillableRuntime):
    """A codex runtime: its close leaves the session's MCP servers running, so only a
    kill that confirms the whole tree dead settles the close."""

    def __init__(self, pid: int, *, tree_dead: bool, kill_raises: bool = False) -> None:
        super().__init__(pid)
        from kiro_crew.acp_backends import ACP_BACKEND_CODEX

        self.acp_backend = ACP_BACKEND_CODEX
        self.process_tree_confirmed_dead = False
        self._unconfirmed_teardowns: set[str] = set()  # the host acknowledged the close
        self._tree_dead = tree_dead
        self._kill_raises = kill_raises

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        await super().kill(expected=expected, reason=reason)
        self.process_tree_confirmed_dead = self._tree_dead
        if self._kill_raises:
            raise RuntimeError("WindowsTreeDrainPending")


@pytest.mark.parametrize(
    ("tree_dead", "kill_raises", "confirmed"),
    [(False, False, False), (False, True, False), (True, True, False), (True, False, True)],
    ids=["survivors", "kill-raised", "raised-after-dead", "tree-dead"],
)
def test_a_codex_last_holder_settles_only_on_a_dead_tree(table, tree_dead, kill_raises, confirmed):
    """A clean ``session/close`` on codex is not a settled close: its MCP servers
    keep running under the process. Treat the close as confirmed and a kill that
    raised or left survivors reports a stopped Bot chat with no retry handle."""
    from kiro_crew.acp.session_provider_shared import RuntimeTreeGone

    rt = CodexRuntime(pid=CODEX_PID, tree_dead=tree_dead, kill_raises=kill_raises)
    only = asyncio.run(table.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
    provider, handle = shared_provider(rt, "dashboard:chat-1-1", only.lease)
    handle.session_id = "sess-1"
    asyncio.run(provider.shutdown())
    assert rt.kills and handle.destroyed == 1
    assert provider._kas_shutdown_unconfirmed is (not confirmed)
    if not confirmed:
        assert isinstance(provider._kas_unconfirmed_runtime, RuntimeTreeGone)
        assert provider._kas_unconfirmed_session_id == "sess-1"


def test_the_retry_handle_confirms_only_once_the_tree_is_gone(monkeypatch):
    import kiro_crew.session_pid_tracking as spt
    from kiro_crew.acp.session_provider_shared import RuntimeTreeGone

    rt = CodexRuntime(pid=CODEX_PID, tree_dead=False)
    handle = RuntimeTreeGone(rt)
    assert asyncio.run(handle.evict_session("sess-1")) is False  # still running
    rt._alive = False
    rt._unreaped_pids = (SURVIVOR_PID,)
    gone: set[int] = set()
    monkeypatch.setattr(spt, "_pid_gone_or_unmanaged", lambda pid: pid in gone)
    assert asyncio.run(handle.evict_session("sess-1")) is False  # a survivor lives
    gone.add(SURVIVOR_PID)
    assert asyncio.run(handle.evict_session("sess-1")) is True
    rt.process_tree_confirmed_dead = True
    gone.clear()
    assert asyncio.run(handle.evict_session("sess-1")) is True
