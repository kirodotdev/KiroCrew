"""Scheduling combinators for the dynamic-workflow DSL (GATES A3, A5).

``parallel`` and ``pipeline`` are the two scheduling primitives a workflow script
reaches through ``ctx``. They are pure ``asyncio`` combinators over caller-supplied
thunks / stage callables — they hold no agent, session, or gateway logic, so this
module sits at the BOTTOM of the layering (``dsl`` → ``context`` → ``runner``;
GATE F1) and imports nothing heavy. The runner injects the concurrency ``limit``
(from ``resolve_max_subagents()``); tests pass a small limit or ``None``.

Semantics (frozen — see ``docs/system-specs/modules/workflows.md``):

* ``parallel(tasks)`` — BARRIER. Run every task concurrently, await them all,
  return results in input order. Each task may be a zero-arg thunk
  (``lambda: ctx.agent(p)``) OR an already-created awaitable (``ctx.agent(p)``) —
  both are accepted, since authors naturally reach for the latter. A task that
  raises (synchronously or in its awaitable) resolves to ``None`` — the call
  itself NEVER raises (GATE A5), so callers ``.filter(None)``.

* ``pipeline(items, *stages)`` — NO barrier between stages. Each item flows through
  all stages in its own chain; item B can reach stage 2 while item A is still in
  stage 1 (GATE A3). Wall-clock = slowest single-item chain, not sum-of-slowest-
  per-stage. Each stage is called ``stage(prev, item, index)`` (arity-adapted: a
  1-arg stage just gets ``prev``); for stage 0 ``prev`` is the item itself. A stage
  that raises drops that item to ``None`` and skips its remaining stages. Results
  are returned in input order.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Optional, Sequence

# A thunk: zero-arg callable returning an awaitable (matches Thunk in __init__).
Thunk = Callable[[], Awaitable[Any]]
# A stage: callable of up to (prev, item, index) returning an awaitable.
Stage = Callable[..., Awaitable[Any]]


def _positional_arity(func: Callable[..., Any]) -> Optional[int]:
    """Number of positional params a callable accepts, or None if it takes *args.

    Used to call a stage with only as many of (prev, item, index) as it declares,
    mirroring the JS pipeline's "extra args ignored" behavior so 1-arg and 3-arg
    stages both work.
    """
    try:
        params = inspect.signature(func).parameters.values()
    except (ValueError, TypeError):
        return None  # builtins / C funcs: fall back to passing everything
    count = 0
    for p in params:
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            return None  # *args → accepts all three
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            count += 1
    return count


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable, else return it as-is.

    Stages and thunks may be sync OR async (e.g. ``lambda prev: prev * 10`` or
    ``async def s(prev): ...``); a plain return value must not be ``await``-ed.
    """
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_stage(stage: Stage, prev: Any, item: Any, index: int) -> Any:
    arity = _positional_arity(stage)
    all_args = (prev, item, index)
    args = all_args if arity is None else all_args[: max(0, min(arity, 3))]
    return await _maybe_await(stage(*args))


async def _run_thunk(thunk: Thunk) -> Any:
    """Run one parallel task; any failure becomes ``None`` (A5).

    Accepts BOTH forms callers naturally write:
      * a zero-arg thunk — ``lambda: ctx.agent(p)`` (the documented form), and
      * an already-created awaitable — ``ctx.agent(p)`` (a coroutine), which is
        what most Python authors reach for first (``gather(*coros)`` habit).
    Forgiving here avoids a whole class of authored-script failures where every
    task silently became ``None`` because a coroutine isn't callable.
    """
    try:
        if inspect.isawaitable(thunk):
            # Caller passed ctx.agent(...) directly (a coroutine/awaitable).
            return await thunk
        return await _maybe_await(thunk())
    except Exception:
        return None


async def parallel(thunks: Sequence[Thunk], *, limit: Optional[int] = None) -> list:
    """Run thunks concurrently (barrier), results in input order.

    A failing thunk → ``None``; this never raises (GATE A5). ``limit`` bounds
    concurrency via a semaphore (the runner passes the subagent cap).
    """
    if not thunks:
        return []

    if limit is None or limit <= 0:
        return list(await asyncio.gather(*[_run_thunk(t) for t in thunks]))

    sem = asyncio.Semaphore(limit)

    async def _guarded(t: Thunk) -> Any:
        async with sem:
            return await _run_thunk(t)

    return list(await asyncio.gather(*[_guarded(t) for t in thunks]))


async def pipeline(items: Sequence[Any], *stages: Stage, limit: Optional[int] = None) -> list:
    """Run each item through all stages independently — no inter-stage barrier (A3).

    Each item gets its own chain; chains run concurrently (bounded by ``limit``).
    A stage that raises drops that item to ``None`` and skips its remaining stages.
    Results are returned in input order. With no stages, returns the items as-is.
    """
    if not items:
        return []
    if not stages:
        return list(items)

    sem = asyncio.Semaphore(limit) if (limit and limit > 0) else None

    async def _chain(item: Any, index: int) -> Any:
        prev: Any = item
        for stage in stages:
            try:
                prev = await _call_stage(stage, prev, item, index)
            except Exception:
                return None  # drop this item; skip remaining stages
        return prev

    async def _guarded(item: Any, index: int) -> Any:
        if sem is None:
            return await _chain(item, index)
        async with sem:
            return await _chain(item, index)

    return list(await asyncio.gather(*[_guarded(it, i) for i, it in enumerate(items)]))
