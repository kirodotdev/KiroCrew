"""The gateway services a memory route reaches: the Security Event Log and the vector store.

``_sel`` resolves the SEL at call time, so a patch of the handlers' SEL reaches every
route that audits; ``_get_vector_store`` returns the gateway's own vector store,
and ``_get_vector_store_async`` is its twin for handlers, which must not run the
store's first ``init()`` on the event loop. The facade and ``memory_embeddings``
import them from here.

Served through :mod:`kiro_crew.dashboard.handlers.memory`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio

from kiro_crew.dashboard.handlers._shared import _get_memory
from kiro_crew.dashboard.state import DashboardState


def _sel():
    """Late-binding sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


def _get_vector_store(state: DashboardState):
    """Get VectorMemoryStore from context_builder's memory, or create standalone."""
    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready()
    mem = _get_memory(state)
    if mem.vector_store:
        return mem.vector_store
    # Fallback: create standalone
    # COUPLING: ``_get_vector_store_async``'s fast-path predicate mirrors the
    # resolution above. A new ``init()``-bearing branch added here must be
    # reflected there, or async handlers may run it on the event loop again.
    if not hasattr(state, "_standalone_vector"):
        # Both imports resolve their target at CALL time, which is what lets a test
        # substitute the attribute on the source module and have this function
        # observe it. ``KiroCrewConfig`` is deliberately imported here, not at top
        # level: a top-level binding would capture the original object at import
        # time and would not see such a substitution.
        from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811
        from kiro_crew.vector_memory import VectorMemoryStore  # noqa: F811

        cfg = KiroCrewConfig.load()
        store = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim, config=cfg)
        store.init()
        state._standalone_vector = store  # type: ignore[attr-defined]
        mem.vector_store = store
    return state._standalone_vector  # type: ignore[attr-defined]


async def _get_vector_store_async(state: DashboardState):
    """Async facade over ``_get_vector_store`` honouring init's caller contract.

    ``VectorMemoryStore.init()`` documents that async callers must offload it
    (it is blocking file IO end to end — sqlite connect, migrations, the
    owner-only lockdown pass), so the standalone fallback inside
    ``_get_vector_store`` must not run inline in a handler. Fast path: when a
    store is already resolvable without running ``init()`` — the
    context_builder supplied one, or a prior call cached the standalone
    fallback on ``state`` — delegate synchronously, so the common request path
    pays no thread hop. In both fast-path cases ``_get_vector_store`` returns
    before reaching its fallback, so ``init()`` stays unreachable on the loop.
    """
    # Resolve the memory store ON the loop: ``_get_memory``'s
    # check-create-publish of ``state._standalone_memory`` is atomic here (no
    # await), exactly as it is for every synchronous caller. Resolving it only
    # inside the worker would race a concurrent loop-side ``_get_memory`` into
    # publishing a second MemoryStore, detaching ``vector_store`` from the
    # object every other handler reads. MemoryStore's own ``init()`` is a
    # cheap mkdir+seed (not the lockdown-bearing one this wrapper offloads), so
    # running it on the loop is safe.
    mem = _get_memory(state)
    if mem.vector_store or hasattr(state, "_standalone_vector"):
        return _get_vector_store(state)
    # Slow path: at most the first standalone request per process constructs
    # and ``init()``s the store — offload it. All concurrent misses await ONE
    # shared task, giving the serialization the synchronous call sites get for
    # free from the event loop: without it, two concurrent first
    # requests would both miss the cache and both run ``init()``, leaking one
    # of the two sqlite connections. ``asyncio.shield`` keeps the task (and
    # its worker thread) alive when a caller is cancelled — e.g. an aiohttp
    # client disconnect — so a request landing in that window awaits the same
    # init instead of arming a second one. The slot is armed with no await
    # between the read and the write (cannot race on one loop) and cleared on
    # completion: after success the fast path serves from the cache
    # (``_get_vector_store`` publishes it before the task resolves), and after
    # failure the next request retries with a fresh task, so retry semantics
    # stay per-request.
    task = getattr(state, "_standalone_vector_init_task", None)
    if task is None:
        task = asyncio.get_running_loop().create_task(asyncio.to_thread(_get_vector_store, state))
        state._standalone_vector_init_task = task  # type: ignore[attr-defined]
        task.add_done_callback(lambda _t: setattr(state, "_standalone_vector_init_task", None))
    return await asyncio.shield(task)
