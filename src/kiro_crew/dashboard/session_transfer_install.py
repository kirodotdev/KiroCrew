"""Installing an arrived bundle as a new slot: ``POST /api/chat/slots/import``.

Validating the bundle, redacting the receive-side rows, publishing the history prefix,
resolving the agent hint, and :func:`_install_arrived_bundle`, which materialises the
slot, persists its transcript, joins its Layer B and files it under its arrival folder.

Owner of :mod:`kiro_crew.dashboard.session_transfer`, which is its only import path:
callers and tests reach every name here as ``session_transfer.<name>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.agent_discovery import list_agents
from kiro_crew.atomic_write import fsync_dir, replace_with_retry
from kiro_crew.dashboard.arrival_folders import (
    arrival_folder_exists,
    arrival_folder_id,
    discard_arrival_folders,
    mark_arrival_folder_shared,
)
from kiro_crew.dashboard.chat_persistence import (
    _build_message_entry_uncached,
    save_slot_off_loop,
    session_transcript_remains,
    session_was_deleted,
)
from kiro_crew.dashboard.chat_utils import (
    _sync_dashboard_slots,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.session_transfer_intake import _read_bundle_body, _reject
from kiro_crew.dashboard.session_transfer_layers import (
    _forget_layer_b_join,
    _join_layer_b,
    _resolve_layer_b_sid,
    _unlink_layer_b_files,
    _write_layer_b_files,
)
from kiro_crew.dashboard.session_transfer_staging import (
    _IMPORT_TITLE_MARKER,
    _MAX_TITLE_CHARS,
    _SUPPORTED_BUNDLE_VERSIONS,
    _VISIBLE_ROLES,
)
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, DashboardState
from kiro_crew.dashboard.token_auth import effective_request_app
from kiro_crew.history import mint_row_mid, monotonic_transcript_ts
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

# Layering: chat_handlers must stay importable before this module finishes
# loading, so a MODULE-LEVEL import of chat_handlers here risks an import cycle:
# whichever of the two loads first hits the other while it is still partially
# initialised.
# The two symbols this module needs (``_materialise_slot_from_history`` and
# ``_redact_history_rows``) are used only inside ``api_chat_slot_import``, so they
# are imported FUNCTION-LOCALLY at the top of that handler instead. Keep it that
# way: a module-level import reinstates the cycle. The proper long-term fix is to
# move the shared collaborators down to chat_persistence, per the note there.

logger = logging.getLogger("kiro_crew.dashboard.session_transfer")


#: How many of an import's newest rows are hydrated into the live slot. The rest
#: are written straight to the transcript as its frozen prefix, the same shape a
#: session opened from History has, so the slot's in-memory cap never trims an
#: imported row and hydration costs the same whatever the session's length.
_IMPORT_WINDOW = 500


def _build_redacted_rows(messages: list[dict[str, Any]]) -> list[dict]:
    """The receive-side rows for ``messages``, content-redacted, each carrying the
    ``meta.mid`` and ``ts`` it is persisted and hydrated with. **Runs in a
    thread**: the build and the redaction both scale with the message count.

    Minted here, once, because the prefix rows go to disk and the window rows go
    to the slot: a row minted in two places would reach the transcript with one
    id and the window with another, and the save would keep both copies.
    """
    # Function-local for the same import-cycle reason as in the handler.
    from kiro_crew.dashboard.chat_handlers import _redact_history_rows

    rows = [{"role": m["role"], "content": m["content"], "ts": m["ts"]} for m in messages]
    rows = _redact_history_rows(rows)
    now = datetime.now(timezone.utc)
    previous: str | None = None
    for row in rows:
        if not row.get("ts"):
            row["ts"] = monotonic_transcript_ts(previous, now)
        previous = row["ts"]
        row["meta"] = {"mid": mint_row_mid()}
    return rows


def _prefix_row(row: dict) -> dict:
    """A prefix row in the shape :meth:`_ChatSlot.append` gives a window row."""
    role = row.get("role", "assistant")
    return {
        "role": role,
        "content": row.get("content", ""),
        "cls": "msg msg-u" if role == "user" else "msg msg-a",
        "ts": row.get("ts", ""),
        "meta": row["meta"],
    }


class _PrefixPublication:
    """Decides whether an import's prefix file survives a cancelled import.

    The prefix is written in a worker thread, and a cancelled import cannot
    join that thread: its rollback runs synchronously on the event loop, and
    the thread goes on running. Two flags settle it instead. The worker sets
    :attr:`published` after its rename and then reads :attr:`abandoned`; the
    rollback sets :attr:`abandoned` and then reads :attr:`published`. Each
    set-then-read is one step under :attr:`lock`, so whichever side comes
    second sees the other's flag and removes the file -- at least one does,
    and both is harmless. The lock guards two booleans and nothing else: no
    rename, retry sleep or unlink ever runs under it, so the loop never waits
    on the worker's IO.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.abandoned = False
        self.published = False

    def mark_published(self) -> bool:
        """Record the rename; returns whether the import was abandoned first."""
        with self.lock:
            self.published = True
            return self.abandoned

    def mark_abandoned(self) -> bool:
        """Record the cancellation; returns whether the rename already landed."""
        with self.lock:
            self.abandoned = True
            return self.published

    def is_abandoned(self) -> bool:
        with self.lock:
            return self.abandoned


