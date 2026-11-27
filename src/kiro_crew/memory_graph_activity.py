"""Tell the owner's open memory graph which memories were just read.

A ``memory_activity`` frame on the dashboard socket names the store, the kind of
read, the ids of the memories read and the time, so the Graph tab can light them
as it happens (``MemoryGraphTab``'s spreading activation). Two reads send one:

* ``recall`` -- ``GET /api/memory/recall`` returned these memories (the agent's
  ``memory_recall`` tool, or the dashboard's own test recall);
* ``session_start`` -- a session's first prompt carried these lessons and findings.

The frame carries ids and layout identities only, never memory text, at most :data:`IDS_CAP` of them,
each through the same redaction the graph routes apply to a node's id, so an id
in the frame is the id the graph drew. It goes only to owner-authenticated
sockets (``broadcast_ws_owners``): the graph routes serve a store only to its
owner (``resolve_requested_memory_store``), and the owner may read every store on
this gateway, so no socket learns of a read in a store it could not open. With no
owner socket open nothing is built.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from contextvars import copy_context
from typing import Any, Final

logger = logging.getLogger(__name__)

FRAME: Final = "memory_activity"
RECALL: Final = "recall"
SESSION_START: Final = "session_start"
KINDS: Final[tuple[str, ...]] = (RECALL, SESSION_START)
#: Most ids one frame names; the graph lights at most about 20 a second anyway.
IDS_CAP: Final = 64


def owner_watching(state: Any) -> bool:
    """Whether an owner dashboard socket is open, the only reader of a frame."""
    return any(not ws.closed for ws in list(getattr(state, "_owner_ws_clients", None) or ()))


def activity_frame(
    store: str, kind: str, ids: Iterable[object], *, now: float | None = None
) -> dict[str, Any] | None:
    """The frame for *ids* read from *store*, or ``None`` when it names no id."""
    if kind not in KINDS:
        raise ValueError(f"unknown memory activity kind {kind!r}")
    # Imported here: the graph routes load with the first graph request.
    from kiro_crew.dashboard.handlers.memory_graph_layout_api import _redacted
    from kiro_crew.memory_stores import named_store_or_empty

    keys = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))[:IDS_CAP]
    if not keys:
        return None
    return {
        # The Graph tab names the global store "", as the graph routes resolve it.
        "store": named_store_or_empty(store),
        "kind": kind,
        "ids": [_redacted(i) for i in keys],
        "ts": round(time.time() if now is None else now, 3),
    }


def recalled_ids(payload: object, lessons: Iterable[object] = ()) -> list[str]:
    """The graph ids a recall response delivered: its lessons, facts and experiences."""
    retrieval = payload.get("retrieval") if isinstance(payload, dict) else None
    out = list(lessons)
    if isinstance(retrieval, dict):
        for group, field in (("facts", "key"), ("episodes", "id")):
            rows = retrieval.get(group)
            if isinstance(rows, list):
                out += [row.get(field) for row in rows if isinstance(row, dict)]
    return [i for i in out if isinstance(i, str) and i]


def _located_frame(store: str, kind: str, ids: list[str]) -> dict[str, Any] | None:
    """Read only the existing layout, off the event loop; never start a build."""
    frame = activity_frame(store, kind, ids)
    if frame is None:
        return None
    try:
        from kiro_crew.dashboard.handlers.memory_graph_layout_api import (
            _read_at_one_layout,
            _redacted,
        )
        from kiro_crew.memory_graph_service import graph_for
        from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path

        graph = graph_for(resolve_store_path(store or DEFAULT_MEMORY_STORE))
        frame.update(
            _read_at_one_layout(
                graph,
                lambda: {
                    "clusters": {
                        _redacted(i): point[2] for i, point in graph.sidecar.placed(ids).items()
                    }
                },
            )
        )
    except Exception:  # noqa: BLE001 -- nodes can still light without a usable layout
        logger.debug("memory activity layout not available", exc_info=True)
    return frame


def announce(state: Any, store: str, kind: str, ids: Iterable[object]) -> None:
    """Queue the frame for *ids* without suspending the caller; never raises."""

    def send(done: asyncio.Future) -> None:
        if done.cancelled():
            return
        try:
            frame = done.result()
            if frame is not None and owner_watching(state):
                state.broadcast_ws_owners(FRAME, frame)
        except Exception:  # noqa: BLE001 -- a picture of a read must never fail the read
            logger.debug("memory activity not announced", exc_info=True)

    try:
        if not owner_watching(state):
            return
        keys = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))[:IDS_CAP]
        if keys:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                None, copy_context().run, _located_frame, store, kind, keys
            ).add_done_callback(send)
    except Exception:  # noqa: BLE001 -- a picture of a read must never fail the read
        logger.debug("memory activity not announced", exc_info=True)
