"""Run a consolidation pass's vector-memory writes in the write child, or here.

``memory.consolidation_write_child_process`` (default off) sends the two vector
writes of a pass, ``HistoryConsolidator._write_structured_memory`` and the vector
branch of ``_save_lessons``, to :mod:`kiro_crew.consolidation_write_child`. Each
function here wraps one of them: it answers with the child's result when the child
ran the job, and runs the original method in the gateway when the switch is off,
the store is not one the child can open, or the child could not finish the job.
Either way the pass sees the same outcome: the writes happened, or the same fence
exception (``_LeaseLostMidRun``, ``_PersistenceDisabledMidRun``,
:class:`TranscriptWithheld`, :class:`TranscriptBusy`) the in-process path raises.

The first parameter of each function is the ``HistoryConsolidator`` running the
pass. This module is not one of that class's owners: it reaches the consolidator's
fence types through the ``kiro_crew.history_consolidation`` facade, at call time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.consolidation_write_child import (
    ChildUnavailable,
    facets_to_json,
    get_client,
    write_child_enabled,
)
from kiro_crew.history_derivation import TranscriptBusy, TranscriptWithheld

if TYPE_CHECKING:
    from kiro_crew.history_consolidation import HistoryConsolidator, _InheritGlobal, _RunCommitState
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory_schema import MemoryFacets
    from kiro_crew.vector_memory import VectorMemoryStore

_LOGGER = logging.getLogger("kiro_crew.history")


def _facade() -> Any:
    from kiro_crew import history_consolidation

    return history_consolidation


def _child_job_target(
    consolidator: "HistoryConsolidator", vector_store: object
) -> tuple[str, str] | None:
    """``(sessions_dir, db_path)`` the child should open, or ``None`` to write here.

    Only a real V1 ``VectorMemoryStore`` over a database file qualifies: a private
    member store is written through its own admission path, and anything else (a
    test double, an unopened store) has no file for the child to open.
    """
    if not write_child_enabled():
        return None
    from kiro_crew.vector_memory import VectorMemoryStore

    if not isinstance(vector_store, VectorMemoryStore):
        return None
    if getattr(vector_store, "algorithm_version", "v1") == "v2":
        return None
    db_path = getattr(vector_store, "_db_path", None)
    sessions_dir = getattr(getattr(consolidator, "_log", None), "_dir", None)
    if not isinstance(db_path, Path) or not isinstance(sessions_dir, Path) or not db_path.exists():
        return None
    return str(sessions_dir), str(db_path)


def _serve_embed(vector_store: "VectorMemoryStore") -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Answer the child's embedding requests with the gateway store's own embedder."""
    from kiro_crew.embeddings_settings import PRIORITY_NORMAL

    def serve(frame: dict[str, Any]) -> dict[str, Any]:
        priority = frame.get("priority")
        vector = vector_store._try_embed(
            str(frame.get("text", "")), priority if isinstance(priority, int) else PRIORITY_NORMAL
        )
        reply: dict[str, Any] = {"vector": None, "generation": vector_store.space_generation}
        if vector is not None:
            token = getattr(vector, "space_token", None)
            reply.update(
                vector=list(vector),
                token=list(token) if token else None,
                managed=bool(getattr(vector, "managed", False)),
            )
        return reply

    return serve