def _remove_after_save(path: Path | None, future: asyncio.Future) -> None:
    """Done-callback for a cancelled import's in-flight save: once the worker
    has finished writing, remove the transcript it wrote. The callback reads
    the future's outcome so a failed save is not reported as never retrieved.
    Blocking only for an unlink, the same cost the cancellation arm already
    pays for the Layer B files."""
    if not future.cancelled():
        future.exception()
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _write_import_prefix(
    path: Path,
    created_at: str,
    rows: list[dict],
    publication: _PrefixPublication | None = None,
) -> None:
    """Write the transcript for an import whose older ``rows`` stay on disk.
    **Blocking IO.**

    The file is a metadata line and the prefix rows, built by the same entry
    builder the save uses. The save that follows reads those lines back verbatim
    as the frozen prefix and appends the window, so every row lands exactly once.

    Rows are written one at a time into a staged file beside ``path`` and the
    file is renamed into place, so at most one serialised row is in memory on
    top of ``rows`` -- a joined copy of the whole prefix would exceed the
    memory admission's reservation on exactly the large imports it admits.
    """
    attachments = (path.parent, path.stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps({"_type": "metadata", "created_at": created_at}) + "\n")
            for row in rows:
                entry = _build_message_entry_uncached(_prefix_row(row), attachments=attachments)
                if entry is not None:
                    out.write(json.dumps(entry) + "\n")
            out.flush()
            os.fsync(out.fileno())
        if publication is not None and publication.is_abandoned():
            tmp.unlink(missing_ok=True)
            return
        replace_with_retry(tmp, path)
        if publication is not None and publication.mark_published():
            # Cancelled while the rename ran: the rollback may have looked
            # before the rename landed, so the file is removed here.
            path.unlink(missing_ok=True)
            return
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent, best_effort=True)


def _validate_bundle(body: Any) -> tuple[dict[str, Any], web.Response | None]:
    """Validate an inbound bundle STRUCTURALLY. Returns ``(bundle, error_response)``.

    Checks shape and types only — version, that ``messages`` is a non-empty array
    of ``{role, content}`` objects with visible roles, and the field types of
    ``title`` / ``origin`` / ``agent`` / ``layer_b``. It imposes NO size ceiling:
    a large session is copied, not refused, and memory is bounded upstream by the
    stream-to-disk in :func:`_read_bundle_body` plus the arrival permit and the
    memory admission. ``title`` is TRUNCATED, never rejected — a label losing its
    tail costs a reader nothing.
    """
    if not isinstance(body, dict):
        return {}, _reject("body must be a JSON object", "transfer_body_not_object")

    version = body.get("bundle_version")
    # Reject an unknown version outright instead of best-effort parsing: see
    # BUNDLE_VERSION.
    if version not in _SUPPORTED_BUNDLE_VERSIONS:
        return {}, _reject(
            f"unsupported bundle_version {version!r} "
            f"(this instance speaks {list(_SUPPORTED_BUNDLE_VERSIONS)})",
            "transfer_version_unsupported",
        )

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        return {}, _reject("messages must be an array", "transfer_messages_not_array")
    if not raw_messages:
        return {}, _reject("bundle carries no messages", "transfer_bundle_empty")

    messages: list[dict[str, Any]] = []
    for i, m in enumerate(raw_messages):
        if not isinstance(m, dict):
            return {}, _reject(f"message {i} is not an object", "transfer_message_not_object")
        role = m.get("role")
        if role not in _VISIBLE_ROLES:
            return {}, _reject(
                f"message {i} has role {role!r}; expected one of {list(_VISIBLE_ROLES)}",
                "transfer_message_bad_role",
            )
        content = m.get("content", "")
        if not isinstance(content, str):
            return {}, _reject(
                f"message {i} content must be a string", "transfer_message_bad_content"
            )
        ts = m.get("ts", "")
        messages.append({"role": role, "content": content, "ts": ts if isinstance(ts, str) else ""})

    title = body.get("title", "")
    if not isinstance(title, str):
        return {}, _reject("title must be a string", "transfer_bad_title")
    origin = body.get("origin", "")
    if not isinstance(origin, str):
        return {}, _reject("origin must be a string", "transfer_bad_origin")
    agent = body.get("agent", "")
    if not isinstance(agent, str):
        return {}, _reject("agent must be a string", "transfer_bad_agent")

    validated: dict[str, Any] = {
        # The sender's explicit "I had context but withheld it" signal, coerced
        # because it arrives from an untrusted peer. Carried through because
        # validation normalises the body, so anything dropped here is invisible
        # downstream -- and this is what tells a degraded import apart from a
        # session that simply never had a kiro-cli context.
        "layer_b_skipped": bool(body.get("layer_b_skipped")),
        "title": title[:_MAX_TITLE_CHARS],
        "origin": origin[:_MAX_TITLE_CHARS],
        "agent": agent,
        "messages": messages,
    }

    # Layer B is optional: absent on a v1 bundle, or on a session that never had
    # a kiro-cli context. When present it must be well-formed — the same
    # untrusted-input stance as messages — but it is not size-capped here: the
    # body was already streamed to disk under the free-space headroom and admitted
    # against available memory before it reached this validator.
    layer_b = body.get("layer_b")
    if layer_b is not None:
        if not isinstance(layer_b, dict):
            return {}, _reject("layer_b must be an object", "transfer_layer_b_not_object")
        env = layer_b.get("envelope")
        events = layer_b.get("events")
        if not isinstance(env, dict):
            return {}, _reject(
                "layer_b.envelope must be an object", "transfer_layer_b_bad_envelope"
            )
        if not isinstance(events, str):
            return {}, _reject("layer_b.events must be a string", "transfer_layer_b_bad_events")
        validated["layer_b"] = {"envelope": env, "events": events}

    return validated, None


def _resolve_agent(name: str) -> str:
    """Return *name* if this instance has an agent by that name, else ``""``.

    An agent template is a local object; carrying a name the target does not
    have would leave the slot pointing at nothing. Resolution failure is not an
    error — the session imports onto the default agent.

    **Blocking**: ``list_agents`` scans the agents directory and parses each
    manifest, so callers on the event loop must offload it (see the call site in
    :func:`api_chat_slot_import`).
    """
    if not name:
        return ""
    try:
        if any(getattr(a, "name", "") == name for a in list_agents()):
            return name
    except Exception:
        # Discovery is best-effort: a broken agents dir must not fail an import.
        logger.debug("session_transfer: agent discovery failed", exc_info=True)
    return ""


