"""Slot notes: ``POST /api/chat/slots/{slot}/note`` and the deferred hold a note waits in
while a turn runs.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _MAX_DEFERRED_NOTES,
        _NOTE_CONTEXT_MAX_AGE,
        _UNSET,
        MAX_DEFERRED_NOTE_CHARS,
        MAX_SOURCE_LABEL_LEN,
        SOURCE_LABEL_CTRL_RE,
        DashboardState,
        DeferredHoldFull,
        DeferredHoldRebound,
        _build_pending_context_entry,
        _ChatSlot,
        _is_secret_shaped_context,
        _normalize_source,
        _reauthorize_after_await,
        _source_cap_reached,
        _validate_content,
        _validate_max_age,
        _validate_source,
        deny_app_slot_session_access,
        effective_session_key,
        logger,
        note_hold_durable,
        persist_deferred_notes_sync,
        read_bounded_json,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        slot_history_key,
        slot_not_found,
    )


def _discard_held_note(slot: _ChatSlot, note: dict[str, object]) -> None:
    """Remove *note* from the live hold by IDENTITY, if it is still there.

    Identity, never equality: two same-content notes from a capped source are
    byte-identical dicts (their context half is None), and ``list.remove``
    would evict the FIRST equal one — a sibling note that already received its
    durable 200 — leaving this failed note behind to be persisted by the next
    successful write. A note the flush already drained is simply absent; that
    absence carries NO meaning here (it can be delivered OR dropped at the
    rebind seam), which is why the caller answers from positive evidence, not
    from this function.
    """
    for i, held in enumerate(slot._deferred_notes):
        if held is note:
            del slot._deferred_notes[i]
            return


def _note_delivered_live(slot: _ChatSlot, note: dict[str, object]) -> bool:
    """True when a delivered row stamped with this note's id is in the slot's
    LIVE message list — evidence clause (a): the flush delivered the note this
    lifetime, and the save that commits the row retires its durable entry.
    In-memory and synchronous, so every branch can afford it. A note with no
    id has no row stamp to look for and reads as not-delivered, toward the
    branch's refusal (retryable, never a silent unkept promise)."""
    note_id = note.get("id")
    if not isinstance(note_id, str) or not note_id:
        return False
    for row in slot.messages:
        row_meta = row.get("meta")
        if isinstance(row_meta, dict) and row_meta.get("noteId") == note_id:
            return True
    return False


