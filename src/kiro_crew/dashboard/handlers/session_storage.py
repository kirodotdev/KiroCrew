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
import time
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _is_restricted_session, _read_session_key
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import transcript_stems
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.session_digest import digest
from kiro_crew.session_map import SessionMap
from kiro_crew.session_storage import (
    MIN_RECLAIM_AGE_DAYS,
    SessionIndex,
    SessionStorageError,
    SessionUnit,
    empty_trash,
    list_trash,
    list_units,
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


def _build_index(state: DashboardState | None = None) -> SessionIndex:
    """Pair every mapped session's replay log with its transcript files.

    Stems come from :func:`kiro_crew.history.transcript_stems`, which returns both
    the canonical name and the pre-migration bare ``thread_ts`` name a Slack thread
    may still log under. Using only the canonical stem would leave a legacy
    transcript looking like it belongs to no session — and therefore reclaimable
    while the session is still resumable.

    *state*, when given, additionally marks the sessions with a turn in flight.
    That set is only ever used to EXPLAIN a refusal, never to grant one: every
    refusal is already decided by ``active_sids``, which is the whole map. Omitting
    the state therefore cannot make anything reclaimable that would not be —
    it only costs the caller the ability to say which sessions are truly busy.
    """
    mapping = SessionMap().mapped_sids_by_key()
    stem_to_sid = {stem: sid for key, sid in mapping.items() for stem in transcript_stems(key)}
    running = state.running_session_keys() if state is not None else frozenset()
    live_sids = frozenset(sid for key, sid in mapping.items() if key in running)
    return SessionIndex(
        stem_to_sid=stem_to_sid,
        active_sids=frozenset(mapping.values()),
        live_sids=live_sids,
    )


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


# ------------------------------------------------------------------ inventory
#
# The list surface. Where the report above answers "how much in total", these
# answer "which sessions, and may I have this one back" — the question a person
# actually acts on.
#
# The split across three endpoints is a cost boundary, not taste. Titles are one
# readline() per transcript and are cheap enough to serve for every row; a first
# message, a turn count and an image count each need the WHOLE file, which at six
# figures of sessions is not servable on open. So those are fetched per row, when
# a row expands.


def _origin(unit: SessionUnit) -> str:
    """A display-ready provenance line, e.g. ``dashboard · chat-70``.

    Composed from the id, so it carries no translatable prose — the parts are
    literal channel and slot names. A unit with no transcript stem is one that
    only exists in the replay store, which is what a subagent looks like on disk;
    it has no channel to name, so its own id is the honest answer.

    The result is scrubbed by the caller before it ships: a session id may be
    only loosely constrained (``_UNIT_ID_RE`` admits the alphanumeric shape of an
    access-key id), and this string IS rendered, so it gets the same treatment as
    any other content on the way out.
    """
    stem = unit.stems[0] if unit.stems else ""
    if not stem:
        return unit.uid
    channel, _, rest = stem.partition("_")
    return f"{channel} · {rest}" if rest else stem


def _redact(text: str) -> str:
    """Scrub a string that came out of a user's transcript.

    Both fields this screen shows — a session's title and its first message —
    are conversation content, so either can carry a pasted key or a credential
    in a URL. Per the ``security-controls`` rule every LLM- or user-originated
    string is passed through both scrubbers, in this order, before it reaches a
    dashboard surface.
    """
    if not text:
        return text
    cleaned, _ = redact_exfiltration_urls(text)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


def _titles_by_stem(conversation_log: Any) -> dict[str, str]:
    """Map transcript stem to its session title.

    ``list_sessions`` reads only each file's first metadata line and caches on
    mtime, so this stays a readline per session rather than a full read. The log
    is taken from dashboard state rather than constructed here precisely so that
    cache is shared. A session that never got a title simply has no entry.
    """
    try:
        rows = conversation_log.list_sessions()
    except Exception:
        logger.debug("session titles unreadable", exc_info=True)
        return {}
    # Only string titles. The resume path assigns a client-supplied `body["title"]`
    # to the slot without validating its type, so a number can reach the persisted
    # metadata — and a number is truthy, so a plain `.get("title")` check would let
    # it through to the scrubbers and turn a read into a 500.
    return {
        row["key"]: row["title"]
        for row in rows
        if row.get("key") and isinstance(row.get("title"), str) and row["title"]
    }


def _inventory_payload(state: DashboardState) -> dict[str, Any]:
    index = _build_index(state)
    units = list_units(index)
    titles = _titles_by_stem(state.conversation_log)
    report = measure(index)

    sessions = []
    # Biggest first: the screen exists to answer "what is taking the space", so the
    # answer should be the first row rather than something to sort for. Sorted on
    # the units, not on the built payload, because the rows are heterogeneous dicts.
    for unit in sorted(units, key=lambda u: u.bytes, reverse=True):
        title = _redact(next((titles[stem] for stem in unit.stems if stem in titles), ""))
        # A session with NO transcript half is one that was never a conversation in
        # the product: a subagent run, which only ever writes a replay log. Those
        # are what the client folds into a single group.
        #
        # Deliberately NOT "absent from the session map": a mapped entry is pruned
        # once a session stops being resumable, so keying on the map would sweep a
        # titled conversation the user still recognises into the anonymous group —
        # and those are exactly the rows worth showing, because being unmapped is
        # also what makes them reclaimable.
        background = not unit.stems
        sessions.append(
            {
                "uid": unit.uid,
                "title": title,
                "origin": _redact(_origin(unit)),
                "bytes": unit.bytes,
                "mtime": unit.mtime,
                # Not advisory: a client must not offer to reclaim one of these,
                # and the module refuses it independently if a client tries.
                "active": unit.active,
                # Why it is refused. `live` is a turn in flight, which is the real
                # hazard; `active and not live` is merely "the product could still
                # resume this", which is a policy choice rather than a danger. Both
                # are refused today; separating them stops the screen telling a user
                # a month-old idle conversation is "in use".
                "live": unit.live,
                "background": background,
            }
        )
    return {
        "total_bytes": report.total_bytes,
        "total_sessions": report.total_sessions,
        "reclaimable_bytes": report.reclaimable_bytes,
        "reclaim_blocked_reason": report.reclaim_blocked_reason,
        "sessions": sessions,
        "trash": {
            "bytes": report.trash_bytes,
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


async def api_session_inventory(request: web.Request) -> web.Response:
    """GET /api/system/session-storage/sessions — one row per session.

    Uncached and scan-bound like the report, so it is fetched when the screen
    opens or after an action, never on a poll.
    """
    state: DashboardState = request.app["state"]
    data = await asyncio.to_thread(_inventory_payload, state)
    return web.json_response(data)


def _detail_payload(uid: str) -> dict[str, Any] | None:
    index = _build_index()
    unit = next((u for u in list_units(index) if u.uid == uid), None)
    if unit is None:
        return None
    d = digest(unit.uid, unit.stems, unit.sid)
    return {
        "uid": unit.uid,
        "first_message": _redact(d.first_message),
        "turns": d.turns,
        "images": d.images,
        "bytes": unit.bytes,
        "mtime": unit.mtime,
    }


async def api_session_inventory_detail(request: web.Request) -> web.Response:
    """GET /api/system/session-storage/sessions/{uid} — one row's detail.

    Reads whole files, so it is deliberately per-row and must never be called in
    a loop over the list. An unreadable or malformed file degrades to empty
    values rather than failing: the row still has a real size to show, and a
    truncated transcript is not a reason to refuse to talk about the session.
    """
    uid = request.match_info.get("uid", "")
    if not uid:
        return _bad_request("uid is required.", "invalid_uid")
    data = await asyncio.to_thread(_detail_payload, uid)
    if data is None:
        return web.json_response({"error": "No such session.", "code": "unknown"}, status=404)
    return web.json_response(data)


def _classify(uids: list[str], index: SessionIndex, now: float) -> tuple[list[str], list[dict]]:
    """Split a client's selection into what may move and what may not, with reasons.

    This exists because :func:`move_to_trash` is all-or-nothing by design: one
    live or too-fresh session in the list and the WHOLE call raises, moving
    nothing. That is the right guarantee for the module — a selection either
    happens or it does not — but it makes a bulk screen useless if a single row
    went live while the user was reading.

    So the eligible ones are separated here and only those are handed over, which
    means the module's refusal never has to fire on a normal request. The
    guarantee is NOT weakened: it still re-reads the session map inside the lock
    and still refuses anything live, so this pre-pass can only ever be more
    conservative than the authority, never less.
    """
    by_uid = {u.uid: u for u in list_units(index)}
    eligible: list[str] = []
    refused: list[dict] = []
    for uid in uids:
        unit = by_uid.get(uid)
        if unit is None:
            refused.append({"uid": uid, "reason": "unknown"})
        elif unit.live:
            # A turn is in flight. The one genuinely hazardous case.
            refused.append({"uid": uid, "reason": "in_use"})
        elif unit.active:
            # Idle, but the product could still resume it. Refused today; calling
            # this "in use" would be a lie the user can disprove by looking at the
            # last-used date.
            refused.append({"uid": uid, "reason": "resumable"})
        elif unit.age_days(now) < MIN_RECLAIM_AGE_DAYS:
            refused.append({"uid": uid, "reason": "too_fresh"})
        else:
            eligible.append(uid)
    return eligible, refused


async def api_session_inventory_trash(request: web.Request) -> web.Response:
    """POST /api/system/session-storage/trash — move a named selection to the trash.

    Unlike ``cleanup``, which derives its own selection from an age threshold,
    this accepts the rows a person ticked. That is safe because the authority did
    not move to the client: :func:`move_to_trash` re-reads the session map inside
    the mutation lock and unions the active sets, so a selection that has gone
    stale can only be refused, never honoured against a live session.

    Sessions the server would not take are reported per uid rather than silently
    dropped — doing less than the user asked without saying so is a defect.
    """
    state: DashboardState = request.app["state"]
    if _is_restricted_session(state, request):
        return _deny("session_storage.trash", request)

    body = await _json_body(request)
    if body is None:
        return _bad_request("Request body must be a JSON object.", "invalid_body")
    uids = _optional_str_list(body, "uids")
    if uids is _MALFORMED:
        return _bad_request("uids must be a list of strings.", "invalid_selection")
    # Omitting the selection must NOT widen to "everything": this endpoint exists
    # to act on named rows, and there is no meaningful default for which.
    if not uids:
        return _bad_request("uids is required.", "nothing_specified")
    if len(uids) > _MAX_SELECTION:
        return _bad_request("Too many sessions in one request.", "selection_too_large")

    # The running-state signal only LABELS a refusal, so passing state here
    # cannot widen what may be taken: active_sids still refuses the whole map.
    index = await asyncio.to_thread(_build_index, state)
    eligible, refused = await asyncio.to_thread(_classify, uids, index, time.time())

    if refused:
        # A refusal is a security-relevant outcome, not a quiet detail of a 200.
        # Someone asked to remove specific conversations and was told no; audited
        # here so the record shows the attempt and which sessions were protected.
        # Emitted for a PARTIAL refusal too, otherwise a request that took nine of
        # ten sessions would leave the tenth's protection unrecorded.
        _sel().log_api_access(
            caller=_read_session_key(request),
            operation="session_storage.trash",
            outcome="denied",
            source="dashboard",
            resources=",".join(f"{r['uid']}:{r['reason']}" for r in refused)[:512],
        )
    if not eligible:
        return web.json_response({"sessions": 0, "bytes": 0, "batch_id": "", "refused": refused})

    try:
        batch = await asyncio.to_thread(
            move_to_trash,
            eligible,
            reason=REASON_MANUAL,
            index=index,
            refresh=_build_index,
        )
    except SessionStorageError as exc:
        return _refused(exc, "trash_refused")

    _sel().log_api_access(
        caller=_read_session_key(request),
        operation="session_storage.trash",
        outcome="success",
        source="dashboard",
        resources=f"{batch.batch_id}:{batch.sessions}",
    )
    return web.json_response(
        {
            "sessions": batch.sessions,
            "bytes": batch.bytes,
            "batch_id": batch.batch_id,
            "refused": refused,
        }
    )