async def api_chat_slot_import(request: web.Request) -> web.Response:
    """POST /api/chat/slots/import — materialise a transferred session bundle.

    Always creates a NEW slot (copy semantics, see the module docstring). The
    imported slot deliberately has no project directory: the user picks one on
    arrival.

    The SINGLE server route behind both arrival routes — a session pushed over
    the tunnel by a peer's ``send_session_bundle``, and a session installed from
    an exported file — so everything that must hold for "a session arrived here"
    belongs in this function and nowhere else. Two such rules live here: the body
    is accepted gzipped or plain (``_read_bundle_body``), and the session is filed
    under ``Imported`` / ``from <sender>`` (``arrival_folders``). Both are written
    once, for both routes, on purpose: the transport a bundle arrived by must not
    decide either how its bytes are read or where the session lands.

    Owns the stack that holds a decompressed bundle's expansion permit. The
    permit has to outlive the READ — a gzip body is still resident, in parsed
    form, through redaction and persistence — so it cannot be released inside
    ``_read_bundle_body``, and the arrival is a separate function purely so the
    permit's span is the whole arrival without re-indenting it under a block.
    """
    async with contextlib.AsyncExitStack() as keep:
        # Bound to a name rather than returned from inside the block so the
        # function has one definite exit: an AsyncExitStack's ``__aexit__`` is
        # typed as possibly SUPPRESSING, which makes a return inside the block a
        # path that can fall through it. The permit still spans the arrival —
        # the stack closes here, after the arrival has produced its response.
        response = await _install_arrived_bundle(request, keep)
    return response