async def _persist_deferred_note_hold(
    state: DashboardState,
    slot: _ChatSlot,
    note: dict[str, object],
    authorized_history_key: str,
) -> web.Response | None:
    """Make a just-held /note durable before the 200 acknowledges it.

    Returns ``None`` on success (or when there is nothing durable to keep the
    promise against) and a non-200 the caller must return otherwise.
    Semantics — durable-before-200, honestly degraded at the edges:

    - **No conversation log** (memory-only deployment, bare test states):
      nothing survives a restart at all, so a 200 keeps its original
      "accepted for this gateway lifetime" meaning. No write, no failure.
    - **No metadata line yet** (the guard refuses the merge): the SLOT has no
      durable identity, so a restart drops the tab itself and there is no
      restored slot the note could outlive. Accepted without a durable copy —
      but ONLY when the slot never had one: a file that existed when this
      write began and is gone under the lock means a concurrent permanent
      delete won, and that is refused with the uniform not-found shape
      instead, because the 200's durable promise was just destroyed along
      with the session itself.
    - **Rebind in the window** (DeferredHoldRebound): the slot does not
      route to the transcript this note was authorized against, so the write
      was refused rather than landing app content in a foreign transcript's
      metadata. Refused with the endpoint's uniform not-found shape — the
      same answer the ownership gate gives, so nothing an unauthorized caller
      can observe distinguishes the cases. A note the concurrent flush
      DROPPED at the rebind seam takes this 404 too: it was never delivered
      and has no durable copy, so a 200 would be a delivery promise nothing
      owns.
    - **Hold full** (DeferredHoldFull): admitting the entry would evict a
      retained one — the only durable copy of an already acknowledged note —
      so the NEW note is refused with the live cap's retryable 429.
    - **Write raises** (lock timeout, I/O error): the promise cannot be kept,
      so the note is discarded and the caller gets a retryable 503 rather
      than a 200 that lies about durability.

    On EVERY branch — the success path included — the 200 stands only on
    POSITIVE EVIDENCE that the note has an owner, never on inference from a
    negative signal (reading absent-from-the-hold as "delivered" misreads a
    rebind-dropped note; reading a written merge as "durable" misreads a
    flush-side drop under a diverged channel-origin key). The evidence
    clauses, any one sufficient:

    (a) a delivered row stamped ``meta.noteId`` is in the slot's LIVE message
        list — the flush delivered it this lifetime (checked here,
        in-memory);
    (b) its id is in the durable hold — on disk already (a sibling's merge
        writer commits the WHOLE live list), or in the merge this write
        committed (resolved under the lock, carried on the outcome and both
        hold exceptions);
    (c) its delivered row is in the COMMITTED transcript (same locked
        resolver).

    With evidence, an error answer would make the caller re-post a note that
    is already delivered or already durable, and the restore would then
    produce a duplicate. Without evidence, the branch's refusal is the honest
    answer. The generic failure branch carries no resolver verdict (the
    writer may have failed before its guard ran), so it makes the durable
    observation itself with :func:`_note_already_durable`.
    """
    conversation_log = getattr(state, "conversation_log", None)
    if conversation_log is None:
        return None
    # Whether the slot HAS a durable identity, decided before the write. The
    # slot-side flag is MONOTONIC and synchronous — a permanent delete racing
    # this worker cannot unwind it, so a delete landing even before the probe
    # below still reads as "the slot had an identity" and a no-line outcome is
    # refused rather than 200-acknowledged. The mtime probe supplements it for
    # a file that exists without this slot object ever having observed it.
    # A slot that never had a file keeps the documented
    # accepted-without-durable-copy semantics.
    had_durable_identity = bool(getattr(slot, "_disk_meta_observed", False)) or (
        await asyncio.to_thread(conversation_log.mtime_of, authorized_history_key) is not None
    )
    try:
        outcome = await asyncio.to_thread(
            persist_deferred_notes_sync,
            conversation_log,
            slot,
            note,
            authorized_history_key,
        )
    except DeferredHoldRebound as exc:
        if exc.evidence.durable or exc.evidence.committed or _note_delivered_live(slot, note):
            return None
        sel().log_api_access(
            caller=str(note.get("session", "")) or "dashboard",
            operation="note_post",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="slot was rebound to another session while the hold was persisting",
        )
        _discard_held_note(slot, note)
        return slot_not_found()
    except DeferredHoldFull as exc:
        if exc.evidence.durable or exc.evidence.committed or _note_delivered_live(slot, note):
            return None
        _discard_held_note(slot, note)
        return web.json_response(
            {
                "error": "slot's durable deferred-note hold is full until its rows are saved",
                "code": "deferred_notes_full",
            },
            status=429,
        )
    except Exception:
        logger.error(
            "Failed to persist the deferred-note hold for slot %s", slot.key, exc_info=True
        )
        # No resolver verdict travels with a generic failure (the writer may
        # have failed before its guard ran), so make the durable observation
        # here — it is still an observation, never an inference. A recorded
        # drop dominates it (same rule as the resolver): a drop-marked
        # durable entry is retired row-lessly by the next save, so it cannot
        # back a delivery promise.
        note_id = note.get("id")
        recorded_dropped = isinstance(note_id, str) and note_id in getattr(
            slot, "_dropped_note_ids", set()
        )
        if (
            not recorded_dropped
            and await _note_already_durable(conversation_log, authorized_history_key, note)
        ) or _note_delivered_live(slot, note):
            return None
        _discard_held_note(slot, note)
        return web.json_response(
            {
                "error": "failed to persist the held note; retry the request",
                "code": "deferred_note_persist_failed",
            },
            status=503,
        )
    if outcome.written:
        if (
            outcome.evidence.durable
            or outcome.evidence.committed
            or _note_delivered_live(slot, note)
        ):
            return None
        # The merge landed but carries NO representation of this note: the
        # concurrent flush dropped it at the rebind seam while the slot's
        # history key still matched (the two keys diverge for a
        # channel-origin slot), so nothing will ever deliver or replay it.
        # Same refusal as the rebind branch — the note's authorization no
        # longer matches where the slot routes.
        sel().log_api_access(
            caller=str(note.get("session", "")) or "dashboard",
            operation="note_post",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="held note was dropped at the rebind seam while the hold was persisting",
        )
        _discard_held_note(slot, note)
        return slot_not_found()
    if had_durable_identity:
        # The guard saw NO metadata line for a slot whose file existed when
        # this write began: a permanent delete won the lock. The session and
        # any durable copy are gone, so a 200 here would acknowledge a note
        # that can never be delivered or restored. Refuse with the endpoint's
        # uniform not-found shape (what the caller would have seen had the
        # delete landed a moment earlier).
        if outcome.evidence.committed or _note_delivered_live(slot, note):
            return None
        _discard_held_note(slot, note)
        return slot_not_found()
    return None