def _run_in_child(
    kind: str,
    key: str,
    vector_store: "VectorMemoryStore",
    target: tuple[str, str],
    facets: "MemoryFacets | None",
    commit_state: "_RunCommitState",
    args: dict[str, Any],
) -> bool:
    """Run one job in the child. ``True`` when it ran; ``False`` when the caller must.

    A fence the child hit is raised here as the exception the in-process path raises.
    """
    sessions_dir, db_path = target
    job = {
        "kind": kind,
        "key": key,
        "sessions_dir": sessions_dir,
        "db_path": db_path,
        "lease_token": commit_state.lease_token,
        "committed": commit_state.committed,
        "embedding_dim": getattr(vector_store, "_embedding_dim", None),
        "embed_available": vector_store.embed_fn is not None,
        "space_generation": vector_store.space_generation,
        "facets": facets_to_json(facets),
        "args": args,
    }
    try:
        json.dumps(job)
    except (TypeError, ValueError):
        _LOGGER.warning("Consolidation write job for %s is not plain data; writing here", key)
        return False
    try:
        frame = get_client().run_job(
            job, serve_embed=_serve_embed(vector_store), on_committed=commit_state.mark_committed
        )
    except ChildUnavailable:
        return False
    if frame.get("committed") and not commit_state.committed:
        commit_state.mark_committed()
    if frame.get("ok"):
        return True
    outcome = frame.get("kind")
    message = str(frame.get("message") or "")
    facade = _facade()
    if outcome == "lease_lost":
        raise facade._LeaseLostMidRun(key)
    if outcome == "persistence_disabled":
        raise facade._PersistenceDisabledMidRun
    if outcome == "busy":
        raise TranscriptBusy(message)
    if outcome == "withheld":
        raise TranscriptWithheld(message)
    # The store raised something the child cannot classify. Rerun here so the
    # pass sees the exception the gateway path raises (and charges it as it does).
    _LOGGER.warning(
        "Consolidation write child reported an error for %s (%s); writing here", key, message
    )
    return False


def write_structured_memory(
    consolidator: "HistoryConsolidator",
    result: dict,
    key: str,
    vector_store: "VectorMemoryStore | None | _InheritGlobal",
    *,
    facets: "MemoryFacets | None" = None,
    snapshot: dict | None = None,
    visible_keys: frozenset[str] | None = None,
    messages: list[dict] | None = None,
    commit_state: "_RunCommitState | None" = None,
) -> None:
    """``consolidator._write_structured_memory``, in the child when it can."""
    facade = _facade()
    if isinstance(vector_store, facade._InheritGlobal):
        vector_store = consolidator._vector_store
    store = cast("VectorMemoryStore | None", vector_store)
    state = commit_state or facade._RunCommitState()

    def here() -> None:
        consolidator._write_structured_memory(
            result,
            key,
            store,
            facets=facets,
            snapshot=snapshot,
            visible_keys=visible_keys,
            messages=messages,
            commit_state=state,
        )

    target = _child_job_target(consolidator, store)
    if target is None or store is None:
        return here()
    semantic = result.get("semantic")
    item_keys = (
        {i["key"] for i in semantic if isinstance(i, dict) and isinstance(i.get("key"), str)}
        if isinstance(semantic, list)
        else set()
    )
    # The child needs only what the items name: the previous body of a key it
    # rewrites, and whether a key it deletes was shown to the model. The full
    # table (tens of thousands of rows on a large store) stays here.
    previous = {
        k: {"value_json": snapshot[k].get("value_json")}
        for k in item_keys
        if snapshot and isinstance(snapshot.get(k), dict)
    }
    args = {
        "result": result,
        "snapshot": previous,
        "visible_keys": None if visible_keys is None else sorted(item_keys & visible_keys),
    }
    if not _run_in_child("structured", key, store, target, facets, state, args):
        here()


def save_lessons(
    consolidator: "HistoryConsolidator",
    raw: object,
    vector_store: "VectorMemoryStore | None | _InheritGlobal",
    lesson_store: "LessonStore | None | _InheritGlobal",
    *,
    facets: "MemoryFacets | None" = None,
    key: str = "",
    commit_state: "_RunCommitState | None" = None,
) -> None:
    """``consolidator._save_lessons``, in the child when it can.

    Only the vector-store branch moves; the JSONL ``LessonStore`` branch is a few
    appended lines and stays here.
    """
    facade = _facade()
    if isinstance(vector_store, facade._InheritGlobal):
        vector_store = consolidator._vector_store
    store = cast("VectorMemoryStore | None", vector_store)
    state = commit_state or facade._RunCommitState()

    def here() -> None:
        consolidator._save_lessons(
            raw, store, lesson_store, facets=facets, key=key, commit_state=state
        )

    target = _child_job_target(consolidator, store) if isinstance(raw, list) else None
    if target is None or store is None:
        return here()
    if not _run_in_child("lessons", key, store, target, facets, state, {"raw": raw}):
        here()
