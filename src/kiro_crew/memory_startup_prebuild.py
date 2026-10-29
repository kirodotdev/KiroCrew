"""Build the session-start memory reads' query-independent state before a session needs it.

After a restart the first session-start builds each pay the cold half of the memory
reads: the cached semantic scoring set, its term index (stemming every row once) and
the lesson rows' tokens. None of that depends on the message being answered, so the
gateway builds it on a worker thread once its post-bind maintenance has run, and the
first sessions find it warm.

What runs here only reads: no row is written, no embedding is computed and the embed
lock is never taken, so it competes with nothing a user is waiting on but CPU. It
builds exactly the structures the first session-start build would have built anyway
and keeps no extra copy, so a gateway that serves a session holds the same memory
with or without it; only a gateway that never starts a session holds them earlier.
A group the config leaves out of the context is not built, because no session would
read it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Any non-empty text: the lessons render ranks only when it has a query, and the
#: ranking's per-lesson tokens are the cold part. No vector rides with it.
_LESSON_WARM_QUERY = "session start"


def _groups_read(cfg: Any) -> tuple[bool, bool]:
    """Whether a session-start build reads ``(semantic memory, lessons)`` under *cfg*."""
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.context_assembly import inclusion as _inclusion

    groups = ctx._config_scoped_groups(None, cfg)
    return (
        _inclusion._group_included(groups, _inclusion.CONTEXT_GROUP_MEMORY),
        _inclusion._group_included(groups, _inclusion.CONTEXT_GROUP_LESSONS),
    )


def prebuild_startup_reads(
    store: Any, cfg: Any = None, should_stop: Callable[[], bool] = lambda: False
) -> dict[str, float]:
    """Build *store*'s scoring set, term index and lesson tokens; return each step's seconds.

    Blocking, so the caller runs it on a worker thread. *cfg* defaults to the loaded
    config. A step that fails is logged and skipped: the session-start build then pays
    that cost itself, as it did before. *should_stop* is checked between steps, so a
    stopping gateway does not wait for the rest.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.vector_memory_runtime import semantic as _semantic
    from kiro_crew.vector_memory_runtime.embedding import _RecallQuery

    semantic_read, lessons_read = _groups_read(
        cfg if cfg is not None else ctx.KiroCrewConfig.load()
    )
    spent: dict[str, float] = {}

    def step(name: str, fn: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            return fn()
        except Exception:  # noqa: BLE001 -- a warm-up only; the first session pays instead
            logger.warning("memory prebuild: %s failed", name, exc_info=True)
            return None
        finally:
            spent[name] = time.monotonic() - started

    if semantic_read and not should_stop():
        scoring = step("scoring_set", store._semantic_scoring_set)
        if scoring is not None and not should_stop():
            step(
                "term_index",
                lambda: _semantic._scoring_term_index(store, scoring, store._fact_identities()),
            )
    if lessons_read and not should_stop():
        step(
            "lessons",
            lambda: store.get_lessons_context(
                _LESSON_WARM_QUERY, background=True, recall_query=_RecallQuery(None, None, None)
            ),
        )
    return spent
