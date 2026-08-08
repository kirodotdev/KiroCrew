"""Dashboard endpoints for session storage: what it costs, and reclaiming it.

Read is open; every mutation is gated on :func:`_is_restricted_session` and
audited through the SEL, because all three of them move or delete a user's
conversation history.

The wire shape deliberately reports a session as ONE size. Sessions occupy two
stores underneath (see :mod:`kiro_crew.session_storage`), but that is an
implementation detail the reader cannot act on, so it is neither split out here
nor derivable from these payloads.

Reclaiming stages files in a trash rather than deleting them, which means the
reclaim itself does not return space to the filesystem. ``trash`` carries the
staged total precisely so a client can say so; a client that reports a reclaim as
freed space is lying to the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _is_restricted_session, _read_session_key
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import transcript_stems
from kiro_crew.session_map import SessionMap
from kiro_crew.session_storage import (
    SessionIndex,
    SessionStorageError,
    empty_trash,
    list_trash,
    measure,
    move_to_trash,
    restore,
    select_reclaimable,
)

logger = logging.getLogger(__name__)

# Why a reclaim is being run. Recorded in the batch manifest and surfaced in the
# trash listing so a user can tell a bulk threshold sweep apart from sessions they
# picked by hand.
REASON_POLICY = "policy"
REASON_MANUAL = "manual"

# A reclaim of six figures of sessions is minutes of filesystem work even at
# rename speed, so every operation here is offloaded off the event loop.
_MAX_SELECTION = 200_000


def _sel():
    # circular import: the handlers package imports this module at load, so the
    # SEL accessor is resolved per call instead of at import time. Late binding
    # also keeps the test suite's patch of the package-level sel() effective.
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _build_index() -> SessionIndex:
    """Pair every mapped session's replay log with its transcript files.

    Stems come from :func:`kiro_crew.history.transcript_stems`, which returns both
    the canonical name and the pre-migration bare ``thread_ts`` name a Slack thread
    may still log under. Using only the canonical stem would leave a legacy
    transcript looking like it belongs to no session — and therefore reclaimable
    while the session is still resumable.
    """
    mapping = SessionMap().mapped_sids_by_key()
    stem_to_sid = {stem: sid for key, sid in mapping.items() for stem in transcript_stems(key)}
    return SessionIndex(stem_to_sid=stem_to_sid, active_sids=frozenset(mapping.values()))


def _deny(operation: str, request: web.Request) -> web.Response:
    _sel().log_api_access(
        caller=_read_session_key(request),
        operation=operation,
        outcome="denied",
        source="dashboard",
        resources="restricted_session_block",
    )
    return web.json_response(
        {
            "error": "Reclaiming session storage is not allowed in this session mode.",
            "code": "restricted_session",
        },
        status=403,
    )


def _bad_request(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


def _refused(exc: SessionStorageError, code: str) -> web.Response:
    return web.json_response({"error": str(exc), "code": code}, status=400)


# Sentinel for "the key was present but is not a list of strings". Distinct from
# ``None``, which means "omitted" and is what widens an operation to every batch or
# every session in one — so a malformed value must never collapse into it.
_MALFORMED: list[str] = []


def _optional_str_list(body: dict[str, Any], key: str) -> list[str] | None:
    """Parse an optional list-of-strings field.

    Returns ``None`` when the key is absent, the list when it is well-formed, and
    :data:`_MALFORMED` (identity-compared) otherwise. Filtering a malformed value
    down to whatever happened to be a string is the dangerous reading: a bare
    string is not a list, so it would silently become "omitted" and widen a
    targeted delete into a total one.
    """
    if key not in body or body[key] is None:
        return None
    value = body[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return _MALFORMED
    return list(value)


async def _json_body(request: web.Request) -> dict[str, Any] | None:
    """Parse a JSON object body; ``None`` when it is absent, empty, or malformed.

    A parse failure must NOT become ``{}``. An empty object is a legitimate request
    on these endpoints, so collapsing malformed input into it would let a truncated
    or non-JSON body read as "no arguments given" — and on this surface "no
    arguments" is what widens an operation.
    """
    raw = await request.read()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _report_payload() -> dict[str, Any]:
    index = _build_index()
    report = measure(index)
    return {
        "total_bytes": report.total_bytes,
        "total_sessions": report.total_sessions,
        "active_sessions": report.active_sessions,
        "active_bytes": report.active_bytes,
        "reclaimable_sessions": report.reclaimable_sessions,
        "reclaimable_bytes": report.reclaimable_bytes,
        # Non-empty when this instance must not reclaim at all; a client should
        # explain rather than offer an action that can only be refused.
        "reclaim_blocked_reason": report.reclaim_blocked_reason,
        "buckets": [
            {"label": b.label, "sessions": b.sessions, "bytes": b.bytes} for b in report.buckets
        ],
        "trash": {
            "bytes": report.trash_bytes,
            # Staged bytes still occupy the filesystem. Named so a client cannot
            # present a reclaim as reclaimed space without contradicting itself.
            "still_on_disk": True,
            "instant": report.trash_same_filesystem,
            "batches": [
                {
                    "batch_id": batch.batch_id,
                    "created_at": batch.created_at,
                    "reason": batch.reason,
                    "sessions": batch.sessions,
                    "bytes": batch.bytes,
                }
                for batch in list_trash()
            ],
        },
    }


async def api_session_storage(request: web.Request) -> web.Response:
    """GET /api/system/session-storage — what sessions cost and what can be reclaimed.

    Uncached: it walks both stores, so it is far too expensive to serve on a poll
    and is meant to be fetched when the screen opens or after an action.
    """
    data = await asyncio.to_thread(_report_payload)
    return web.json_response(data)


async def api_session_storage_cleanup(request: web.Request) -> web.Response:
    """POST /api/system/session-storage/cleanup — stage old sessions for deletion.

    ``dry_run`` returns the same counts without moving anything, so a client can
    show exactly what a threshold will do before the user commits. The selection
    is re-derived here rather than accepted from the client: the numbers a screen
    is showing may be minutes old, and acting on them would move sessions the
    user never saw.
    """
    state: DashboardState = request.app["state"]
    if _is_restricted_session(state, request):
        return _deny("session_storage.cleanup", request)

    body = await _json_body(request)
    if body is None:
        return _bad_request("Request body must be a JSON object.", "invalid_body")
    raw_days = body.get("older_than_days")
    if not isinstance(raw_days, (int, float)) or isinstance(raw_days, bool):
        return web.json_response(
            {"error": "older_than_days must be a number.", "code": "invalid_threshold"},
            status=400,
        )
    try:
        # JSON puts no bound on an integer, so a caller can send hundreds of
        # digits. That is a bad request, not a server error — float() raises
        # OverflowError on it, which would otherwise surface as a 500.
        threshold = float(raw_days)
    except (OverflowError, ValueError):
        return _bad_request("older_than_days is out of range.", "invalid_threshold")
    dry_run = bool(body.get("dry_run"))
    # Reading and possibly migrating session_map.json is filesystem work, so it
    # belongs off the loop like every other operation on this surface.
    index = await asyncio.to_thread(_build_index)

    try:
        selected = await asyncio.to_thread(select_reclaimable, index, threshold)
    except SessionStorageError as exc:
        return _refused(exc, "invalid_threshold")

    # Above the per-batch bound, stage the OLDEST sessions and report the rest as
    # remaining, rather than refusing. A refusal dead-ends the very install this
    # exists for: the measured motivating machine already holds six figures of
    # sessions, and no threshold a client could pick would get under the cap.
    # Oldest-first makes repeating the call monotonic progress.
    selected.sort(key=lambda unit: unit.mtime)
    remaining = max(0, len(selected) - _MAX_SELECTION)
    selected = selected[:_MAX_SELECTION]

    total = sum(unit.bytes for unit in selected)
    if dry_run:
        return web.json_response(
            {"dry_run": True, "sessions": len(selected), "bytes": total, "remaining": remaining}
        )
    if not selected:
        return web.json_response(
            {"sessions": 0, "bytes": 0, "batch_id": "", "remaining": remaining}
        )

    try:
        batch = await asyncio.to_thread(
            move_to_trash,
            [unit.uid for unit in selected],
            reason=REASON_POLICY,
            index=index,
            # Re-read the map inside the lock: the scan above can take long enough
            # for a session to be resumed and mapped in the meantime.
            refresh=_build_index,
        )
    except SessionStorageError as exc:
        return _refused(exc, "cleanup_refused")

    _sel().log_api_access(
        caller=_read_session_key(request),
        operation="session_storage.cleanup",
        outcome="success",
        source="dashboard",
        resources=f"{batch.batch_id}:{batch.sessions}",
    )
    return web.json_response(
        {
            "sessions": batch.sessions,
            "bytes": batch.bytes,
            "batch_id": batch.batch_id,
            "remaining": remaining,
        }
    )


async def api_session_storage_restore(request: web.Request) -> web.Response:
    """POST /api/system/session-storage/restore — undo a staged batch.

    Omitting ``uids`` restores the whole batch, which is the unit a user thinks in
    ("undo what I just did"); naming them restores only those, for the case where
    one conversation turns out to be wanted out of a large sweep.
    """
    state: DashboardState = request.app["state"]
    if _is_restricted_session(state, request):
        return _deny("session_storage.restore", request)

    body = await _json_body(request)
    if body is None:
        return _bad_request("Request body must be a JSON object.", "invalid_body")
    batch_id = body.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        return web.json_response(
            {"error": "batch_id is required.", "code": "invalid_batch"}, status=400
        )
    uids = _optional_str_list(body, "uids")
    if uids is _MALFORMED:
        # Omitted means "the whole batch"; a malformed value must not widen into it.
        return web.json_response(
            {"error": "uids must be a list of strings.", "code": "invalid_batch"},
            status=400,
        )

    try:
        restored = await asyncio.to_thread(restore, batch_id, uids)
    except SessionStorageError as exc:
        return _refused(exc, "restore_refused")

    _sel().log_api_access(
        caller=_read_session_key(request),
        operation="session_storage.restore",
        outcome="success",
        source="dashboard",
        resources=f"{batch_id}:{restored}",
    )
    return web.json_response({"restored": restored})


async def api_session_storage_empty(request: web.Request) -> web.Response:
    """POST /api/system/session-storage/empty — delete staged batches for good.

    The only irreversible operation in this surface, and the only one that returns
    space to the filesystem. Audited with the bytes freed so the record shows what
    was actually destroyed rather than what was requested.
    """
    state: DashboardState = request.app["state"]
    if _is_restricted_session(state, request):
        return _deny("session_storage.empty", request)

    body = await _json_body(request)
    if body is None:
        return _bad_request("Request body must be a JSON object.", "invalid_body")
    batch_ids = _optional_str_list(body, "batch_ids")
    if batch_ids is _MALFORMED:
        return _bad_request("batch_ids must be a list of strings.", "invalid_batch")

    # Emptying takes EXPLICIT intent: either the batches to destroy, or all=true.
    # This endpoint is the only irreversible one, and an "omitted means everything"
    # default put that outcome at the end of every path that produced an empty
    # body — a malformed payload, a wrong-typed field, a client that forgot the
    # argument. Requiring the caller to say which, or to say all, removes the
    # default rather than guarding each way of reaching it.
    empty_all = body.get("all") is True
    if empty_all and batch_ids:
        return _bad_request("Pass batch_ids or all=true, not both.", "invalid_batch")
    if not empty_all and not batch_ids:
        return _bad_request(
            "Specify batch_ids, or all=true to empty the whole trash.",
            "nothing_specified",
        )

    try:
        freed = await asyncio.to_thread(empty_trash, None if empty_all else batch_ids)
    except SessionStorageError as exc:
        return _refused(exc, "empty_refused")

    _sel().log_api_access(
        caller=_read_session_key(request),
        operation="session_storage.empty",
        outcome="success",
        source="dashboard",
        resources=f"freed:{freed}",
    )
    return web.json_response({"freed_bytes": freed})