async def _install_arrived_bundle(
    request: web.Request, keep: contextlib.AsyncExitStack
) -> web.Response:
    """Materialise one arrived bundle. See :func:`api_chat_slot_import`.

    *keep* holds resources that must live until the arrival is finished rather
    than until the body has been read — today that is the expansion permit.
    """
    # Imported function-locally, not at module level: chat_handlers' import graph
    # reaches back here (see the layering note at the top of this module), so a
    # module-level import would close an import cycle.
    from kiro_crew.dashboard.chat_handlers import _materialise_slot_from_history

    state: DashboardState = request.app["state"]
    request_app = request.get("app", "")
    caller = request_app or "dashboard"
    # The AUTHORIZATION identity, resolved by the shared rule rather than read
    # off the request the way ``request_app`` above is. The two differ for a
    # caller that carries no app claim but does carry a session key an app owns:
    # ``request.get("app")`` is empty there and the shared rule derives the app.
    # Filing must see the derived value, because that is the caller an
    # app-scoped arrival has to be refused a folder for. Kept SEPARATE from
    # ``request_app`` on purpose — that value is the slot's own app attribution
    # and its meaning is not this one's.
    folder_app = effective_request_app(state, request)

    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="rate_limit",
            resources=f"slot_count={state.live_slot_count()}",
            error="slot cap reached",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({MAX_LIVE_SLOTS})",
                "code": "transfer_slot_cap",
            },
            status=429,
        )

    body, body_err = await _read_bundle_body(request, keep)
    if body_err is not None:
        return body_err

    # Validation walks every message; for a large bundle that is seconds of
    # GIL-held work, so it runs in a thread instead of stalling the loop.
    bundle, err = await asyncio.to_thread(_validate_bundle, body)
    # The validated bundle is all that is read from here on; the raw document
    # would otherwise stay resident beside it for the rest of the arrival.
    del body
    if err is not None:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="dashboard",
            resources="bundle validation",
            error="bundle rejected",
        )
        return err

    messages = bundle["messages"]
    # Agent resolution scans the agents directory and parses each manifest, so it
    # cannot run on the event loop. Only pay the thread hop when a hint was
    # actually sent — the common case is an empty hint, which resolves to "" with
    # no IO at all.
    agent_hint = bundle["agent"]
    resolved_agent = await asyncio.to_thread(_resolve_agent, agent_hint) if agent_hint else ""

    # Title/origin are redacted here because they feed the marked title and the
    # audit line; the shared materialiser re-redacts the composed title via
    # ``_rehydrate_slot_title`` (idempotent). Per-MESSAGE redaction is NOT done
    # here: the receive-side rows are content-redacted just below, in one
    # off-loop ``_redact_history_rows`` pass before construction, so a second
    # pass would double the regex cost over the whole of a large peer transcript
    # for no persisted difference. User turns stay verbatim there,
    # matching fork.
    source_title = bundle["title"] or "Untitled"
    source_title, _ = redact_exfiltration_urls(source_title)
    source_title, _ = redact_credentials(source_title)
    origin = bundle["origin"]
    origin, _ = redact_exfiltration_urls(origin)
    origin, _ = redact_credentials(origin)
    suffix = f" (from {origin})" if origin else ""

    # Normalise the bundle turns to the row shape the materialiser hydrates from.
    # Build the receive-side rows (dict construction, no GIL-held regex), then
    # content-redact them OFF THE LOOP before construction. Redaction over a large
    # transcript is heavy GIL-held regex, so it runs in a
    # thread where it yields freely and — critically — BEFORE any slot exists, so
    # a stall here is only a stall, not a window on a half-built slot. The
    # materialiser is then synchronous and does no content redaction. This is the
    # importer's own egress-mirroring scrub (defense-in-depth; it must not assume
    # the sender scrubbed).
    rows = await asyncio.to_thread(_build_redacted_rows, messages)

    # The marked title travels as the persisted title so the shared path restores
    # it; ``origin`` is NOT set on the metadata snapshot -- that key is the
    # sending instance's label, not a slot origin tag, and import deliberately
    # lands untagged (default origin), exactly as before. No folder_id / pinned /
    # tags travel: the bundle's own folder_id is a reference into the SENDER's
    # tree and is dropped, and ``folder_id`` is deliberately absent HERE so
    # placement is resolved after the slot exists (see the filing call below) --
    # a folder created in front of the post-await slot-cap re-check is left
    # behind when that check answers 429.
    meta = {
        "title": f"{_IMPORT_TITLE_MARKER}{source_title}{suffix}",
        "agent": resolved_agent,
    }

    # Re-check the cap HERE, with no await between this test and the creation
    # inside the shared materialiser below. The check at the top of the handler
    # is necessary but not sufficient: body parsing and agent resolution await,
    # so N concurrent imports near the cap all clear that first test before any
    # of them allocates, and all N are admitted.
    # This second test closes that window because the loop cannot switch tasks
    # between it and the ``get_or_create_slot`` inside ``_materialise_slot_from_history``.
    #
    # Distinct from the construction accounting the materialiser opens: that keeps
    # a RETRACTED slot counted, which is a different window (after creation). Both
    # are needed.
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="denied",
            source="rate_limit",
            resources=f"slot_count={state.live_slot_count()}",
            error="slot cap reached (post-await recheck)",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({MAX_LIVE_SLOTS})",
                "code": "transfer_slot_cap",
            },
            status=429,
        )

    # The shared materialiser mints the key (name=None), registers the slot and
    # holds it under ``begin_slot_construction`` for the duration of hydration,
    # then hands it back registered and counted. ``serialize_slots`` omits an
    # under-construction slot, so Layer B, the durable save and the publish all
    # run below while the slot is hidden from clients (though registered, so a
    # concurrent same-key lookup still resolves it) -- it is shown only when this
    # handler ends construction and pushes at its tail.
    slot = _materialise_slot_from_history(
        state,
        name=None,
        history_key="",
        meta=meta,
        all_messages=rows,
        app=request_app,
        # The newest rows are the live window; the older ones are written to the
        # transcript as its frozen prefix just before the save below, so
        # _disk_older_count counts exactly the rows that write puts on disk.
        window_limit=_IMPORT_WINDOW,
        # Import synthesised its metadata; it read no transcript off disk, so the
        # delete-won disk-identity guard must stay dormant.
        disk_meta_observed=False,
        # Silent replay of a bundle onto a slot that stays registered and under
        # construction throughout its synchronous hydration (it is retracted from
        # ``_slots`` only afterwards, for the async tail below): broadcasting each
        # row would push an under-construction slot's peer content to every client
        # and retire live question cards.
        broadcast_rows=False,
        # Rows arrive with the id _build_redacted_rows minted; minting stays on
        # for any row that somehow lacks one, so none lands id-less.
        mint_missing_mids=True,
    )
    sm_key = effective_session_key(slot)
    sessions = getattr(state, "sessions", None)
    # Retract the slot from ``_slots`` for the async finalization tail below.
    # The materialiser's hydrate loop is synchronous, but Layer B write/join and
    # the durable save that follow AWAIT while the slot would otherwise be
    # registered -- and the raw ``state._slots.get(name)`` acquirers (delete/close,
    # regenerate, rewind) bypass the ``get_slot``/``get_or_create_slot`` guards, so
    # a crafted request against the minted key could close, resurrect or truncate
    # the slot mid-finalization. Popping it closes EVERY such acquirer at once:
    # the slot is not in ``_slots`` to be found. This is safe here where it was
    # NOT for resume: import MINTS its key from the monotonic counter and does not
    # return it until this handler responds, so no concurrent request can target
    # it -- there is no second caller to mint a duplicate the way a client-supplied
    # resume key allowed. The slot stays under ``begin_slot_construction`` (the
    # count is released in the finally) and is re-registered on the success path
    # below, after finalization lands. Every error path already pops it (a no-op
    # now) and rolls back the join.
    state._slots.pop(slot.key, None)

    # Resume mode, reported back to the sender so a degraded copy is never shown
    # as a full one. "prefix" is correct for a v1/no-Layer-B bundle: the session
    # opens on the transcript, which is exactly what was sent.
    resume_mode = "prefix"
    layer_b_sid = ""
    # Beside ``layer_b_sid`` and for the same reason: the except arms below read
    # it, and a failure BEFORE the filing call must find an empty tuple rather
    # than an unbound name.
    created_folders: tuple[str, ...] = ()
    # The same rows plus the record the resolver WROTE for each, which is what
    # lets the rollback tell a row still holding what the import created from one
    # a person has since renamed, recoloured or moved. Empty deletes nothing.
    created_rows: tuple[tuple[str, str, str], ...] = ()
    # Read by the cancellation arm, which cannot join the prefix writer's
    # thread: it abandons the publication instead (see ``_PrefixPublication``).
    prefix_path: Path | None = None
    prefix_publication = _PrefixPublication()
    # The durable save, run as its own future so a cancelled import can let it
    # finish and then remove what it wrote (see the cancellation arm).
    save_future: asyncio.Future | None = None
    transcript_path: Path | None = None
    # Adopted rows the filing found HIDDEN. The un-hide is deferred to the
    # landed path because no rollback can put the flag back on an adopted row.
    hidden_rows: tuple[str, ...] = ()
    layer_b = bundle.get("layer_b")

    try:
        if layer_b:
            # Files in a thread (blocking IO), join on the loop (the live map's
            # whole-file write is unsynchronised against concurrent session
            # starts). See _write_layer_b_files / _join_layer_b.
            written_sid = await asyncio.to_thread(_write_layer_b_files, layer_b, slot.agent)
            # On disk now; the text is the largest thing an arrival holds and
            # nothing below reads it.
            layer_b = bundle["layer_b"] = {"envelope": layer_b.get("envelope")}
            layer_b_sid = written_sid or ""
            resumable = bool(layer_b_sid) and _join_layer_b(sessions, sm_key, layer_b_sid)
            if not resumable:
                logger.info(
                    "session_transfer: imported %s without Layer B; "
                    "it will resume via the transcript prefix",
                    slot.key,
                )
                if layer_b_sid:
                    await asyncio.to_thread(_unlink_layer_b_files, layer_b_sid)
                    layer_b_sid = ""
            resume_mode = "session_load" if resumable else "prefix"

        # Mark the IMPORTED TAB when it arrived without resumable context. The
        # sender's row is gone the moment its menu closes; the tab title is the
        # one surface that persists and is present where the loss will be felt.
        # Fires when the sender either SENT context that failed to land or told
        # us it deliberately withheld context (``layer_b_skipped``, a mid-turn
        # source); never for a bundle that simply never had a kiro-cli context.
        if resume_mode == "prefix" and (bundle.get("layer_b") or bundle.get("layer_b_skipped")):
            slot.title = f"{slot.title} — transcript only"

        # ARRIVAL PROVENANCE FILING (docs/request-for-change/rfc-arrival-provenance-filing.md).
        # Here rather than in ``meta`` above for two reasons, both load-bearing.
        #
        # The slot already exists, so the post-await slot-cap re-check above
        # cannot answer 429 from here on -- a folder write standing in front of
        # that check is left behind when it fires, which is folder-store
        # exhaustion with a narrower trigger than the loop an app token would
        # otherwise run.
        #
        # And it is the LAST await before the durable save, so the window in which
        # a delete can invalidate the placement is as short as this handler can
        # make it. The re-check below closes what is left of that window, and the
        # repair after re-registration closes the rest: for this whole stretch the
        # slot is retracted from ``state._slots``, which is the mapping the folder
        # delete handler's unfile sweep iterates, so a delete landing here cannot
        # see the session to unfile it.
        #
        # Best-effort by contract: ``arrival_folder_id`` answers "" for every
        # refusal (app-scoped caller, ceiling reached, store write failure) and
        # the session then lands unfiled, exactly as it did before this shipped.
        # The transcript is the payload; the grouping is convenience.
        filing = await arrival_folder_id(state, origin=origin, request_app=folder_app)
        arrival_folder = filing.folder_id
        # Rows this filing CREATED, for the failure paths below. Held in the
        # handler's own scope rather than re-derived: after a failure the store no
        # longer says which rows were new, and an adopted row must survive.
        created_folders = filing.created_ids
        created_rows = filing.created_rows
        hidden_rows = filing.hidden_ids
        if arrival_folder and await arrival_folder_exists(state, arrival_folder):
            slot.folder_id = arrival_folder

        # best_effort=False: a swallowed write failure would answer 200 while the
        # imported session exists only in memory, so the peer believes the
        # transfer landed and a restart before the next flush loses it. An import
        # that cannot be persisted must fail loudly instead.
        try:
            if slot._disk_older_count and state.conversation_log is not None:
                prefix_path = state.conversation_log._path(slot_history_key(slot))
                await asyncio.to_thread(
                    _write_import_prefix,
                    prefix_path,
                    slot.created_at,
                    rows[: slot._disk_older_count],
                    prefix_publication,
                )
            # The prefix is on disk; the window is on the slot. Nothing below
            # reads the full row list.
            del rows[:]
            if state.conversation_log is not None:
                transcript_path = state.conversation_log._path(slot_history_key(slot))
            save_future = asyncio.ensure_future(save_slot_off_loop(state, slot, best_effort=False))
            # Shielded: the save's worker thread cannot be stopped, so a
            # cancellation must not detach this task from it.
            await asyncio.shield(save_future)
        except Exception:
            if prefix_path is not None:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(prefix_path.unlink, missing_ok=True)
            # Retryable, peer at no fault: coded answer, source untouched, resend
            # is safe. Drop the registered-but-hidden slot and release its
            # construction count in the finally; it was never shown (the
            # construction filter hid it), so no broadcast is owed. Undo the join
            # written above.
            state._slots.pop(slot.key, None)
            sid = _forget_layer_b_join(sessions, sm_key) or layer_b_sid
            if sid:
                await asyncio.to_thread(_unlink_layer_b_files, sid)
            # The folder was committed before this save, so without this the
            # failed import leaves an empty row behind. Placed after the pop, so
            # the importing slot is already out of the mapping the rollback reads
            # and does not count as a session filed into the row.
            await discard_arrival_folders(state, created_rows)
            logger.warning(
                "session_transfer: could not persist imported slot=%s; refusing the import",
                slot.key,
                exc_info=True,
            )
            sel().log_api_access(
                caller=caller,
                operation="chat.slot_import",
                outcome="error",
                source="dashboard",
                resources=f"to={slot.key}",
                error="durable save failed",
            )
            return web.json_response(
                {
                    "error": "could not persist the imported session; please retry",
                    "code": "transfer_import_save_failed",
                },
                status=503,
            )
    except asyncio.CancelledError:
        # CancelledError is a BaseException, so ``except Exception`` never sees
        # it: a shutdown or client disconnect after the join left an orphaned
        # map entry plus its files. Roll back synchronously (awaiting inside a
        # cancelled task is not dependable), then re-raise so cancellation
        # propagates.
        state._slots.pop(slot.key, None)
        try:
            sid = _forget_layer_b_join(sessions, sm_key)
            if sid:
                _unlink_layer_b_files(sid)
        except Exception:
            logger.debug("session_transfer: cancellation rollback failed", exc_info=True)
        # The prefix writer's thread outlives this task, so its rename could
        # otherwise land after the rollback and leave History a session missing
        # its newest rows. Abandoning stops a pending rename or removes a done one.
        published = prefix_path is not None and prefix_publication.mark_abandoned()
        if save_future is not None and not save_future.done():
            # The save's worker is still writing the transcript, and it reads
            # the prefix back as it goes: removing either now would leave it to
            # publish a truncated file. Remove the transcript once it is done.
            save_future.add_done_callback(
                functools.partial(_remove_after_save, transcript_path or prefix_path)
            )
        elif save_future is not None and transcript_path is not None:
            with contextlib.suppress(OSError):
                transcript_path.unlink(missing_ok=True)
        elif published and prefix_path is not None:
            with contextlib.suppress(OSError):
                prefix_path.unlink(missing_ok=True)
        # No folder rollback here, deliberately. ``discard_arrival_folders`` is
        # async because the folder store's lock is, and this arm is synchronous
        # for the reason stated above. Scheduling it as a task would be
        # dependable only for a disconnect and not for a shutdown, so it would
        # trade a plain gap for one that looks closed. A cancellation mid-import
        # can therefore still leave an empty row, which the row's own visibility
        # makes recoverable by hand.
        raise
    except Exception:
        # The slot was hidden by the construction filter throughout, so nothing
        # is visible to retract -- but the join and its files may exist. Undo
        # them; the finally releases the construction count and the pop drops the
        # hidden slot.
        state._slots.pop(slot.key, None)
        try:
            sid = _forget_layer_b_join(sessions, sm_key)
            if sid:
                await asyncio.to_thread(_unlink_layer_b_files, sid)
        except Exception:
            logger.debug("session_transfer: join rollback failed", exc_info=True)
        # Same reason as the durable-save arm: a folder committed before the
        # failure is this import's to unwind. Outside the try above so a join
        # rollback failure cannot skip it.
        await discard_arrival_folders(state, created_rows)
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="error",
            source="dashboard",
            resources=f"to={slot.key}",
            error="import finalisation failed",
        )
        raise
    finally:
        # Every exit releases the construction count the shared materialiser
        # opened: the 503 return above, the raises here, and the success path.
        # Runs before the re-registration below, but nothing between them awaits,
        # so no coroutine can observe the slot as neither published nor counted.
        state.end_slot_construction(slot.key)

    sel().log_api_access(
        caller=caller,
        operation="chat.slot_import",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"to={slot.key},messages={len(messages)},"
            f"origin={origin or 'unknown'},agent={slot.agent or 'default'},"
            f"resume={resume_mode}"
        ),
    )
    # Everything is in place -- transcript persisted, Layer B joined. The slot
    # was RETRACTED from ``_slots`` for the async finalization tail above (so no
    # raw acquirer could reach it mid-finalization); this re-registers it now that
    # it is a complete, resumable session. The finally above ended construction,
    # so this push is the first frame a client sees and it shows a fully
    # materialised session.
    state._slots[slot.key] = slot

    # ARRIVAL FILING REPAIR. The placement above was checked while the slot was
    # RETRACTED from ``_slots``, and the folder delete handler's unfile sweep
    # iterates exactly that mapping -- so a delete committing during the durable
    # save could not reach this session and left it pointing at a row that no
    # longer exists. Re-checking HERE, after re-registration, is what closes
    # that: from this line on the sweep can see the slot, so this is the last
    # moment a delete can be missed. A gone folder is cleared and re-saved,
    # which renders the session at the top level -- what an unfiled arrival
    # always did -- rather than at a dangling id no later folder operation
    # corrects. That re-save is best-effort about a LOCK (a timeout marks the
    # slot dirty for the periodic flush) but NOT about a concurrent delete: see
    # the refusal branch below, which rolls the import back rather than
    # reporting it landed. Before the push below, so the first frame a client
    # sees carries the repaired placement rather than one it has to be
    # corrected out of.
    async def _refuse_as_deleted(witness: str) -> web.Response:
        """Roll the import back and answer a coded failure, never ``ok``.

        Shared by both refusal paths below so they cannot drift: whichever
        witness fired, the session is gone and exactly the same unwinding is
        owed — drop the slot, undo the Layer B join, remove its files (those
        helpers are local to this module, so the permanent delete does not
        unwind them). *witness* names which one fired, for the log only; the
        wire answer is identical because the caller's situation is identical.
        """
        # WHAT MAY THIS REFUSAL CLAIM? The transcript was persisted BEFORE this
        # point, and the witness above collapses three outcomes into "deleted":
        # the file is gone, the file belongs to a NEW incarnation, and existence
        # is unverifiable. Only the first lets this answer say nothing was kept.
        #
        # The other two leave a file on disk that must NOT be unlinked here -- a
        # new incarnation is somebody else's session, and an unverifiable read
        # names nothing that can safely be removed -- so the honest answer
        # discloses the leftover instead of asserting a clean slate. Retryable
        # either way; only the promise differs. Read BEFORE the unwinding below,
        # so it reports the disk as the refusal found it.
        remains = await asyncio.to_thread(session_transcript_remains, state, slot)
        # KEY-SCOPED CLEANUP NEEDS AN IDENTITY GUARD, for the same reason the
        # file above is left alone: the witness fires when this slot's session was
        # deleted, and a replacement can land at the SAME key while this tail
        # runs. Popping by key alone would then drop the replacement's slot, and
        # forgetting the join by key alone would take its mapping and its files.
        # Compare the OBJECT: only this import's own slot is this object, so a
        # replacement is left exactly as its own writer left it.
        #
        # THREE OUTCOMES, NOT TWO. ``dict.get`` answers ``None`` for an ABSENT key
        # exactly as it does for a REPLACED one, and only the replaced case must be
        # left alone. Absent is what the ORDINARY permanent delete produces -- it
        # pops by key and puts nothing back -- and there this import's own Layer B
        # pair is nobody else's, while the delete does not unwind these
        # module-local helpers (see this function's docstring). Folding absent into
        # replaced orphans a ``.json``, a ``.jsonl`` and a join for a session with
        # no tab, and nothing re-cleans it: this return is terminal and re-arms
        # nothing.
        current = state._slots.get(slot.key)
        if current is slot:
            state._slots.pop(slot.key, None)
            sid = _forget_layer_b_join(sessions, sm_key) or layer_b_sid
            if sid:
                await asyncio.to_thread(_unlink_layer_b_files, sid)
        elif current is None:
            # Nothing to pop, and the cleanup the delete does not do is owed here.
            #
            # Scoped to ``layer_b_sid``, this import's OWN sid, rather than to the
            # sid the join reports: in the narrower case where a replacement
            # landed and was itself popped, the mapping at ``sm_key`` belongs to
            # that replacement. Unlinking the sid it names would delete that
            # session's files, and forgetting it would drop its mapping and its
            # continuable mark -- the harm the object comparison above prevents
            # for the REPLACED case, which the ABSENT case cannot inherit from it
            # because ``dict.get`` answers alike for both.
            #
            # So the join is dropped only while it still NAMES this import's own
            # sid. ``resumable_sid`` and ``forget_conversation`` both resolve
            # ``_session_map.get`` on the same folded key, so the guard reads the
            # exact value the forget would report and delete, and both are
            # synchronous with no await between them, so nothing interleaves on
            # the loop. A foreign mapping is left to its own writer, and an
            # import holding no Layer B of its own drops nothing.
            if layer_b_sid and _resolve_layer_b_sid(sessions, sm_key) == layer_b_sid:
                _forget_layer_b_join(sessions, sm_key)
            if layer_b_sid:
                await asyncio.to_thread(_unlink_layer_b_files, layer_b_sid)
        else:
            logger.warning(
                "session_transfer: slot=%s was replaced during import; leaving the "
                "replacement's slot, join and files untouched",
                slot.key,
            )
        # Covers BOTH witnesses by sitting in the shared path: the session is
        # gone, so a folder this import created for it has nothing left to hold.
        # After the pop, so the importing slot is already out of the mapping the
        # rollback reads and does not count as a session filed into the row.
        await discard_arrival_folders(state, created_rows)
        logger.warning(
            "session_transfer: slot=%s was permanently deleted during import "
            "(%s); refusing to report the transfer as landed",
            slot.key,
            witness,
        )
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_import",
            outcome="error",
            source="dashboard",
            resources=f"to={slot.key},transcript_remains={remains}",
            error="session deleted during import",
        )
        if remains:
            return web.json_response(
                {
                    "error": "the imported session was deleted while it was "
                    "being installed, and a transcript file remains on disk "
                    "that this instance cannot safely remove; please retry",
                    "code": "transfer_import_deleted_partial",
                },
                status=409,
            )
        return web.json_response(
            {
                "error": "the imported session was deleted while it was "
                "being installed; nothing was kept",
                "code": "transfer_import_deleted",
            },
            status=409,
        )

    if slot.folder_id and not await arrival_folder_exists(state, slot.folder_id):
        logger.info(
            "session_transfer: arrival folder %s went away during import of %s; "
            "leaving the session unfiled",
            slot.folder_id,
            slot.key,
        )
        slot.folder_id = ""
        # THE REPAIR MAY ONLY WRITE A SLOT IT STILL OWNS. The existence check
        # above awaits, and a close landing inside that await pops the slot and
        # THEN persists ``closed=True`` (``chat_handlers`` pops first, then saves
        # with the flag). This object's in-memory ``closed`` is still False, so an
        # unguarded repair save writes that flag back OFF: the archived record
        # loses the dismissal and the tab the person closed resurfaces.
        #
        # PRESENT, AND THIS OBJECT -- the opposite polarity to
        # ``chat_handlers._slot_still_ours``, which counts an ABSENT key as still
        # ours because a close pops before its own teardown steps. Here an absent
        # key is precisely the close this must yield to, so that helper cannot
        # decide it. Same test as the refusal path's guard above.
        #
        # Skipping rather than refusing, because the import DID land: the
        # transcript is persisted, and a close is the person's own later action on
        # a session that arrived. What the skip leaves behind is a dangling
        # ``folder_id`` on the archived record, which is a state the folder delete
        # handler already documents as "ignored on the next load".
        if state._slots.get(slot.key) is slot:
            # A REFUSAL IS NOT A COMMIT. ``save_slot_off_loop`` converts an
            # exception to ``True`` under ``best_effort`` (the slot is marked
            # dirty and the periodic flush retries), but it returns ``False``
            # CLEANLY for two cases, and they need opposite answers.
            #
            # PINNED WITH ``expected_slot_name``, because the identity check above
            # is synchronous and this save is not: the executor wait frees the
            # event loop, so a close landing in that window pops the slot and
            # persists ``closed=True`` while this call still holds
            # ``closed=False`` in memory. Without the pin the commit-boundary
            # recheck in ``chat_persistence`` is skipped entirely, and the stale
            # snapshot lands on top of the dismissal -- the tab the person closed
            # comes back. The pin makes that check atomic with the write.
            #
            # WHICH ``False`` IS IT: the delete-won guard, or the pin? Re-read the
            # map, which is synchronous and cannot race here. A map that does not
            # hold THIS slot means the pin refused, so a close or replacement won
            # -- the import landed and a close is the person's own later action, so
            # skip exactly as the branch below does. A map that still holds it
            # means the delete-won guard fired, which is terminal: nothing re-arms
            # ``_dirty``, so publishing would answer ``ok: true`` for a session
            # whose file is gone and leave the slot published as a zombie.
            if not await save_slot_off_loop(state, slot, force=True, expected_slot_name=slot.key):
                if state._slots.get(slot.key) is not slot:
                    logger.warning(
                        "session_transfer: slot=%s was replaced at the repair "
                        "save's commit boundary; skipping the filing repair so a "
                        "concurrent close is not overwritten",
                        slot.key,
                    )
                else:
                    return await _refuse_as_deleted("the repair save met the delete-won guard")
        else:
            logger.warning(
                "session_transfer: slot=%s left _slots during the arrival-folder "
                "check; skipping the filing repair so a concurrent close is not "
                "overwritten",
                slot.key,
            )

    # THE ROW THIS ARRIVAL SHARES IS RECORDED HERE, immediately above the final
    # witness. A filing that ADOPTED its destination has to leave something behind
    # for the rollback of whichever import CREATED that row: an archived session
    # is invisible to the live-slot occupancy check, so without a mark that
    # rollback would delete a placement this session still points at.
    #
    # Written on this path rather than in the resolver, and that is the whole
    # point of the placement: an adoption that never became a session needs no
    # protection, and a mark the resolver wrote could not be taken back when the
    # import failed -- two concurrent same-origin imports both failing leave each
    # other's rows marked and unreclaimable for good.
    #
    # ABOVE the witness, because this call is the last await on the path and a
    # ``DELETE /api/sessions/{key}`` can land inside it. Below the witness it
    # would yield the loop past the last check, so that delete removes the
    # transcript and pops the slot and the handler still publishes ``200 ok``,
    # with nothing downstream to correct it -- the identical window the witness
    # exists to close, reopened by being one line later. Above it, the same delete
    # is caught and the request refuses. A second witness below this call buys the
    # same guarantee and costs either an extra ``stat`` on every import that
    # adopted nothing, or a conditional witness, which is the case analysis the
    # heading below refuses.
    #
    # The cost of that ordering is a refusal that can follow the mark: a delete
    # landing in this await leaves the row marked while the import gives up, so
    # the creating import's rollback can never reclaim it. That is one visible,
    # deletable sidebar row, and only when the creating import ALSO failed -- the
    # next arrival from that origin adopts the row instead of making another. It
    # cannot be unwound here, because taking a mark back needs each import's own
    # claim recorded on the row, and one import's failure would then strip
    # another's.
    #
    # Only when the destination was adopted. A row THIS import created is in
    # ``created_folders``, and no other import holds those ids, so no other
    # rollback can reach them. Destination only: an adopted parent keeps a
    # surviving child, which the rollback's second guard already spares.
    adopted_destination = bool(slot.folder_id) and slot.folder_id not in created_folders
    if adopted_destination or hidden_rows:
        if not await mark_arrival_folder_shared(
            state,
            slot.folder_id if adopted_destination else "",
            unhide=hidden_rows,
        ):
            # THE MARK IS THE ONLY THING SPARING AN ADOPTED ROW once this session
            # archives: the rollback's occupancy guard reads LIVE slots, and an
            # archived session is popped out of that mapping. So an unrecorded
            # mark means a concurrent creator's rollback can reclaim the row while
            # this transcript still points at it, and the person is left with a
            # session filed into a folder that is gone.
            #
            # Unfiling instead, which is the state the folder-gone repair above
            # already produces and which the folder delete handler documents as
            # "a dangling id can legitimately exist" -- except this makes it true
            # rather than merely tolerated, because the id is cleared and
            # persisted rather than left dangling. Same shape as that repair,
            # deliberately: the slot-identity guard so a concurrent close is not
            # overwritten, and the delete-won ``False`` treated as terminal.
            if state._slots.get(slot.key) is slot:
                slot.folder_id = ""
                # Pinned and discriminated exactly as the repair save above, and
                # for the same reason: the identity check is synchronous, this save
                # is not, and a close landing in the gap would otherwise have its
                # ``closed=True`` overwritten by this call's stale ``closed=False``.
                if not await save_slot_off_loop(
                    state, slot, force=True, expected_slot_name=slot.key
                ):
                    if state._slots.get(slot.key) is not slot:
                        logger.warning(
                            "session_transfer: slot=%s was replaced at the "
                            "unfiling save's commit boundary; skipping so a "
                            "concurrent close is not overwritten",
                            slot.key,
                        )
                    else:
                        return await _refuse_as_deleted(
                            "the unfiling save met the delete-won guard"
                        )
            else:
                logger.warning(
                    "session_transfer: slot=%s left _slots before the shared-row "
                    "mark could be recorded; skipping the unfiling so a "
                    "concurrent close is not overwritten",
                    slot.key,
                )

    # ONE WITNESS ON EVERY PATH TO SUCCESS, AND NO AWAIT BELOW IT. The guard above
    # only fires when the arrival FOLDER went away, so on its own it leaves the
    # common case unchecked: a ``DELETE /api/sessions/{key}`` landing in the
    # folder-existence await removes this transcript and pops the slot while the
    # folder it pointed at is still perfectly fine, so the branch is skipped and
    # the handler would answer ``200 ok`` for data that has already been
    # destroyed. Nothing downstream corrects that -- the success return does not
    # re-arm ``_dirty`` and the delete's pop is terminal -- so the misleading
    # ``ok`` is the permanent record.
    #
    # The second half of that heading carries as much weight as the first, and is
    # why the arrival-row mark sits above: any await between this check and the
    # response reopens the very window the check closes, because the delete lands
    # inside that await and the check has already passed.
    # ``test_a_delete_landing_in_the_shared_row_mark_refuses`` is what keeps an
    # await from drifting back below it.
    #
    # Unconditional rather than an ``elif``, which would be the cheaper shape and
    # the wrong one: a successful ``save_slot_off_loop`` does NOT imply the
    # delete-won guard reached a decision, because ``best_effort`` converts a
    # raising save to ``True``. Checking every time makes "no path reaches ``ok``
    # without passing the witness" true by structure instead of by case analysis,
    # and ``test_the_witness_still_runs_when_the_repair_save_reported_success``
    # is what keeps that shape from being quietly narrowed back to an ``elif``.
    # The cost is one extra ``stat`` per import, which a request already bounded
    # by the live-slot cap can carry.
    #
    # ``session_was_deleted`` is the save's own witness -- the export path's
    # snapshot (``transcript_snapshot.TRANSFER``) asks it twice, and its docstring
    # names this caller class: one that republishes a slot's content and so cannot
    # rely on observing the guard's ``False``, because the periodic flush can reach
    # the guard first and clear ``_dirty``. Off the loop because it stats and reads
    # metadata.
    if await asyncio.to_thread(session_was_deleted, state, slot):
        return await _refuse_as_deleted("the delete witness fired after the finalization tail")

    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "key": slot.key,
            "title": slot.title,
            "messages": len(messages),
            # Resume fidelity, so the SENDER can say "Sent" vs "Sent (transcript
            # only)" instead of showing the same green row either way.
            "resume_mode": resume_mode,
            # The arrival folder the import was filed in ("" when unfiled), so
            # the importing tab can open a collapsed folder path to the row.
            "folder_id": slot.folder_id,
        }
    )
