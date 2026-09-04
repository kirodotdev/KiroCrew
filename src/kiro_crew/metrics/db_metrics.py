"""Per-query timing for KiroCrew's SQLite stores.

``kirocrew.db.query.duration`` — a histogram (ms) of how long individual store
operations take, labelled by which store and which kind of operation.

Why this exists: route latency (``kirocrew.gateway.request.duration``) tells you
*that* an endpoint is slow, and the stack sampler tells you which Python frames
were on CPU, but neither separates "slow because it waited on SQLite" from "slow
because it computed". A search over the memory store and a JSON serialization
pass look identical in a route timing. This closes that gap.

DESIGN NOTES

*Bounded cardinality.* Labels are drawn from two fixed sets and an outcome, so
the series count is a small constant (stores x ops x 2) regardless of what SQL is
executed. Raw SQL text is NEVER used as a label: it is unbounded, and it can
carry user content — a ``LIKE`` pattern or an FTS5 query string is search terms.
Unrecognized values collapse to ``other``/``unknown`` rather than passing through.

*Logical operations, not statements.* The unit measured is a store operation
(one search, one upsert), not each ``execute`` inside it. A single logical search
may run several statements plus row materialization, and the useful question is
how long the operation took, not how the store split it internally.

*Best-effort, like the HTTP middleware.* Timing is recorded in a ``finally`` so
failed operations are measured too, and a telemetry failure never changes the
caller's outcome: exceptions from the recorder are swallowed, and the original
exception propagates unchanged.

*Off by default.* ``get_recorder()`` returns a no-op recorder unless
``telemetry.enabled`` (or ``KIROCREW_TELEMETRY``) is set, so an unopted host pays
one function call and a ``time.monotonic()`` pair per operation.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., object])

DB_METRIC = "kirocrew.db.query.duration"

# Which store the operation ran against. Bounded on purpose -- see module docstring.
STORES = frozenset(
    {
        "memory",  # memory.py: the conversational memory store
        "knowledge",  # knowledge/store.py: the knowledge base
        "vector",  # vector_memory.py: embedding / similarity store
        "history",  # history.py: session transcripts
        "other",
    }
)

# The kind of work, not the statement. "search" covers FTS5/similarity queries,
# which are the ones expected to dominate.
OPS = frozenset(
    {
        "search",
        "read",
        "write",
        "delete",
        "migrate",
        "connect",
        "other",
    }
)

_OK = "ok"
_ERROR = "error"


def store_label(store: str) -> str:
    """Collapse *store* to a member of :data:`STORES`."""
    return store if store in STORES else "other"


def op_label(op: str) -> str:
    """Collapse *op* to a member of :data:`OPS`."""
    return op if op in OPS else "other"


def record_query(store: str, op: str, elapsed_ms: float, *, ok: bool = True) -> None:
    """Record one observation. Never raises."""
    try:
        get_recorder().histogram(
            DB_METRIC,
            elapsed_ms,
            unit="ms",
            attrs={
                "store": store_label(store),
                "op": op_label(op),
                "outcome": _OK if ok else _ERROR,
            },
            description="Duration of a SQLite store operation",
        )
    except Exception:  # noqa: BLE001 - telemetry must never break a store call
        logger.debug("db query timing failed", exc_info=True)


@contextmanager
def timed_query(store: str, op: str) -> Iterator[None]:
    """Time a logical store operation.

    ::

        with timed_query("memory", "search"):
            rows = conn.execute(sql, params).fetchall()

    Records in a ``finally`` so a raising operation is still measured, and tags
    the outcome so a store that is slow only when it fails is visible as such.
    The caller's exception propagates unchanged.
    """
    start = time.monotonic()
    ok = True
    try:
        yield
    except BaseException:
        # Includes CancelledError/KeyboardInterrupt: those are still outcomes
        # worth attributing, and the exception is re-raised untouched.
        ok = False
        raise
    finally:
        record_query(store, op, (time.monotonic() - start) * 1000.0, ok=ok)


def timed(store: str, op: str) -> Callable[[F], F]:
    """Decorator form of :func:`timed_query`, for timing a whole method.

    Preferred when the operation *is* the method body: wrapping an existing body
    in a ``with`` block means re-indenting it, which turns a one-line change into
    a large diff and makes the instrumentation hard to review against the logic
    it surrounds.

    ::

        @timed("vector", "search")
        def search_episodic(self, ...): ...

    Synchronous callables only, which is what the SQLite stores are -- they do
    blocking DB work and are called through ``asyncio.to_thread`` where needed.
    An async def would return a coroutine immediately and the recorded duration
    would be the time to *create* it, near zero, so that case is rejected loudly
    rather than silently mismeasured.
    """

    def decorate(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"timed() cannot wrap the async function {fn.__qualname__}: it would "
                "measure coroutine creation, not execution. Use `async with` around "
                "the awaited work, or time the synchronous inner call."
            )

        @functools.wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> object:
            with timed_query(store, op):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate
