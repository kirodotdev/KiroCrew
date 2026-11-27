"""A shared sub-agent is a tenant of its parent's pid mapping while its session lives.

A ``spawn_run`` child on its parent's runtime is never in the session manager's
registry, so the manager's process snapshot cannot list it. Its process tree
inherits the parent's ``KIROCREW_HOST_PID``, so a caller in it with no token of
its own reads the parent's pid mapping. That mapping must say the pid is shared
while the child lives, or the caller resolves as the parent.

Real ``SessionManager``, ``SubagentManager``, ``AcpRuntime`` registry and handle
destructor; only the process transport is substituted.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, patch

import pytest
from test_update_provider import _UNALLOCATABLE_PID

import kiro_crew.mcp_caller
from kiro_crew import platform_compat, session_pid_sig
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.session import SessionManager
from kiro_crew.session_parts.record import _Session
from kiro_crew.session_pid_sig import read_session_pid_mapping
from kiro_crew.subagent import SubagentInfo, SubagentManager

PID = _UNALLOCATABLE_PID
PARENT = "dashboard:chat-1"
CHILD = "subagent:shared-child"


@pytest.fixture(name="pid_home")
def pid_home_fixture(tmp_path: Path):
    """A private mapping dir, read back by the real reader and the real ladder."""
    (tmp_path / "sel_hmac.key").write_bytes(b"\x01" * 32)
    with (
        patch.object(session_pid_sig, "config_dir", return_value=tmp_path),
        patch.object(session_pid_sig, "_sel_hmac_key_bytes", return_value=None),
        patch.object(platform_compat, "get_process_start_id", return_value=None),
        patch.object(session_pid_sig, "sel_hmac_key_path", return_value=tmp_path / "sel_hmac.key"),
        patch("kiro_crew.config.paths.config_dir", return_value=tmp_path),
    ):
        yield tmp_path


def _tokenless_identity() -> kiro_crew.mcp_caller.OwnIdentity:
    """What a tokenless caller in the child's process tree resolves to."""
    with (
        mock.patch.dict(
            os.environ, {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": str(PID)}, clear=False
        ),
        patch.object(kiro_crew.mcp_caller, "session_key_from_env_token", return_value=""),
    ):
        return kiro_crew.mcp_caller.resolve_own_identity(consult_protected_binding=False)


def _runtime(tmp_path: Path) -> AcpRuntime:
    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._process = SimpleNamespace(returncode=None)
    runtime._pid = PID
    runtime._dead = False
    runtime.terminate_session = AsyncMock(wraps=runtime.terminate_session)
    runtime.send_notification = AsyncMock()
    runtime._send_and_await = AsyncMock(return_value={})
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_confirmed", [True, False], ids=["close-confirmed", "close-unconfirmed"]
)
async def test_a_shared_child_makes_its_parents_pid_shared_until_it_closes(
    pid_home, tmp_path, close_confirmed
):
    """HEADLINE: while the child lives, a tokenless lookup on the pid fails.

    A wrong name is worse than no name: the parent's key would carry the
    parent's tool policy, secrets scope and peer attestation into the child.
    A close the host never answered may leave the child running, so the pid
    stays shared until the close is confirmed.
    """
    sessions = SessionManager(KiroCrewConfig())
    manager = SubagentManager(sessions=sessions, ctx_builder=SimpleNamespace())
    sessions.set_child_teardown_handler(manager)
    runtime = _runtime(tmp_path)

    parent_queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["sess-parent"] = parent_queue
    parent = AcpSessionProvider(
        AcpSessionHandle("sess-parent", parent_queue, runtime),
        runtime,
        owns_runtime=True,
        session_key=PARENT,
    )
    sessions._sessions[PARENT] = _Session(
        provider=SimpleNamespace(client=parent, _client=parent, session_id="sess-parent"),
        agent="kirocrew",
    )

    child_queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["sess-child"] = child_queue
    handle = AcpSessionHandle("sess-child", child_queue, runtime)
    handle.keep_transcript = True
    runtime.create_session = AsyncMock(return_value=handle)
    sessions.get_subagent_runtime = AsyncMock(return_value=runtime)
    info = SubagentInfo(id="shared-child", task="t", parent_session_key=PARENT, cwd=str(tmp_path))
    manager._agents[info.id] = info
    try:
        await publish_turn_identity(sessions, PARENT)
        assert read_session_pid_mapping(PID, pid_home).session_key == PARENT
        assert _tokenless_identity().session_key == PARENT

        # Binding the child re-publishes the parent's mapping before its first prompt.
        await manager._create_shared_session(info, CHILD, "kirocrew")
        assert not sessions.has_session(CHILD)
        mapping = read_session_pid_mapping(PID, pid_home)
        assert mapping.shared
        assert set(mapping.tenants) == {PARENT, CHILD}
        assert _tokenless_identity().failed

        # The parent's own next turn keeps the child in the set.
        await publish_turn_identity(sessions, PARENT)
        assert set(read_session_pid_mapping(PID, pid_home).tenants) == {PARENT, CHILD}
        assert _tokenless_identity().failed

        if not close_confirmed:
            runtime._send_and_await = AsyncMock(side_effect=asyncio.TimeoutError())
        await manager._teardown_run_session(info, CHILD)
        assert "sess-child" not in runtime._session_queues
        # Until the parent republishes, the closed child is still listed: that
        # refuses the parent's tokenless callers, which fails closed.
        assert read_session_pid_mapping(PID, pid_home).shared
        await publish_turn_identity(sessions, PARENT)
        mapping = read_session_pid_mapping(PID, pid_home)
        if not close_confirmed:
            assert "sess-child" in runtime._unconfirmed_teardowns
            assert set(mapping.tenants) == {PARENT, CHILD}
            assert _tokenless_identity().failed
            # Neither the run's end, its dismissal (the record leaves the
            # manager) nor a dead transport over a live process drops it.
            info.done = True
            assert await manager.settle_before_delete(info.id) == "delivered"
            assert info.id not in manager._agents
            runtime._dead = True
            await publish_turn_identity(sessions, PARENT)
            assert set(read_session_pid_mapping(PID, pid_home).tenants) == {PARENT, CHILD}
            assert _tokenless_identity().failed
            runtime._dead = False
            # Once the host confirms the close, the pid is the parent's alone again.
            runtime._unconfirmed_teardowns.discard("sess-child")
            await publish_turn_identity(sessions, PARENT)
            assert not read_session_pid_mapping(PID, pid_home).shared
            assert manager._shared_tenants == {}
            return
        # After a confirmed close, the parent's next turn has the pid alone again.
        assert not mapping.shared
        assert mapping.session_key == PARENT
        identity = _tokenless_identity()
        assert identity.session_key == PARENT and not identity.failed
    finally:
        runtime.unregister_session("sess-child")
        runtime.unregister_session("sess-parent")
        sessions._sessions.pop(PARENT, None)
        manager.close()
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_child_does_not_start_when_its_tenancy_cannot_be_published(pid_home, tmp_path):
    """A failed write leaves the parent-only mapping; the child must not run on it."""
    import errno

    from kiro_crew.messaging import identity

    sessions = SessionManager(KiroCrewConfig())
    manager = SubagentManager(sessions=sessions, ctx_builder=SimpleNamespace())
    sessions.set_child_teardown_handler(manager)
    runtime = _runtime(tmp_path)
    parent_queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["sess-parent"] = parent_queue
    parent = AcpSessionProvider(
        AcpSessionHandle("sess-parent", parent_queue, runtime),
        runtime,
        owns_runtime=True,
        session_key=PARENT,
    )
    sessions._sessions[PARENT] = _Session(
        provider=SimpleNamespace(client=parent, _client=parent, session_id="sess-parent"),
        agent="kirocrew",
    )
    queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["sess-child"] = queue
    runtime.create_session = AsyncMock(return_value=AcpSessionHandle("sess-child", queue, runtime))
    sessions.get_subagent_runtime = AsyncMock(return_value=runtime)
    info = SubagentInfo(id="shared-child", task="t", parent_session_key=PARENT, cwd=str(tmp_path))
    manager._agents[info.id] = info
    real = session_pid_sig.publish_session_pid

    def _disk_full(pid, key, **kwargs):
        if kwargs.get("co_tenants"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(pid, key, **kwargs)

    try:
        await publish_turn_identity(sessions, PARENT)
        with patch.object(identity, "publish_session_pid", side_effect=_disk_full):
            with pytest.raises(RuntimeError, match="co-tenant"):
                await manager._create_shared_session(info, CHILD, "kirocrew")
        assert read_session_pid_mapping(PID, pid_home).session_key == PARENT
        # The bind closed the session itself: the run's start may now fall back
        # to a dedicated process and clear its shared ownership.
        assert "sess-child" not in runtime._session_queues
    finally:
        runtime.unregister_session("sess-child")
        runtime.unregister_session("sess-parent")
        sessions._sessions.pop(PARENT, None)
        manager.close()
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_bind_cancelled_during_setup_still_records_the_tenant(tmp_path, monkeypatch):
    """The handle exists before the bind's first await, so its tenancy must too."""
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    sessions = SessionManager(KiroCrewConfig())
    manager = SubagentManager(sessions=sessions, ctx_builder=SimpleNamespace())
    sessions.set_child_teardown_handler(manager)
    runtime = _runtime(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["sess-child"] = queue
    handle = AcpSessionHandle("sess-child", queue, runtime)
    info = SubagentInfo(id="shared-child", task="t", parent_session_key=PARENT, cwd=str(tmp_path))
    manager._agents[info.id] = info

    async def _cancelled(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(RunEventCoordinator, "_remember_identity_off_loop", _cancelled)
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager._bind_shared_handle(info, CHILD, runtime, handle)
        assert sessions.shared_session_keys_on_pid(PID) == (CHILD,)
    finally:
        runtime.unregister_session("sess-child")
        manager.close()
        await sessions.close_all()


@pytest.mark.asyncio
async def test_a_child_on_another_runtime_is_not_a_tenant_of_the_parents_pid(tmp_path):
    """The roster is per process: a live shared child on a different pid is not counted."""
    sessions = SessionManager(KiroCrewConfig())
    manager = SubagentManager(sessions=sessions, ctx_builder=SimpleNamespace())
    sessions.set_child_teardown_handler(manager)
    runtime = _runtime(tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    runtime._session_queues["sess-child"] = queue
    runtime.create_session = AsyncMock(return_value=AcpSessionHandle("sess-child", queue, runtime))
    sessions.get_subagent_runtime = AsyncMock(return_value=runtime)
    info = SubagentInfo(id="shared-child", task="t", parent_session_key=PARENT, cwd=str(tmp_path))
    manager._agents[info.id] = info
    try:
        await manager._create_shared_session(info, CHILD, "kirocrew")
        assert sessions.shared_session_keys_on_pid(PID) == (CHILD,)
        assert sessions.shared_session_keys_on_pid(PID - 1) == ()
        # A finished run whose session is still registered stays a tenant.
        info.done = True
        assert sessions.shared_session_keys_on_pid(PID) == (CHILD,)
        runtime.unregister_session("sess-child")
        assert sessions.shared_session_keys_on_pid(PID) == ()
        # An exited process hosts nothing, even with a close left unconfirmed.
        manager._shared_tenants["sess-gone"] = (runtime, "subagent:gone")
        runtime._unconfirmed_teardowns.add("sess-gone")
        runtime._process = SimpleNamespace(returncode=0)
        assert sessions.shared_session_keys_on_pid(PID) == ()
        assert manager._shared_tenants == {}
    finally:
        runtime.unregister_session("sess-child")
        manager.close()
        await sessions.close_all()
