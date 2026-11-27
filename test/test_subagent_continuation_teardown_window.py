"""A continuation never claims the session its conversation's earlier run is tearing down.

A sub-agent run ends by releasing its session's lease and resetting the session
(``run_lifecycle._teardown_run_session_impl``). A ``spawn_continue`` that arrived
after the run was marked done waits on that lease. The reset pops the session in
its first hold of the registry lock, and a claimant validates under that lock
after it, so the claimant finds the key unregistered and starts a session of its
own. A KAS run's session is built for the parents known at its start
(``acp.kas_parent_limits``), so a continuation by another session must get a new
one. The session manager is real; only the provider is faked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import async_wait_until, await_until

_KEY = "subagent:conv1"


def _manager() -> tuple[SessionManager, list]:
    providers: list = []

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.disown_work_dir = MagicMock()
        provider.context_usage_pct = lambda: 0.0
        provider.context_window_tokens = lambda: 0
        provider.has_active_turn = lambda: False
        provider.runtime_abort_target = lambda: None
        provider.spawn_identity = ""
        provider._runtime = None
        provider.cwd = kwargs.get("cwd") or ""
        providers.append(provider)
        return provider

    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    cfg.session.pool_size = 0
    return SessionManager(cfg, provider_factory=factory), providers


async def _end_run(manager: SessionManager) -> None:
    """The run's own ending of its session: the lease, then the reset, with no step between."""
    manager.release(_KEY, cleanup=False)
    await asyncio.wait_for(manager.reset(_KEY), timeout=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("registry_lock_busy", [False, True])
async def test_a_claim_waiting_on_an_ending_run_gets_a_session_of_its_own(registry_lock_busy):
    manager, providers = _manager()
    free = asyncio.Event()
    helpers: list[asyncio.Task] = []
    try:
        first, _is_new, _resumed = await manager.get_or_create(_KEY, agent="plain")
        ending = manager._sessions[_KEY]
        continuation = asyncio.create_task(manager.get_or_create(_KEY, agent="plain"))
        helpers.append(continuation)
        await async_wait_until(
            lambda: bool(ending.semaphore._waiters),
            describe=lambda: "the continuation is not waiting on the ending run's lease",
        )
        if registry_lock_busy:
            # Another start holds the registry lock across the run's release and reset.
            async def hold() -> None:
                async with manager._lock:
                    await await_until(free, what="the test freeing the registry lock")

            holder = asyncio.create_task(hold())
            helpers.append(holder)
            await async_wait_until(
                manager._lock.locked, describe=lambda: "the registry lock is not held"
            )
            ended = asyncio.create_task(_end_run(manager))
            helpers.append(ended)
            await async_wait_until(
                lambda: len(manager._lock._waiters or ()) >= 2,
                describe=lambda: "the reset and the claim are not both waiting on the lock",
            )
            free.set()
            await await_until(asyncio.gather(holder, ended), what="the run's reset")
        else:
            await _end_run(manager)
        second, _is_new, _resumed = await await_until(continuation, what="the continuation")
        manager.release(_KEY)
    finally:
        # A step that failed above leaves its helpers where they were, the holder on
        # the registry lock that ``close_all`` takes first. Each is let go and ended
        # before the manager closes, so the failure is the one the step raised.
        free.set()
        for task in helpers:
            task.cancel()
        await await_until(
            asyncio.gather(*helpers, return_exceptions=True), what="the test's own tasks ending"
        )
        await manager.close_all()
    assert second is not first and providers == [first, second]
    first.shutdown.assert_awaited()


def test_a_run_awaits_nothing_between_releasing_its_lease_and_resetting_its_session():
    """The property above holds only while no step separates the two: a claimant
    woken by the release would validate before the pop and take the ending session."""
    import ast
    from pathlib import Path

    from kiro_crew.subagent_manager import run_lifecycle

    tree = ast.parse(Path(run_lifecycle.__file__).read_text(encoding="utf-8"))
    teardown = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_teardown_run_session_impl"
    )
    # Every construct that can suspend: an await, an async with, an async for.
    suspends = (ast.Await, ast.AsyncWith, ast.AsyncFor)
    awaits = [node for node in ast.walk(teardown) if isinstance(node, suspends)]
    [reset] = [
        node.lineno
        for node in awaits
        if isinstance(node, ast.Await) and ".reset(" in ast.unparse(node)
    ]
    release = max(
        node.lineno
        for node in ast.walk(teardown)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "release"
        and node.lineno < reset
    )
    assert [node.lineno for node in awaits if release < node.lineno < reset] == []