async def _note_already_durable(
    conversation_log: object, authorized_history_key: str, note: dict[str, object]
) -> bool:
    """True when a concurrent sibling's merge writer already persisted *note*.

    Off the loop (locked file read). When it answers True the note must NOT
    be rolled back or error-answered: its durable entry is real, the restore
    will replay it, and a 503/429 would make the caller re-post a duplicate.
    """
    note_id = note.get("id")
    if not isinstance(note_id, str) or not note_id:
        return False
    return await asyncio.to_thread(
        note_hold_durable, conversation_log, authorized_history_key, note_id
    )


async def api_chat_slot_note(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/note — visible transcript line + silent next-turn context.

    A background actor (a cron, an app) uses this to drop a short, DECLARATIVE
    note into a chat that is both (a) visible in the transcript right away and
    (b) known to the agent if the user later asks about it -- WITHOUT firing an
    LLM turn.

    A plain transcript append is not enough on its own: a live provider holds its
    own in-memory conversation state and a normal send forwards only the new user
    message, so a row written via ``slot.append()`` alone is never seen by the
    model. The channel that IS seen is ``_pending_context``, which is drained and
    prepended to the next user message. So the endpoint does two writes against
    the same slot:

    1. visible line -- ``slot.append(role="inject", cls="reconcile-note")`` so it
       renders in the transcript and persists.
    2. context entry -- a ``_pending_context`` entry (the same channel
       ``/context`` uses) drained onto the user's next manual message exactly
       once, then cleared.

    Both writes always happen. A context-only write is ``POST /context``, which
    already exists; there is no visible-only mode, because no caller wanted one.

    A session reset in between can replay the transcript row into the new
    session, so the model may see the note twice in one prompt. The queued copy
    is kept regardless: the replay is char-budget bounded, so dropping it would
    lose an older note the replay had already trimmed away.

    Notes are meant to be declarative -- state what happened, never ask. An
    interrogative note rides along as background context and may get answered on
    the next unrelated turn. The context half defaults to a 24h ``maxAge`` so a
    never-followed-up note self-expires; the visible line is permanent.

    Body::

        {
            "content": "...",         // required, declarative, non-empty string
            "source": "board-sync",   // optional frame label + per-source cap bucket;
                                      //   <=64 chars, no control chars; empty -> "note"
            "maxAge": 86400,          // optional seconds; omitted -> 24h default.
                                      //   Explicit null -> no expiry, as on /context.
            "ephemeral": true         // optional, default true (passed to the context entry)
        }

    A credential-shaped ``content`` never seats a context half: the context
    half reaches the model in-band and is not a confidential channel. The
    visible line is still written, redacted, and ``contextSkipped`` is true.

    Returns ``{"ok", "appended", "visibleDeferred", "contextSkipped", "pending"}``.
    If the source's per-source context cap is already full the visible line is
    still written and ``contextSkipped`` is true: the cap protects the context
    queue, not the transcript, so the call is NOT 429'd.

    When a turn is already running BOTH halves are held and written at that
    turn's end, so ``appended`` is false and ``visibleDeferred`` is true. Its
    order is preserved, and the hold is DURABLE: it is persisted
    into the slot's own metadata line before the 200 is returned, replayed by
    both slot-restore paths after a gateway restart, and retired by the save
    that commits the delivered rows. A caller therefore never needs to re-post
    after a restart; the one retry signal is a 503 ``deferred_note_persist_failed``,
    which means the hold could not be made durable and was not accepted.
    Appending mid-turn would take the row the replay path skips and cause the
    user's own request to be replayed; queueing the context mid-turn would let
    the turn already in flight drain it, so the note would shape the request it
    was written after and the next turn would find nothing. Every rejection
    still happens on the POST. ``pending`` counts held entries too, so it always
    reports what the model will receive. Holding more than
    ``_MAX_DEFERRED_NOTES`` on one turn is a 429 ``deferred_notes_full`` (also
    returned when the durable hold still carries delivered-but-unsaved entries
    at its ceiling), and a held note's content is bounded at
    ``MAX_DEFERRED_NOTE_CHARS`` — a 413 ``deferred_note_too_large`` — because
    the durable copy is persisted verbatim and must replay exactly what the
    200 accepted.
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        # Byte-identical to the ownership denial below, or an app token could tell
        # "not mine" from "does not exist" and enumerate foreign slot names.
        return slot_not_found()

    request_app = request.get("app", "")
    denied = deny_app_slot_session_access(request_app, slot, name, "note_post")
    if denied is not None:
        return denied

    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    content = body.get("content", "")
    bad = (
        _validate_content(content)
        or _validate_source(body.get("source"))
        or _validate_max_age(body.get("maxAge"))
    )
    if bad is not None:
        return bad

    # Default an empty, absent, or whitespace-only source to "note" so the drain
    # frame reads [Background context from "note"] rather than empty quotes.
    source = _normalize_source(body.get("source")) or "note"

    # The VISIBLE source label (the "Note from ..." pill) is stamped from the
    # AUTHENTICATED caller, never from the body. ``request_app`` is set by the
    # app-token auth middleware from the validated token record (an app's
    # registered slug), so it cannot be spoofed: app A posting
    # ``source="Kiro"`` or another app's name cannot make the bubble attribute
    # the note to anyone but A. A dashboard user carries an EMPTY ``request_app``
    # and gets no author pill — the note is their own and needs no attribution.
    # Because the label is a trusted, slug-shaped identity and not
    # caller-controlled free text, it needs no credential/exfil redaction (an
    # app slug cannot carry a spliced credential span), which is why the
    # two-pass ``redact_caller_text`` and ``slot_buffers``'s redactor-sink
    # allowlist entry both drop out with this change. The body's ``source`` is
    # still read above for the internal drain-frame cap bucket only.
    #
    # An app name has no length bound at the single-name contract and the
    # import path caps it at 120 chars, so a slug longer than
    # ``MAX_SOURCE_LABEL_LEN`` is admissible. Apply the SAME structural bound
    # the restore sanitizer applies (length + control char -> "") HERE, so what
    # the admit path stamps always survives a persist/restore round-trip
    # unchanged: without it a 65+ char app name would persist verbatim, then be
    # collapsed to "" on restore, and the flushed note would silently lose its
    # author pill for that app forever.
    display_source = request_app
    if len(display_source) > MAX_SOURCE_LABEL_LEN or SOURCE_LABEL_CTRL_RE.search(display_source):
        display_source = ""

    # Ownership was decided before the body read. Re-decide it here, against the
    # slot as it is NOW, because that await is long enough for a rebind.
    stale = _reauthorize_after_await(state, slot, name, request_app, "note_post")
    if stale is not None:
        return stale

    # A turn in flight owns the tail of the transcript: the replay path skips
    # exactly one recall-eligible row to drop the current-turn user message, and
    # an `inject` row appended now would take that slot and get skipped in its
    # place, replaying the user's request twice. So the visible line is HELD and
    # written at the turn's end, which is why `appended` is reported separately.
    # This is decided BEFORE either write: a note rejected for a full hold must
    # not leave its context half behind to reach the next turn anyway.
    deferred = slot.running
    if deferred and len(slot._deferred_notes) >= _MAX_DEFERRED_NOTES:
        return web.json_response(
            {
                "error": f"slot already holds {_MAX_DEFERRED_NOTES} deferred notes",
                "code": "deferred_notes_full",
            },
            status=429,
        )
    if deferred and len(content) > MAX_DEFERRED_NOTE_CHARS:
        # A held note is persisted VERBATIM before the 200, so
        # what the 200 accepts is exactly what a restart replays — truncating
        # the durable copy would replay altered content for an acknowledged
        # note. The bound therefore sits at the boundary, where the caller can
        # act on it: shorten the note, or wait for the turn to end (immediate
        # notes keep the larger shared content bound).
        return web.json_response(
            {
                "error": (
                    f"a note posted during a running turn is capped at "
                    f"{MAX_DEFERRED_NOTE_CHARS} characters; shorten it or wait "
                    "for the turn to end"
                ),
                "code": "deferred_note_too_large",
            },
            status=413,
        )

    # The per-source cap protects the context QUEUE, not the transcript. So when
    # the context half is capped we still write the VISIBLE line -- the audit
    # record the caller came for -- and report contextSkipped=true, rather than
    # 429-ing the whole request and losing the visible note too. This matters
    # most for the default source="note" bucket, which every sourceless caller
    # shares. An omitted maxAge takes this endpoint's 24h default; an explicit
    # null means no expiry, the same as it does on /context.
    context_skipped = False
    context_entry: dict[str, object] | None = None
    if _is_secret_shaped_context(content):
        # Pending context reaches the model in-band, so a credential-shaped note
        # never seats a context half. Its visible line is still written,
        # redacted below, and the skip is reported the way a full cap is.
        context_skipped = True
    elif _source_cap_reached(slot, source):
        context_skipped = True
    elif deferred and not slot.has_pending_context_seat():
        # The HELD path must refuse the context half at admission, the same way
        # the immediate arm does when ``append_pending_context`` returns False.
        # A held note's context is seated later, by ``flush_deferred_notes`` ->
        # ``append_pending_context`` at turn end; if the queue is already at the
        # seat ceiling now, that append refuses, and the only trace is a log
        # warning -- nothing the caller ever sees. So the note would be
        # acknowledged (200, ``visibleDeferred``) with its context silently
        # dropped: the acknowledge-then-lose behaviour this endpoint
        # exists to remove, moved onto the held path. ``has_pending_context_seat``
        # counts live entries PLUS each held note's reserved context half, so the
        # check is accurate: a later ``/context`` arrival cannot take the seat the
        # flush would need, and expiry between now and the flush can only free
        # seats, never consume them. We hold the VISIBLE line regardless (the
        # audit record the caller came for) and report contextSkipped=true.
        context_skipped = True
    else:
        max_age = body.get("maxAge", _UNSET)
        if max_age is _UNSET:
            max_age = _NOTE_CONTEXT_MAX_AGE
        context_entry, err = _build_pending_context_entry(
            slot, content, source, body.get("ephemeral", True), max_age
        )
        if err is not None:
            return err
        assert context_entry is not None
        # A held note's context is queued by the flush, not here. The drain runs
        # inside the turn and after its task is assigned, so an entry queued now
        # is read by the turn already running -- the note would shape the request
        # it was written after, and the next turn would find nothing.
        if not deferred:
            # Both immediate halves resolve their destination LATE, so each
            # records the session it was authorized against -- same reason the
            # deferred arm below does, and checked at those later seams. The
            # entry is seated only after the visible row lands, below.
            context_entry["noteSession"] = effective_session_key(slot)

    # Caller-controlled content reaching the visible transcript (SSE plus the
    # on-disk JSONL). Redact at this sink so a secret or exfil URL cannot land
    # in user-visible history. The context half is never rewritten: a
    # credential-shaped note seats no context half at all, and the context half
    # otherwise carries the caller's bytes unchanged. Attribution scope: the
    # note CONTENT runs through the exfiltration → credential redaction pair and
    # nothing more — a note carries a source LABEL, not a rewritten body, so no
    # format-char normalization is layered on the content here.
    visible_content, _ = redact_exfiltration_urls(content)
    visible_content, _ = redact_credentials(visible_content)
    if deferred and len(visible_content) > MAX_DEFERRED_NOTE_CHARS:
        # The bound must hold on the PERSISTED string, not just the raw input:
        # redaction can GROW content (each flagged URL becomes a longer
        # [REDACTED: ...] tag), and a persisted entry over the bound is dropped
        # fail-closed by the restore sanitizer — a 200 here would be an
        # acknowledgement the restart silently breaks. The raw-content check
        # above still stands on its own: the context half persists the RAW
        # string, and the sanitizer applies the same bound to it.
        return web.json_response(
            {
                "error": (
                    f"a note posted during a running turn is capped at "
                    f"{MAX_DEFERRED_NOTE_CHARS} characters after redaction; "
                    "shorten it or wait for the turn to end"
                ),
                "code": "deferred_note_too_large",
            },
            status=413,
        )
    if deferred:
        note: dict[str, object] = {
            # Identity for the durable hold's merge (slot_buffers.
            # persist_deferred_notes_sync): a disk entry whose id is absent
            # from the in-memory hold was delivered or dropped, never lost.
            "id": uuid.uuid4().hex[:12],
            "content": visible_content,
            "cls": "reconcile-note",
            "context": context_entry,
            # The AUTHENTICATED caller identity (``request_app``), carried
            # through the durable hold so the flushed visible row can say which
            # app wrote the note -- the same spoof-proof value an immediate note
            # stamps below. Empty for a dashboard user, so the flush renders no
            # "from ..." pill.
            "source": display_source,
            # The session this note was authorized against. The gate above
            # only admits a slot that still routes to its own session, but
            # an unbound slot can acquire a foreign binding while the note
            # is held, and the flush resolves its target late.
            "session": effective_session_key(slot),
        }
        # The transcript this authorization resolves to, captured in the SAME
        # routing observation as the session stamp above: the durable write
        # targets this key and re-verifies the slot still resolves to it
        # under the store lock, so a rebind during the persist window cannot
        # land app-authorized content in a foreign transcript's metadata.
        authorized_history_key = slot_history_key(slot)
        slot._deferred_notes.append(note)
        # Make the hold durable BEFORE the 200 acknowledges it:
        # ``visibleDeferred: true`` is a delivery promise for a transcript
        # line, and an in-memory-only hold silently voids it on a gateway
        # restart. The write persists the CURRENT hold into the slot's own
        # metadata line under the history lock, off the event loop, and the
        # restore paths replay it into ``_deferred_notes`` on the first boot
        # after a restart.
        err = await _persist_deferred_note_hold(state, slot, note, authorized_history_key)
        if err is not None:
            return err
    else:
        slot.append(
            role="inject",
            content=visible_content,
            cls="reconcile-note",
            broadcast=True,
            meta={
                "noteSession": effective_session_key(slot),
                # Attribute the note through the SAME app-label pill an app
                # inject row already uses (``meta.appLabel`` ->
                # ``components.mcpApp.from_app``): ``display_source`` is the
                # authenticated caller identity (``request_app``), so a reader
                # sees "Sent by app {X}" on the note bubble. Omit the key when
                # the caller gave no identity (a dashboard user), since the
                # renderer draws the pill only on a truthy value -- a sourceless
                # note stays unattributed.
                **({"appLabel": display_source} if display_source else {}),
            },
        )
        # Visible row first, the same order the held flush keeps: a raise from
        # the append above fails the request with nothing seated, so the
        # context half is never queued without the row that gives it meaning.
        if context_entry is not None and not slot.append_pending_context(context_entry):
            # The visible line is written; contextSkipped carries the refused
            # half, the same surface the per-source cap uses.
            context_skipped = True

    sel().log_api_access(
        caller=request_app or request.get("user", "dashboard"),
        operation="note_post",
        outcome="ok",
        source="app_kit",
        resources=f"slot={name}",
    )

    # A hold is delivered only if the slot still routes to the same session at
    # flush; a rebind during the hold drops it. An IMMEDIATE note is equally
    # conditional while the slot is UNBOUND, because both halves resolve their
    # destination late and every binding site claims an EMPTY binding
    # (``if not slot.linked_session_key``) -- so an already-bound slot cannot be
    # re-claimed and its immediate note is genuinely unconditional.
    delivery_conditional = deferred or not slot.linked_session_key
    return web.json_response(
        {
            "ok": True,
            "appended": not deferred,
            "visibleDeferred": deferred,
            "deliveryConditional": delivery_conditional,
            "contextSkipped": context_skipped,
            "pending": len(slot._pending_context) + slot.deferred_context_count(),
        }
    )
