"""A fire-and-forget task in the runtime modules outlives ``gc.collect()``.

The event loop keeps only a weak reference to a task. A task nothing else holds
can be collected while it is still waiting, and its work silently never
finishes. Each site below keeps the task it starts in a set until it is done.

The scheduled coroutine is parked on a future that only its own frame can
reach. That is what makes the test honest: had the coroutine parked on
something the test holds (an ``asyncio.Event``), the waiter's callback would
keep the task alive and the test would pass with or without the fix. With the
private future, the task is a pure reference cycle, so ``gc.collect()`` frees
it unless the module's set holds it.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.testing.wait import await_until


class _Park:
    """Stands in for the coroutine a site schedules, and parks it privately.

    The private future is reachable only from the parked frame; the test reads
    it back through a weak reference, which is alive exactly when the task is.
    """

    def __init__(self) -> None:
        self.parked = asyncio.Event()
        self.future_ref: weakref.ref[asyncio.Future[None]] | None = None
        self.task_ref: weakref.ref[asyncio.Task[Any]] | None = None
        self.finished = False

    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        current = asyncio.current_task()
        assert current is not None
        self.task_ref = weakref.ref(current)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.future_ref = weakref.ref(future)
        self.parked.set()
        await future
        self.finished = True

    async def survives_collection_and_finishes(self, owner: Any, name: str) -> None:
        """``owner.<name>`` is the set that should hold the task.

        Read only after the collection check, with an empty default, so code
        that keeps no set fails on the symptom (the task is gone) rather than
        on the missing attribute.
        """
        await await_until(self.parked, what="the scheduled coroutine to park")
        assert self.task_ref is not None and self.future_ref is not None
        gc.collect()
        task = self.task_ref()
        future = self.future_ref()
        assert task is not None, "the task was garbage-collected while it was still running"
        assert future is not None
        held: set[Any] = getattr(owner, name, set())
        assert task in held, "the task is not held by the module's set"
        future.set_result(None)
        await await_until(task, what="the released task to finish")
        assert self.finished is True
        assert task not in held, "a finished task is still held"


@pytest.mark.asyncio
async def test_the_subagent_cleanup_task_outlives_a_collection() -> None:
    from kiro_crew.session import SessionManager

    cfg = MagicMock()
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    manager = SessionManager(cfg=cfg, provider_factory=None)

    park = _Park()
    provider = MagicMock()
    provider.session_id = "test-session-uuid"
    provider.cleanup_session = park
    session = MagicMock()
    session.provider = provider
    session.semaphore = asyncio.Semaphore(0)
    manager._sessions["subagent:abc123"] = session

    manager.release("subagent:abc123", cleanup=True)
    del provider, session

    await park.survives_collection_and_finishes(manager, "_background_tasks")


@pytest.mark.asyncio
async def test_the_idle_resize_task_outlives_a_collection() -> None:
    from kiro_crew.knowledge import llm_pool

    pool = llm_pool.LLMPool(2)
    park = _Park()
    pool._maybe_scale_to_zero = park  # type: ignore[method-assign]
    pool._resize_when_idle = True
    pool._in_use = 1

    pool.release(0)

    await park.survives_collection_and_finishes(llm_pool, "_BACKGROUND_TASKS")


@pytest.mark.asyncio
async def test_the_probe_teardown_task_outlives_a_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.instances import ssh_tunnel_manager, ssh_tunnel_manager_child
    from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

    # Patched on the facade only: it mirrors the name into the owner module.
    monkeypatch.setattr(ssh_tunnel_manager, "_PROBE_INTERVAL", 0.01)
    tunnel = _SshTunnel("cd-1", "cd-1-alias", 53997, 7777, probe_failure_threshold=1)
    tunnel.status.state = TunnelState.CONNECTED

    async def _far_end_dead() -> bool:
        return False

    park = _Park()
    tunnel._forward_alive = _far_end_dead  # type: ignore[assignment]
    tunnel._terminate = park  # type: ignore[assignment]

    await await_until(asyncio.ensure_future(tunnel._probe_loop()), what="the probe loop to exit")

    await park.survives_collection_and_finishes(ssh_tunnel_manager_child, "_BACKGROUND_TASKS")


@pytest.mark.asyncio
async def test_the_shared_crew_child_cancel_task_outlives_a_collection() -> None:
    from kiro_crew.shared_crew import admission_entry

    park = _Park()
    manager = SimpleNamespace(
        cancel_queued=park,
        cancel=object(),
        _agents={},
        _admission=SimpleNamespace(starting_claim_of=lambda _id: None),
    )
    work = SimpleNamespace(extra={"execution_id": "child"}, session_key="dashboard:parent")

    admission_entry.subagent_cancel(manager, asyncio.get_running_loop())(work)

    await park.survives_collection_and_finishes(admission_entry, "_BACKGROUND_TASKS")


@pytest.mark.asyncio
async def test_the_shared_crew_run_cancel_task_outlives_a_collection() -> None:
    from kiro_crew.shared_crew import admission_entry

    park = _Park()
    admission_entry._cancel_hook(park)(SimpleNamespace(extra={"execution_id": "run-1"}))

    await park.survives_collection_and_finishes(admission_entry, "_BACKGROUND_TASKS")


@pytest.mark.asyncio
async def test_a_sprites_terminal_send_outlives_a_collection() -> None:
    from kiro_crew.sprites_session import terminal

    park = _Park()
    terminal._send_in_background(park())

    await park.survives_collection_and_finishes(terminal, "_BACKGROUND_TASKS")


@pytest.mark.asyncio
async def test_the_remote_worker_sigterm_retire_outlives_a_collection() -> None:
    from kiro_crew.remote_session import worker as worker_mod

    worker = worker_mod.Worker("sid-1", b"s" * 32, ["true"])
    park = _Park()
    worker._on_term = park  # type: ignore[method-assign]

    worker._on_sigterm()

    await park.survives_collection_and_finishes(worker, "_term_tasks")


@pytest.mark.asyncio
async def test_an_artifact_knowledge_sync_task_outlives_a_collection() -> None:
    from kiro_crew.knowledge.artifact_ingest import ArtifactKnowledgeSync

    sync = ArtifactKnowledgeSync(MagicMock(), MagicMock(), set(), asyncio.get_running_loop())
    park = _Park()
    sync._handle = park  # type: ignore[method-assign]

    sync._schedule("save", "an-artifact")

    await park.survives_collection_and_finishes(sync, "_tasks")


@pytest.mark.asyncio
async def test_a_crew_log_flush_outlives_a_collection() -> None:
    from kiro_crew.dashboard.handlers.crew_log import CrewLogPublisher

    publisher = CrewLogPublisher(MagicMock())
    publisher._loop = asyncio.get_running_loop()
    park = _Park()
    publisher._flush = park  # type: ignore[method-assign]

    publisher._run()

    await park.survives_collection_and_finishes(publisher, "_tasks")
    assert publisher._flushing is False, "the finished flush did not let the next one start"


@pytest.mark.asyncio
async def test_a_crew_log_session_send_outlives_a_collection() -> None:
    from kiro_crew.dashboard.handlers.crew_log import CrewLogPublisher

    publisher = CrewLogPublisher(MagicMock())
    publisher._loop = asyncio.get_running_loop()
    publisher._pending = {("sid", "status"): (1, 1, {})}
    park = _Park()
    publisher._send_pending = park  # type: ignore[method-assign]

    publisher._drain_sessions()

    await park.survives_collection_and_finishes(publisher, "_tasks")
